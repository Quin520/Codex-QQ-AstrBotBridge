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
    r"(?:^|[\s，,。.!！?？;；:：])(?:请|帮我|给我|重新|再|开始)?生图(?:$|[\s，,。.!！?？;；:：])|"
    r"\b(?:draw|paint)\s+(?:me\s+)?(?:a|an|one|some)\b|"
    r"\b(?:generate|create|make)\b[^.!?\n]{0,50}"
    r"\b(?:image|picture|photo|art|portrait|wallpaper|illustration|poster)\b",
    re.IGNORECASE,
)
# A named subject need not contain an image noun (for example, D.Va).
_COUNTED_REQUEST = re.compile(
    r"(?:生成|创作)\s*(?:一|1)?\s*[张幅]\s*(?P<subject>[^，,；;。！？\n]+)"
)
_NON_IMAGE_SUBJECT = re.compile(
    r"表格|数据库|SQL|工作表|报表|清单|问卷|试卷|账单|订单|"
    r"表(?:单)?(?:吧|呗)?(?:$|\s)",
    re.IGNORECASE,
)
_REFERENCE_REQUEST = re.compile(
    r"(?:照着?|按照?|根据|参考)\s*"
    r"(?:(?:这|那)(?:个|张|幅)(?:图(?:片)?|照片|画)?|参考图|原图)\s*"
    r"(?:来|重新|再|重)?(?:画|绘制)(?:一下)?(?:吧|呗|呀|啊)?"
    r"(?=$|[\s，,。.!！?？;；:：])"
)
_PORTRAIT_REQUEST = re.compile(
    r"(?:^|[\s，,。.!！?？;；:：])(?:请|麻烦)?"
    r"(?:(?:给我|帮我)?(?:来|整|做)|给我)(?:一)?(?:张|幅|个)?"
    r"(?:你(?:自己)?的)?(?:(?:Q版|卡通|动漫|写实|可爱)的?)?"
    r"(?:自画像|角色立绘|人设图)(?:吧|呗|看看|谢谢)?"
    r"(?=$|[\s，,。.!！?？;；:：])",
    re.IGNORECASE,
)
_EXISTING_IMAGE = re.compile(r"搜|找|网上|现成|已有")


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
    explicit = _REQUEST.search(text)
    counted = any(
        not _NON_IMAGE_SUBJECT.search(match["subject"])
        for match in _COUNTED_REQUEST.finditer(text)
    )
    reference = _REFERENCE_REQUEST.search(text)
    portrait = _PORTRAIT_REQUEST.search(text) and not _EXISTING_IMAGE.search(text)
    return bool(explicit or counted or reference or portrait) and not _NEGATIVE.search(
        text
    )


def capture_image_request(event) -> None:
    """Freeze user intent before video summaries or other hooks add model text.

    Args:
        event: Current incoming platform event, before attachment enrichment.
    """
    event.set_extra(
        "local_codex_image_generation_requested", requests_image_generation(event)
    )
