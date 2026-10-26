"""Borrow a validated music candidate without sending the original recording."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

PLUGIN_NAMES = {"astrbot_plugin_listen_music", "astrbot_plugin_bili_player"}


class MusicSourceError(RuntimeError):
    """A music selection cannot safely supply audio for a cover."""


class PreparedSong(NamedTuple):
    """A leased local recording and the selected source's display title."""

    path: Path
    title: str


@asynccontextmanager
async def prepared_song(
    context, event, search_id: str, position: int
) -> AsyncIterator[PreparedSong]:
    """Hold a one-shot music download until the caller finishes using it.

    Args:
        context: AstrBot context containing the registered music plugin.
        event: Trusted current event; its session owns the search snapshot.
        search_id: Opaque ID returned by find_music for this session.
        position: One-based position returned by that same search.

    Yields:
        The prepared local path and verified candidate title. The caller must
        finish reading the path before leaving this context manager.

    Raises:
        MusicSourceError: The plugin, selection, or prepared media is invalid.
    """
    if (
        not isinstance(search_id, str)
        or not search_id.strip()
        or len(search_id) > 128
        or type(position) is not int
        or not 1 <= position <= 100
    ):
        raise MusicSourceError("歌曲候选无效，请先重新搜索歌曲。")
    star = next(
        (
            item
            for item in context.get_all_stars()
            if getattr(item, "name", None) in PLUGIN_NAMES
            and getattr(item, "root_dir_name", None) == item.name
            and getattr(item, "module_path", None) in {
                f"data.plugins.{item.name}.main", f"{item.name}.main"
            }
            and getattr(item, "activated", False)
            and not getattr(item, "reserved", False)
        ),
        None,
    )
    if star is None:
        raise MusicSourceError("请先安装并启用“我想听歌！”插件，再搜索歌曲。")
    module = sys.modules.get(star.module_path)
    plugin = getattr(star, "star_cls", None)
    if (
        module is None
        or plugin is None
        or type(plugin) is not getattr(module, "ListenMusicPlugin", None)
        or type(plugin).__module__ != star.module_path
        or not getattr(plugin, "_initialized", False)
        or not all(
            callable(getattr(plugin, name, None))
            for name in (
                "_active_llm_search",
                "_consume_llm_search",
                "_require_search",
                "_require_delivery",
                "_require_media",
            )
        )
    ):
        raise MusicSourceError("听歌插件尚未就绪或版本不兼容，请检查插件加载状态。")

    session_id = event.unified_msg_origin
    search_id = search_id.strip()
    lease = plugin._active_llm_search(session_id, search_id)
    snapshot = plugin._require_search().snapshot(
        search_id=search_id, session_id=session_id
    )
    candidate = snapshot.candidate_at(position) if snapshot is not None else None
    if lease is None or candidate is None:
        raise MusicSourceError("歌曲候选已过期或不属于当前会话，请重新搜索。")
    # These synchronous checks and consumption cannot interleave with another
    # task. A parallel request therefore cannot replay the same selection.
    if not plugin._consume_llm_search(session_id, lease):
        raise MusicSourceError("歌曲候选已被使用，请重新搜索。")
    media_store = plugin._require_media()
    prepared = None
    try:
        try:
            async with asyncio.timeout(200):
                # Both supported releases default to the same 25 MiB / 15 min
                # voice budget; 0.2.x no longer re-exports it from main.py.
                prepared = await plugin._require_delivery().deliver(candidate)
        except TimeoutError as exc:
            raise MusicSourceError(
                "歌曲下载超时，尚未生成翻唱，请稍后重新搜索。"
            ) from exc
        except Exception as exc:
            raise MusicSourceError(
                "歌曲下载失败，尚未生成翻唱，请检查听歌插件。"
            ) from exc
        if not star.activated or star.star_cls is not plugin:
            raise MusicSourceError("听歌插件已停用，本次翻唱已取消。")
        try:
            path = Path(prepared.media.path)
            root = Path(media_store._media_dir).resolve(strict=True)
            resolved = path.resolve(strict=True)
            valid = (
                path.is_absolute()
                and not path.is_symlink()
                and resolved.is_relative_to(root)
                and resolved.is_file()
                and resolved.suffix.lower()
                in {".m4a", ".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus"}
                and 0 < resolved.stat().st_size <= 25 * 1024 * 1024
            )
        except (OSError, ValueError, TypeError) as exc:
            raise MusicSourceError("听歌插件没有提供有效的本地音频。") from exc
        if not valid:
            raise MusicSourceError("听歌插件没有提供有效的本地音频。")
        yield PreparedSong(resolved, candidate.display_title)
    finally:
        if prepared is not None:
            await media_store.release(prepared.media)
