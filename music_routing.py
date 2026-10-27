"""Let explicit music requests reach the model before link previews are sent."""

import re
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.core.message.components import At, Plain

from .douyin_video import DouyinVideoError, normalize_douyin_url

_URL = re.compile(r"https?://[^\s<>\"'，。！？；（）【】]+", re.IGNORECASE)
_REQUEST = re.compile(
    r"翻唱(?:一下|一遍|一首|这|《)|(?:想听|我要听)"
    r"(?:一下|一首|首|这首|这段|你唱|你翻唱|歌|歌曲|音乐|《|生日快乐)|"
    r"(?:点歌|放一首|来一首|唱一首|唱首|唱一下|给我唱|帮我唱|"
    r"帮我翻唱|给我翻唱|请翻唱)|"
    r"用.{0,24}(?:音色|声音).{0,12}(?:翻唱|唱)|(?:^|\s)翻唱\s+\S|"
    r"^(?:请|帮我|给我)?播放(?:一下|一首|这|《|\s|$)"
)
_SHARE_TITLE = re.compile(
    r"(?:【[^\r\n?？]{0,200}(?:哔哩哔哩|bilibili)[^\r\n?？]{0,30}】|"
    r"[^\r\n?？]{0,200}(?:哔哩哔哩|bilibili))",
    re.IGNORECASE,
)
_PARSER_MODULES = {
    "data.plugins.astrbot_plugin_parser.main",
    "astrbot_plugin_parser.main",
}


class MusicRouting:
    """Keep at most 128 recent music intents scoped to one actual sender."""

    def __init__(self) -> None:
        self._pending: OrderedDict[tuple[str, ...], float] = OrderedDict()

    def route(self, event: AstrMessageEvent, *, douyin_video: bool = False) -> bool:
        """Remove only this event's link preview handler for music requests.

        The caller must first apply the bridge's owner/group authorization.
        This method additionally requires a real current-message bot mention
        in groups. It neither fetches URLs nor reads quoted/forwarded content.

        Args:
            event: An authorized event handled in AstrBot integration mode.
            douyin_video: Route direct Douyin links through the enabled video tool.

        Returns:
            Whether this is a music link request, even if no parser is active.
        """
        group = str(event.get_group_id() or "")
        chain = event.get_messages()
        if event.get_sender_id() == event.get_self_id() or (
            group
            and (
                event.get_message_type() != MessageType.GROUP_MESSAGE
                or not any(
                    isinstance(part, At) and str(part.qq) == str(event.get_self_id())
                    for part in chain
                )
            )
        ):
            return False
        if not group and event.get_message_type() != MessageType.FRIEND_MESSAGE:
            return False
        text = " ".join(part.text for part in chain if isinstance(part, Plain)).strip()
        now = time.monotonic()
        for expired in [
            key for key, stamp in self._pending.items() if now - stamp > 300
        ]:
            self._pending.pop(expired)
        key = (
            event.get_platform_name(),
            event.get_platform_id(),
            event.get_self_id(),
            group,
            event.get_sender_id(),
        )
        explicit = bool(_REQUEST.search(_URL.sub("", text).strip()))
        if re.search(r"(?:不要|别|不用|无需).{0,8}(?:翻唱|唱|播放|点歌)", text):
            explicit = False
        links = []
        douyin_link_request = False
        for candidate in _URL.finditer(text):
            try:
                url = urlsplit(candidate.group().rstrip(").,;!"))
            except ValueError:
                continue
            if url.username or url.password or url.netloc.lower() != url.hostname:
                continue
            if (
                url.hostname == "b23.tv"
                and re.fullmatch(r"/[A-Za-z0-9]{1,64}/?", url.path)
            ) or (
                url.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
                and re.fullmatch(r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]+)/?", url.path)
            ):
                links.append(candidate)
            elif douyin_video:
                try:
                    normalize_douyin_url(candidate.group().rstrip(").,;!"))
                except DouyinVideoError:
                    continue
                links.append(candidate)
                douyin_link_request = True
        remainder = text
        for match in reversed(links):
            remainder = remainder[: match.start()] + remainder[match.end() :]
        remainder = remainder.strip(" \t\r\n，。:：;；()（）")
        followup = key in self._pending and (
            not remainder or bool(_SHARE_TITLE.fullmatch(remainder))
        )
        if explicit:
            self._pending[key] = now
            self._pending.move_to_end(key)
            while len(self._pending) > 128:
                self._pending.popitem(last=False)
        elif text and not followup:
            self._pending.pop(key, None)
        if not links or not (explicit or followup or douyin_link_request):
            return False
        handlers = event.get_extra("activated_handlers", [])
        if isinstance(handlers, list):
            # StarRequestSubStage is iterating this exact list. Slice assignment
            # removes only later preview handlers without skipping neighbours.
            handlers[:] = [
                handler
                for handler in handlers
                if not (
                    getattr(handler, "handler_module_path", "") in _PARSER_MODULES
                    and getattr(handler, "handler_name", "") == "on_message"
                )
            ]
        return True
