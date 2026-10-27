"""Find existing public pictures, validate bytes, and lease one outgoing file."""

import asyncio
import io
import json
import tempfile
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin

import aiohttp
from PIL import Image

from .access import is_allowed
from .group_context import is_proactive_group
from .video_context import PublicVideoResolver, VideoContextError, validate_video_url

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20 * 1024 * 1024
MAX_SEARCH_BYTES = 2 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class ImageSearchError(RuntimeError):
    """A bounded public explanation without private addresses or host paths."""


@dataclass(frozen=True)
class ImageCandidate:
    title: str
    image_url: str
    page_url: str


def public_url(url: str) -> str:
    """Reuse the bridge's public-only URL policy, including literal IP checks.

    Args:
        url: Search result or redirect address; never a local file reference.

    Returns:
        The validated HTTP(S) URL; DNS is checked separately at connection time.

    Raises:
        ImageSearchError: The URL is malformed or points to a forbidden target.
    """
    try:
        if not isinstance(url, str) or len(url) > 4096:
            raise ValueError
        return validate_video_url(url)
    except (ValueError, VideoContextError):
        raise ImageSearchError("图片来源地址不支持，已跳过。") from None


def candidate(title: str, image_url: str, page_url: str) -> ImageCandidate:
    """Accept only bounded, publicly addressable source metadata."""
    title = " ".join(str(title).split())[:160] or "网络图片"
    return ImageCandidate(title, public_url(image_url), public_url(page_url))


class BingImages(HTMLParser):
    """Read image cards as data; never execute search-page scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[ImageCandidate] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        data = dict(attrs)
        if tag != "a" or "iusc" not in (data.get("class") or "").split():
            return
        try:
            item = json.loads(data.get("m", ""))
            result = candidate(item.get("t", "网络图片"), item["murl"], item["purl"])
        except (ValueError, KeyError, TypeError, AttributeError, ImageSearchError):
            return  # Invalid individual search cards are not usable images.
        if len(self.results) < 8 and result.image_url not in {
            entry.image_url for entry in self.results
        }:
            self.results.append(result)


async def fetch_public(session, url: str, limit: int, *, image: bool = False) -> bytes:
    """Bound transfers and validate every redirect; DNS is pinned by the session.

    Args:
        session: Cookie-free session using PublicVideoResolver and no proxy.
        url: A public search endpoint or candidate image address.
        limit: Maximum response bytes, including chunked responses.
        image: Whether the response must also declare an image content type.

    Returns:
        Bounded response bytes for parsing or independent image validation.

    Raises:
        ImageSearchError: Unsafe redirect, bad status, type or response size.
        aiohttp.ClientError: A connection or protocol failure.
    """
    for _ in range(4):
        public_url(url)
        async with session.get(url, allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                if not location:
                    raise ImageSearchError("图片来源返回了无效跳转。")
                url = urljoin(str(response.url), location)
                continue
            if response.status != 200:
                raise ImageSearchError(f"图片来源暂不可用（HTTP {response.status}）。")
            if image and not response.headers.get(
                "Content-Type", ""
            ).lower().startswith("image/"):
                raise ImageSearchError("图片来源没有返回图片文件。")
            if response.content_length and response.content_length > limit:
                raise ImageSearchError("图片或搜索结果超过大小限制。")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ImageSearchError("图片或搜索结果超过大小限制。")
            if not data:
                raise ImageSearchError("图片来源返回了空内容。")
            return bytes(data)
    raise ImageSearchError("图片来源跳转次数过多。")


async def search_bing(session, query: str) -> list[ImageCandidate]:
    url = "https://www.bing.com/images/async?" + urlencode(
        {"q": query, "adlt": "strict", "first": "0", "count": "10"}
    )
    data = await fetch_public(session, url, MAX_SEARCH_BYTES)
    parser = BingImages()
    parser.feed(data.decode("utf-8", errors="replace"))
    return parser.results


async def search_commons(session, query: str) -> list[ImageCandidate]:
    """Use MediaWiki's documented search/imageinfo API as an independent fallback."""
    url = "https://commons.wikimedia.org/w/api.php?" + urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": "5",
            "prop": "imageinfo",
            "iiprop": "url|mime",
            "iiurlwidth": "1280",
        }
    )
    data = json.loads(await fetch_public(session, url, MAX_SEARCH_BYTES))
    if not isinstance(data, dict) or "error" in data:
        raise ImageSearchError("备用图片搜索暂不可用。")
    results = []
    query_result = data.get("query", {})
    if not isinstance(query_result, dict):
        raise ImageSearchError("备用图片搜索返回格式异常。")
    pages = query_result.get("pages", {})
    if not isinstance(pages, dict) or not all(
        isinstance(page, dict) for page in pages.values()
    ):
        raise ImageSearchError("备用图片搜索返回格式异常。")
    for page in sorted(pages.values(), key=lambda item: item.get("index", 999)):
        try:
            info = page["imageinfo"][0]
            results.append(
                candidate(
                    page["title"].removeprefix("File:"),
                    info.get("thumburl") or info["url"],
                    info["descriptionurl"],
                )
            )
        except (KeyError, IndexError, TypeError, ImageSearchError):
            continue
    return results


def save_verified_image(data: bytes, directory: Path) -> Path:
    """Validate format, dimensions and bytes before handing anything to OneBot.

    Args:
        data: Bounded, downloaded image bytes; no model-provided local path.
        directory: A fresh bridge-owned temporary directory.

    Returns:
        The validated image file, with its original bytes and detected suffix.

    Raises:
        ImageSearchError: Invalid, unsupported or oversized image content.
    """
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ImageSearchError("图片超过大小限制或内容为空。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as decoded:
                suffix = {
                    "PNG": ".png",
                    "JPEG": ".jpg",
                    "GIF": ".gif",
                    "WEBP": ".webp",
                }.get(decoded.format)
                if (
                    suffix is None
                    or not 0 < decoded.width * decoded.height <= MAX_IMAGE_PIXELS
                ):
                    raise ImageSearchError("图片格式或尺寸不支持。")
                decoded.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ImageSearchError("图片内容损坏或尺寸过大。") from exc
    path = directory / ("picture" + suffix)
    path.write_bytes(data)
    return path


class ImageSearch:
    """An authorized request can send one verified result to its current chat."""

    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.data_dir = data_dir / "image_search"
        self.lock = asyncio.Lock()
        self.last_started = {}
        self.closed = False

    def close(self) -> None:
        """Revoke in-flight results before an unloading plugin can send them."""
        self.closed = True

    def _authorize(self, event, query: str) -> None:
        if self.closed or not self.config.get("image_search_enabled", False):
            raise PermissionError("网络找图尚未启用。")
        if is_proactive_group(event) or not is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            raise PermissionError("只有允许的群聊或主人私聊可以请求网络找图。")
        if not isinstance(query, str) or not query.strip() or len(query) > 120:
            raise ImageSearchError("请用 1～120 个字符描述要找的图片。")

    async def _prepare(
        self, session, query: str, directory: Path
    ) -> tuple[Path, ImageCandidate]:
        failures = []
        for search in (search_bing, search_commons):
            try:
                results = await search(session, query)
                if not results:
                    failures.append("搜索源未返回可用图片")
            except (
                aiohttp.ClientError,
                OSError,
                TimeoutError,
                ValueError,
                ImageSearchError,
            ):
                failures.append("搜索源暂不可用")
                continue
            for result in results[:3]:
                try:
                    data = await fetch_public(
                        session, result.image_url, MAX_IMAGE_BYTES, image=True
                    )
                    path = await asyncio.to_thread(save_verified_image, data, directory)
                    return path, result
                except (
                    aiohttp.ClientError,
                    OSError,
                    TimeoutError,
                    ValueError,
                    ImageSearchError,
                ):
                    failures.append("候选图片无法下载或未通过校验")
        reason = "；".join(dict.fromkeys(failures)) or "没有找到匹配图片"
        raise ImageSearchError(
            f"未能取得可发送的现成图片：{reason}。请换个关键词；不会自动改为生图。"
        )

    @asynccontextmanager
    async def prepare(self, event, query: str):
        """Keep the temporary image alive until its real send has completed.

        Args:
            event: Real current QQ event, used for authorization and cooldown.
            query: A bounded image subject without private conversation context.

        Yields:
            A verified local image path and the search engine's source metadata.

        Raises:
            PermissionError: Disabled service or unauthorized destination.
            ImageSearchError: Cooldown, download, validation or search failure.
        """
        self._authorize(event, query)
        key = (event.get_platform_id(), event.get_group_id() or event.get_sender_id())
        now = time.monotonic()
        if self.lock.locked():
            raise ImageSearchError("正在找另一张图片，请稍后再试。")
        if now - self.last_started.get(key, -15) < 15:
            raise ImageSearchError("找图间隔为 15 秒，请稍后再试。")
        async with self.lock:
            self.last_started = {
                k: v for k, v in self.last_started.items() if now - v < 15
            }
            self.last_started[key] = now
            self.data_dir.mkdir(parents=True, exist_ok=True)
            connector = aiohttp.TCPConnector(
                resolver=PublicVideoResolver(), use_dns_cache=False, force_close=True
            )
            with tempfile.TemporaryDirectory(
                prefix="search-", dir=self.data_dir
            ) as temporary:
                async with aiohttp.ClientSession(
                    connector=connector,
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                    timeout=aiohttp.ClientTimeout(total=15, connect=6, sock_read=8),
                    auto_decompress=False,
                ) as session:
                    try:
                        async with asyncio.timeout(55):
                            result = await self._prepare(
                                session, query.strip(), Path(temporary)
                            )
                    except TimeoutError as exc:
                        raise ImageSearchError(
                            "图片搜索或下载超时，尚未发送图片；不会自动改为生图。"
                        ) from exc
                self._authorize(event, query)
                yield result
