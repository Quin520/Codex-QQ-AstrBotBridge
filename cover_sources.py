"""Select bounded cover sources without exposing download URLs to the model."""

import asyncio
import copy
import json
import logging
import math
import re
import secrets
import shutil
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .access import is_allowed
from .group_context import is_proactive_group
from .music_source import MusicSourceError, PreparedSong, prepared_song
from .video_context import (
    VideoContextError,
    _run_media,
    download_video,
    validate_video_url,
)

_REFERENCE = re.compile(
    r"https?://|(?<![a-z0-9])(?:BV[a-z0-9]{10}|av[1-9][0-9]{0,18})(?![a-z0-9])",
    re.IGNORECASE,
)
_PREFIX = "cover-fallback-"
logger = logging.getLogger(__name__)


@dataclass
class _Selection:
    key: tuple[str, ...]
    generation: object
    expires: float
    plugin: object
    player: object
    songs: tuple


class CoverSources:
    """Prefer exact Bilibili candidates, with an optional reviewed music fallback."""

    def __init__(self, context, config: dict, data_dir: Path):
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir) / "cover-sources"
        self._generations: OrderedDict[tuple[str, ...], object] = OrderedDict()
        self._selections: OrderedDict[str, _Selection] = OrderedDict()

    def _allowed(self, event) -> bool:
        return (
            bool(self.config.get("singing_enabled", False))
            and not (is_proactive_group(event))
            and is_allowed(
                self.config,
                event.get_platform_name(),
                str(event.get_sender_id() or ""),
                str(event.get_group_id() or ""),
            )
        )

    @staticmethod
    def _key(event) -> tuple[str, ...]:
        return (
            str(event.unified_msg_origin),
            str(event.get_platform_id()),
            str(event.get_self_id()),
            str(event.get_sender_id()),
            str(event.get_group_id() or ""),
        )

    def discard(self, event) -> None:
        """Invalidate fallback selections and in-flight searches for this sender.

        Args:
            event: Trusted current message, never model-supplied identity fields.
        """
        key = self._key(event)
        self._generations[key] = object()
        self._generations.move_to_end(key)
        while len(self._generations) > 128:
            self._generations.popitem(last=False)
        now = time.monotonic()
        for token, selection in tuple(self._selections.items()):
            if (
                selection.key == key
                or selection.expires <= now
                or self._generations.get(selection.key) is not selection.generation
            ):
                self._selections.pop(token, None)

    def _plugin(self, name: str, class_name: str):
        for star in self.context.get_all_stars():
            module_path = getattr(star, "module_path", None)
            if (
                getattr(star, "name", None) != name
                or getattr(star, "root_dir_name", None) != name
                or module_path not in {f"data.plugins.{name}.main", f"{name}.main"}
                or not getattr(star, "activated", False)
                or getattr(star, "reserved", False)
            ):
                continue
            module = sys.modules.get(module_path)
            plugin = getattr(star, "star_cls", None)
            if (
                plugin is not None
                and type(plugin) is getattr(module, class_name, None)
                and type(plugin).__module__ == module_path
            ):
                return plugin
        return None

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"status": "error", "message": message}, ensure_ascii=False)

    def _current(self, key: tuple[str, ...], generation: object) -> bool:
        return self._generations.get(key) is generation

    @staticmethod
    def _player(plugin):
        """Use the installed default NetEase implementation, never its sender."""
        if plugin is None or not callable(getattr(plugin, "get_player", None)):
            return None
        player = plugin.get_player(default=True)
        root = type(plugin).__module__.rsplit(".", 1)[0]
        # Music 3.x renamed ncm.py to netease.py; accept only those two concrete
        # reviewed implementations, never an arbitrary default player.
        module_path = type(player).__module__ if player is not None else ""
        if module_path not in {root + ".core.platform.ncm", root + ".core.platform.netease"}:
            return None
        module = sys.modules.get(module_path)
        if (
            player is None
            or type(player) is not getattr(module, "NetEaseMusic", None)
            or type(player).__module__ != module_path
            or not callable(getattr(player, "fetch_songs", None))
            or not callable(getattr(player, "fetch_extra", None))
        ):
            return None
        return player

    async def find(self, event, title: str) -> str:
        """Return session-bound candidates without sending or downloading music.

        Args:
            event: Trusted event that owns the returned candidate handles.
            title: Specific song name or the user's exact Bilibili reference.

        Returns:
            JSON candidate metadata or a user-safe source error.
        """
        if not self._allowed(event):
            return self._error("当前会话未获授权或未启用翻唱。")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            return self._error("请提供具体歌名或 Bilibili 视频链接。")
        title = " ".join(title.split())
        self.discard(event)
        key = self._key(event)
        generation = self._generations[key]
        previous = {"status": "error", "message": "Bilibili 听歌插件尚未就绪。"}
        original = self._plugin("astrbot_plugin_listen_music", "ListenMusicPlugin")
        if original is None:
            original = self._plugin("astrbot_plugin_bili_player", "ListenMusicPlugin")
        media_search = getattr(original, "find_in_bilibili_for_llm", None)
        legacy_search = getattr(original, "find_music_for_llm", None)
        if (
            original is not None
            and getattr(original, "_initialized", False)
            and (callable(media_search) or callable(legacy_search))
        ):
            try:
                async with asyncio.timeout(35):
                    if callable(media_search):
                        result = await media_search(event, title, delivery="audio")
                    else:
                        result = await legacy_search(event, title)
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    previous = parsed
            except (
                aiohttp.ClientError,
                OSError,
                TimeoutError,
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
            ) as exc:
                logger.warning(
                    "Primary cover source search failed (%s)", type(exc).__name__
                )
                previous = {
                    "status": "error",
                    "message": "Bilibili 音源查询失败或超时。",
                }
        if not self._current(key, generation):
            return self._error("歌曲请求已被新的消息替换。")
        if previous.get("status") == "candidates":
            previous["source"] = "bilibili"
            return json.dumps(previous, ensure_ascii=False)
        if _REFERENCE.search(title):
            previous["status"] = "error"
            previous["message"] = (
                str(previous.get("message") or "指定视频暂时不可用。")
                + "如需尝试其他音源，请提供歌名；不会替换你指定的视频。"
            )
            return json.dumps(previous, ensure_ascii=False)
        if not self.config.get("singing_music_fallback", False):
            return json.dumps(previous, ensure_ascii=False)

        plugin = self._plugin("astrbot_plugin_music", "MusicPlugin")
        player = self._player(plugin)
        if player is None:
            return self._error("备用点歌插件未启用或默认播放器不是网易云音乐。")
        model_module = sys.modules.get(
            type(plugin).__module__.rsplit(".", 1)[0] + ".core.model"
        )
        song_type = getattr(model_module, "Song", None)
        candidates = []
        try:
            async with asyncio.timeout(30):
                songs = await player.fetch_songs(keyword=title[:120], limit=5)
                for song in songs[:5]:
                    if not self._current(key, generation):
                        return self._error("歌曲请求已被新的消息替换。")
                    if type(song) is not song_type or not str(song.name or "").strip():
                        continue
                    song = copy.copy(song)
                    if song.duration is not None and not (
                        type(song.duration) in {int, float}
                        and math.isfinite(song.duration)
                        and 0 < song.duration <= 900_000
                    ):
                        continue
                    try:
                        if not song.audio_url:
                            song = await player.fetch_extra(song)
                        if type(song) is not song_type or not song.audio_url:
                            continue
                        validate_video_url(song.audio_url)
                    except (
                        aiohttp.ClientError,
                        OSError,
                        TimeoutError,
                        VideoContextError,
                    ) as exc:
                        logger.warning(
                            "Fallback candidate unavailable (%s)", type(exc).__name__
                        )
                        continue
                    candidates.append(song)
        except TimeoutError:
            if not candidates:
                return self._error("网易云备用音源查询超时，请稍后重试。")
            logger.warning("Fallback search timed out after finding usable candidates")
        except (
            aiohttp.ClientError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as exc:
            logger.warning(
                "Fallback cover source search failed (%s)", type(exc).__name__
            )
            return self._error("网易云备用音源暂时不可用，请稍后重试。")
        if not self._current(key, generation):
            return self._error("歌曲请求已被新的消息替换。")
        if (
            not self._allowed(event)
            or not self.config.get("singing_music_fallback", False)
            or self._plugin("astrbot_plugin_music", "MusicPlugin") is not plugin
        ):
            return self._error("备用点歌插件已停用，本次查询已取消。")
        if not candidates:
            return self._error("备用音源没有找到可播放的匹配歌曲。")
        token = _PREFIX + secrets.token_hex(16)
        self._selections[token] = _Selection(
            key, generation, time.monotonic() + 300, plugin, player, tuple(candidates)
        )
        while len(self._selections) > 128:
            self._selections.popitem(last=False)
        return json.dumps(
            {
                "status": "candidates",
                "source": "netease",
                "search_id": token,
                "candidates": [
                    {
                        "position": index,
                        "title": str(song.name)[:160],
                        "artist": str(song.artists or "")[:120],
                    }
                    for index, song in enumerate(candidates, 1)
                ],
            },
            ensure_ascii=False,
        )

    @asynccontextmanager
    async def prepared(self, event, search_id: str, position: int):
        """Consume one owned candidate and lease its verified local audio.

        Args:
            event: Current trusted request, rechecked before yielding audio.
            search_id: Opaque handle returned by this service or find_music.
            position: Candidate's one-based position.

        Yields:
            PreparedSong while its bounded temporary audio exists.

        Raises:
            MusicSourceError: Invalid ownership, source failure, or invalid media.
        """
        if not self._allowed(event):
            raise MusicSourceError("当前会话未获授权或未启用翻唱。")
        if (
            not isinstance(search_id, str)
            or len(search_id) > 128
            or type(position) is not int
            or not 1 <= position <= 100
        ):
            raise MusicSourceError("歌曲候选无效，请重新搜索。")
        if not search_id.startswith(_PREFIX):
            async with prepared_song(self.context, event, search_id, position) as song:
                yield song
            return
        selection = self._selections.get(search_id)
        if (
            selection is None
            or selection.key != self._key(event)
            or not self._current(selection.key, selection.generation)
            or selection.expires <= time.monotonic()
            or position > len(selection.songs)
        ):
            raise MusicSourceError("歌曲候选已过期或不属于当前会话，请重新搜索。")
        self._selections.pop(search_id)
        if (
            not self.config.get("singing_music_fallback", False)
            or self._plugin("astrbot_plugin_music", "MusicPlugin")
            is not selection.plugin
            or self._player(selection.plugin) is not selection.player
        ):
            raise MusicSourceError("备用音源或点歌插件已关闭，本次翻唱已取消。")
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise MusicSourceError("未找到 ffprobe，无法安全验证备用音源。")
        song = selection.songs[position - 1]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="source-", dir=self.data_dir
        ) as temporary:
            path = Path(temporary) / "source.audio"
            try:
                await download_video(song.audio_url, path, 25 * 1024 * 1024)
                result = await _run_media(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        "mp3,flac,wav,ogg,mov,aac",
                        "-show_entries",
                        "format=duration:stream=codec_type",
                        "-of",
                        "json",
                        str(path),
                    ],
                    15,
                )
                probe = json.loads(result)
                duration = float(probe.get("format", {}).get("duration", 0))
                if (
                    not any(
                        stream.get("codec_type") == "audio"
                        for stream in probe.get("streams", [])
                    )
                    or not math.isfinite(duration)
                    or not 0 < duration <= 900
                    or not path.is_file()
                    or not 0 < path.stat().st_size <= 25 * 1024 * 1024
                ):
                    raise MusicSourceError(
                        "备用音源不是有效音频，或超过 15 分钟/25 MB 限制。"
                    )
            except (
                VideoContextError,
                OSError,
                TimeoutError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                raise MusicSourceError(
                    "备用音源下载或验证失败（仅限公网音频、25 MB 以内），尚未生成翻唱。"
                ) from exc
            if (
                not self._allowed(event)
                or not self.config.get("singing_music_fallback", False)
                or not self._current(selection.key, selection.generation)
                or self._plugin("astrbot_plugin_music", "MusicPlugin")
                is not selection.plugin
            ):
                raise MusicSourceError("请求或音源设置已变化，本次翻唱已取消。")
            yield PreparedSong(path, str(song.name)[:160])
