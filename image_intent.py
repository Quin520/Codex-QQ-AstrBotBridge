"""Require a current, explicit request before exposing paid image generation."""

import re

from astrbot.core.message.components import Plain

_NEGATIVE = re.compile(
    r"(?:不要|别|不用|不需要|无需|禁止|不许|不想|不是要)[^，,；;。！？\n]{0,16}"
    r"(?:生图|生成|画|绘制|创作)|"
    r"(?:do\s+not|don't|never|without|no)\s+(?:\w+\s+){0,3}"
    r"(?:generat\w*|draw\w*|paint\w*|creat\w*)",
    re.IGNORECASE,
)
_REQUEST = re.compile(
    r"(?:画|绘制)(?:一|个|只|张|幅|些|几|两|三|出|一下|一个|点)|"
    r"(?:帮我|给我|请你|请|麻烦你|自己)(?:画|绘制)|"
    r"(?:生成|创作)[^。！？\n]{0,40}(?:图|画|照片|壁纸|头像|海报)|"
    r"(?:^|[\s，。！：])(?:请|帮我|给我|重新|再|开始)?生图(?:$|[\s，。！：])|"
    r"\b(?:draw|paint)\s+(?:me\s+)?(?:a|an|one|some)\b|"
    r"\b(?:generate|create|make)\b[^.!?\n]{0,50}"
    r"\b(?:image|picture|photo|art|portrait|wallpaper|illustration|poster)\b",
    re.IGNORECASE,
)


def requests_image_generation(event) -> bool:
    """Inspect only this event's direct text, excluding quotes and old context.

    Ambiguous follow-ups remain search/text requests. The user can explicitly
    say '画一张…' to authorize generation again; a model-supplied prompt cannot.

    Args:
        event: Current platform event, optionally captured before media hooks.

    Returns:
        Whether its direct user text explicitly requests image generation.
    """
    captured = event.get_extra("local_codex_image_generation_requested")
    if isinstance(captured, bool):
        return captured
    text = "\n".join(
        item.text for item in event.get_messages() if isinstance(item, Plain)
    )
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", text)
    return bool(_REQUEST.search(text)) and not _NEGATIVE.search(text)


def capture_image_request(event) -> None:
    """Freeze user intent before video summaries or other hooks add model text.

    Args:
        event: Current incoming platform event, before attachment enrichment.
    """
    event.set_extra(
        "local_codex_image_generation_requested", requests_image_generation(event)
    )
