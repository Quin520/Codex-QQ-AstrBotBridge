"""Bind public plugin actions to verified members of the current QQ group."""

import asyncio

from astrbot.api.event import AstrMessageEvent


async def guard_public_arguments(
    event: AstrMessageEvent, plugin_name: str, tool_name: str, kwargs: dict
) -> dict:
    """Constrain reviewed public tools immediately before their execution.

    Args:
        event: Real platform event already authorized by the bridge policy.
        plugin_name: Installed plugin identifier established from its metadata.
        tool_name: Concrete tool name owned by that plugin.
        kwargs: Arguments proposed by the model.

    Returns:
        A copy with the public tool's arguments bound to the current group.
        Unrelated tools retain their original arguments.

    Raises:
        PermissionError: A request crosses groups or membership is unverified.
        ValueError: A poke target or repetition count is invalid.
        TypeError: A poke repetition count has an invalid type.
    """
    report = (
        plugin_name == "astrbot_plugin_qq_group_daily_analysis"
        and tool_name == "group_daily_analysis_get_report"
    )
    poke = plugin_name == "astrbot_plugin_pokepro" and tool_name == "llm_poke_user"
    if not report and not poke:
        return kwargs

    group = str(event.get_group_id() or "").strip()
    if (
        event.get_platform_name() != "aiocqhttp"
        or not group.isascii()
        or not group.isdigit()
        or int(group) <= 0
    ):
        raise PermissionError("This public tool requires the current QQ group.")
    checked = dict(kwargs)
    if report:
        requested = str(checked.get("group") or "").strip()
        if requested and requested != group:
            raise PermissionError("Group members can only query this group's reports.")
        checked["group"] = group
        return checked

    target = str(checked.get("user_id") or "").strip()
    if not target.isascii() or not target.isdigit() or int(target) <= 0:
        raise ValueError("Poke target must be a positive numeric QQ identifier.")
    if int(target) == int(event.get_self_id()):
        raise PermissionError("The bot cannot poke itself with this tool.")
    count = checked.get("times", 1)
    if isinstance(count, bool):
        raise TypeError("Poke count must be a number.")
    try:
        count = max(1, min(5, int(count)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Poke count must be a finite number.") from exc
    try:
        member = await asyncio.wait_for(
            event.bot.get_group_member_info(group_id=int(group), user_id=int(target)),
            timeout=10,
        )
    except Exception as exc:
        raise PermissionError("Could not verify this group's poke target.") from exc
    if (
        not isinstance(member, dict)
        or str(member.get("user_id", "")) != str(int(target))
        or str(member.get("group_id", group)) != group
    ):
        raise PermissionError("Poke target is not a verified member of this group.")
    checked["user_id"] = str(int(target))
    checked["times"] = count
    return checked
