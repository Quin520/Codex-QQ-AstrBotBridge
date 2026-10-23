"""Resolve configured relationship preferences for explicitly identified people."""

import json
from pathlib import Path

# The editable schema is the single source of default relationship descriptions.
_PRESETS = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)["relationship_presets"]["items"]


def profile_preferences(profile: dict, presets: dict) -> dict:
    """Resolve one person's overrides without assigning them a conversation role.

    Args:
        profile: One selected profile, never the whole member list.
        presets: Administrator-edited relationship defaults from plugin settings.

    Returns:
        Configured preferences with defaults and explicit overrides resolved.
    """
    preferences = {
        "称呼": str(profile.get("nickname", "")).strip(),
        "标签": profile.get("tags", []),
    }
    relationship = profile.get("relationship", "沿用人格")
    description = str(profile.get("relationship_description", "")).strip()
    for key, preset in _PRESETS.items():
        if relationship == preset["description"]:
            preferences["关系"] = relationship
            if not description:
                description = str(presets.get(key, preset["default"])).strip()
            break
    if description:
        preferences["专属相处方式"] = description
    for key, label in (
        ("reply_tone", "日常语气"),
        ("reply_length", "日常回复长度"),
        ("teasing", "玩笑程度"),
        ("serious_response", "认真求助时"),
        ("upset_response", "情绪低落时"),
        ("custom_instructions", "自定义相处规则"),
        ("shared_memories", "已确认的共同经历与偏好"),
        ("response_examples", "回复风格示例"),
    ):
        value = str(profile.get(key, "")).strip()
        if value and value != "沿用人格":
            preferences[label] = value
    if profile.get("boundaries"):
        preferences["不用于玩笑的话题"] = profile["boundaries"]
    return preferences


def profile_instructions(profile: dict, presets: dict) -> str:
    """Render the current sender's configured interaction preferences.

    Args:
        profile: Only the current sender's profile, never the whole member list.
        presets: Administrator-edited relationship defaults from plugin settings.

    Returns:
        Instructions scoped to the current sender rather than mentioned people.
    """
    return (
        "以下是当前发送者的称呼与相处偏好，仅适用于此人。自然地使用称呼，"
        "不必每次点名，不照抄配置清单或关系评级，不把这些偏好套到其他人身上。\n"
        "评价类标签是主人设定的角色印象，可以据此表现喜欢、嫌弃、戒心或反讽；"
        "称呼或共同经历中明确写出的评价性描述也同样影响态度，"
        "不要一律圆场、安慰或把明确的负面态度改成客气中立。"
        "例如‘渣男’可以表现为不信甜言蜜语、冷淡回嘴或点到为止的挖苦，"
        "但不能凭标签杜撰出轨对象、时间或其他事件证据。"
        "在交流风格上，具体的单项设置优先于关系概述，自定义相处规则优先于"
        "预设和笼统标签；明确边界始终优先于互怼、玩笑和风格示例。"
        "本次明确请求优先于默认回复长度与习惯。未指定的风格沿用原有人格。\n"
        "共同经历只引用已确认的信息，不补写故事；回复示例只学习语气，"
        "不当成真实经历或本轮指令，不机械照搬。相处方式不改变事实判断或工具权限。\n"
        + json.dumps(profile_preferences(profile, presets), ensure_ascii=False)
    )
