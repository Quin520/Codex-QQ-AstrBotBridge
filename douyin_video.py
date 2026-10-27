"""Deliver public Douyin videos through the installed parser's public API."""

import asyncio
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.core.message.components import Plain

from .access import is_allowed
from .group_context import is_proactive_group
from .video_context import VideoContextError, _run_media

MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_DURATION_SECONDS = 15 * 60
_LINKS = re.compile(r"https?://[^\s<>\"'，。！？；（）【】]+", re.IGNORECASE)
_LONG_HOSTS = {"douyin.com", "www.douyin.com"}
_SHORT_HOSTS = {"v.douyin.com", "jx.douyin.com"}


class DouyinVideoError(RuntimeError):
    """A public explanation without private cookie values or filesystem paths."""


def normalize_douyin_url(value: str) -> str:
    """Accept one exact official video/share URL, never a general fetch target.

    Args:
        value: A user-supplied link or a real web-search result URL.

    Returns:
        An HTTPS URL with tracking/query parameters removed.

    Raises:
        DouyinVideoError: Unsupported host, resource type, port or URL syntax.
    """
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError
        value = value.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or any(ord(char) < 33 or char == "\\" for char in value)
        ):
            raise ValueError
        host = (parsed.hostname or "").lower()
        path = parsed.path
        if host in _LONG_HOSTS and re.fullmatch(r"/video/[0-9]{10,20}/?", path):
            return "https://www.douyin.com" + path.rstrip("/")
        if host in _SHORT_HOSTS and re.fullmatch(r"/[A-Za-z0-9_-]{3,64}/?", path):
            return f"https://{host}" + path.rstrip("/") + "/"
        if host in {"www.iesdouyin.com", "www.douyin.com", "m.douyin.com"}:
            match = re.fullmatch(r"/share/video/([0-9]{10,20})/?", path)
            if match:
                return "https://www.douyin.com/video/" + match[1]
    except (ValueError, TypeError):
        pass
    raise DouyinVideoError(
        "请提供抖音视频页面或官方分享短链接；不支持任意网址、本机路径、图集或直播。"
    )


def direct_douyin_links(event) -> list[str]:
    """Read only current direct text, excluding quoted messages and forwards.

    Args:
        event: Current platform event.

    Returns:
        Normalized Douyin links directly supplied by this sender.
    """
    links = []
    for part in event.get_messages():
        if not isinstance(part, Plain):
            continue
        for match in _LINKS.finditer(part.text):
            try:
                url = normalize_douyin_url(match.group().rstrip(").,;!"))
            except DouyinVideoError:
                continue
            if url not in links:
                links.append(url)
    return links


def copy_parser_video(source: Path, cache_dir: Path, destination: Path) -> None:
    """Copy a bounded regular cache file into a private outgoing lease.

    Args:
        source: Path supplied by the verified installed parser instance.
        cache_dir: That instance's configured media cache.
        destination: A new file inside the bridge's temporary directory.

    Raises:
        DouyinVideoError: Missing, linked, escaped or oversized media file.
    """
    try:
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not source.resolve(strict=True).is_relative_to(
                cache_dir.resolve(strict=True)
            )
            or not 0 < info.st_size <= MAX_VIDEO_BYTES
        ):
            raise DouyinVideoError("解析器没有提供可发送的缓存视频，或视频超过 50 MB。")
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            opened = os.fstat(incoming.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise DouyinVideoError("视频缓存发生变化，请稍后再试。")
            copied = 0
            while chunk := incoming.read(64 * 1024):
                copied += len(chunk)
                if copied > MAX_VIDEO_BYTES:
                    raise DouyinVideoError("视频超过 50 MB，尚未发送。")
                outgoing.write(chunk)
            if copied != info.st_size:
                raise DouyinVideoError("视频缓存尚未下载完整。")
    except OSError as exc:
        raise DouyinVideoError("视频缓存无法读取，尚未发送。") from exc


class DouyinVideo:
    """Bind parser reuse and delivery authorization to the actual current event."""

    def __init__(self, context, config: dict, data_dir: Path, video_context):
        self.context = context
        self.config = config
        self.data_dir = data_dir / "douyin_video"
        self.video_context = video_context
        self.lock = asyncio.Lock()
        self.last_started = {}
        self.closed = False

    def close(self) -> None:
        """Prevent an unloaded bridge from sending a pending download."""
        self.closed = True

    def _authorize(self, event) -> None:
        if self.closed or not self.config.get("douyin_video_enabled", False):
            raise PermissionError("抖音视频发送尚未启用。")
        if is_proactive_group(event) or not is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            raise PermissionError("只能在允许的群聊或主人私聊请求抖音视频。")

    def _parser(self, event):
        star = self.context.get_registered_star("astrbot_plugin_parser")
        module_name = getattr(star, "module_path", "")
        module = sys.modules.get(module_name)
        plugin = getattr(star, "star_cls", None)
        if (
            star is None
            or not star.activated
            or getattr(star, "reserved", False)
            or module_name
            not in {
                "data.plugins.astrbot_plugin_parser.main",
                "astrbot_plugin_parser.main",
            }
            or plugin is None
            or type(plugin) is not getattr(module, "ParserPlugin", None)
            or not callable(getattr(plugin, "parse_url", None))
        ):
            raise DouyinVideoError("请先启用“万能解析器”，并等待它加载完成。")
        cfg = plugin.cfg
        if not cfg.parser.douyin.enable or "douyin" not in plugin.parser_map:
            raise DouyinVideoError("请在万能解析器中启用抖音解析。")
        if (
            cfg.whitelist
            and event.unified_msg_origin not in cfg.whitelist
            or event.unified_msg_origin in cfg.blacklist
        ):
            raise PermissionError("当前会话不在万能解析器允许的范围内。")
        return plugin

    @asynccontextmanager
    async def prepare(self, event, url: str):
        """Resolve and validate one exact video without calling any QQ send API.

        Args:
            event: Authorized current platform event; its destination is fixed.
            url: Official Douyin video URL from the user or a real web search.

        Yields:
            A leased local video file, its public title, and its source URL.

        Raises:
            PermissionError: Disabled bridge, parser or unauthorized event.
            DouyinVideoError: Invalid source, busy/cooling down, parse or media error.
        """
        self._authorize(event)
        url = normalize_douyin_url(url)
        direct = direct_douyin_links(event)
        if direct and url not in direct:
            raise DouyinVideoError(
                "用户已指定抖音链接，请原样使用该链接，不能擅自换视频。"
            )
        plugin = self._parser(event)
        _, ffprobe = self.video_context._executables()
        key = (event.get_platform_id(), event.get_group_id() or event.get_sender_id())
        now = time.monotonic()
        if self.lock.locked() or now - self.last_started.get(key, -20) < 20:
            raise DouyinVideoError("正在处理视频或请求过于频繁，请至少等 20 秒再试。")
        async with self.lock:
            self.last_started = {
                k: v for k, v in self.last_started.items() if now - v < 20
            }
            self.last_started[key] = now
            tasks = []
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="send-", dir=self.data_dir
            ) as directory:
                try:
                    async with asyncio.timeout(180):
                        result = await plugin.parse_url(url)
                        data_module = sys.modules.get(type(result).__module__)
                        video_type = getattr(data_module, "VideoContent", None)
                        for content in result.contents:
                            for name in ("path_task", "cover"):
                                task = getattr(content, name, None)
                                if isinstance(task, asyncio.Task):
                                    tasks.append(task)
                        avatar = getattr(result.author, "avatar", None)
                        if isinstance(avatar, asyncio.Task):
                            tasks.append(avatar)
                        videos = [
                            c
                            for c in result.contents
                            if video_type and type(c) is video_type
                        ]
                        if (
                            getattr(result.platform, "name", "") != "douyin"
                            or len(videos) != 1
                        ):
                            raise DouyinVideoError(
                                "这个分享没有可发送的单个视频，可能是图集、直播或失效内容。"
                            )
                        source = Path(await videos[0].get_path())
                        path = Path(directory) / "video.mp4"
                        # This bounded local copy is intentionally synchronous so
                        # cancellation cannot remove the directory under a worker.
                        copy_parser_video(source, Path(plugin.cfg.cache_dir), path)
                        raw = await _run_media(
                            [
                                ffprobe,
                                "-v",
                                "error",
                                "-protocol_whitelist",
                                "file,pipe",
                                "-format_whitelist",
                                "mov,matroska,webm",
                                "-show_entries",
                                "stream=codec_type:format=duration",
                                "-of",
                                "json",
                                str(path),
                            ],
                            15,
                        )
                        info = json.loads(raw)
                        duration = float(info.get("format", {}).get("duration", 0))
                        limit = min(
                            MAX_DURATION_SECONDS,
                            float(plugin.cfg.max_duration) or MAX_DURATION_SECONDS,
                        )
                        if (
                            not math.isfinite(duration)
                            or not 0 < duration <= limit
                            or not any(
                                s.get("codec_type") == "video"
                                for s in info.get("streams", [])
                            )
                        ):
                            raise DouyinVideoError(
                                "下载内容不是有效视频，或超过解析器时长限制（最多 15 分钟）。"
                            )
                        title = " ".join(str(result.title or "抖音视频").split())[:240]
                except (TimeoutError, VideoContextError) as exc:
                    raise DouyinVideoError(
                        "抖音视频下载或校验失败、超时，尚未发送；请稍后重试或换链接。"
                    ) from exc
                except (PermissionError, DouyinVideoError):
                    raise
                except Exception as exc:
                    # Third-party exceptions may embed signed media URLs or cookies.
                    raise DouyinVideoError(
                        "抖音解析或下载失败，可能已失效或需要登录；请在万能解析器里本地配置抖音 Cookies 后重试。"
                    ) from exc
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                self._authorize(event)
                if self._parser(event) is not plugin:
                    raise DouyinVideoError("解析器已重载，本次视频发送已取消。")
                yield path, title, url
