"""Enforce QQ tool permissions using adapter identity, never message text."""

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import call_local_llm_tool

from .image_intent import requests_image_generation
from .plugin_guards import guard_public_arguments

PUBLIC_PLUGIN_TOOLS = {
    "astrbot_plugin_local_codex": {
        "send_voice",
        "generate_image",
        "search_web_image",
        "send_douyin_video",
        "sing_cover",
        "find_cover_music",
    },
    "astrbot_plugin_listen_music": {
        "find_music", "deliver_music", "search_music",
        "find_in_bilibili", "deliver_media",
    },
    "astrbot_plugin_bili_player": {"find_in_bilibili", "deliver_media"},
    "astrbot_plugin_livingmemory": {"recall_long_term_memory"},
    "astrbot_plugin_music": {"play_song_by_name", "query_lyrics_by_name"},
    "astrbot_plugin_stealer": {"search_meme", "send_meme"},
    "astrbot_plugin_pokepro": {"llm_poke_user"},
    "astrbot_plugin_qq_group_daily_analysis": {"group_daily_analysis_get_report"},
    "meme_manager": {"search_memes"},
}


def is_allowed(config: dict, platform: str, sender: str, group: str) -> bool:
    """Allow the owner's private chat or members of the configured QQ group."""
    owner = str(config.get("owner_qq", "")).strip()
    target_group = str(config.get("group_qq", "")).strip()
    return (
        platform == "aiocqhttp"
        and bool(owner)
        and bool(sender)
        and (
            (not group and sender == owner)
            or bool(target_group)
            and group == target_group
        )
    )


class QQToolPolicy:
    """Keep only reviewed public tools, with other plugin tools owner-only."""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config

    def plugin_name(self, tool: FunctionTool) -> str:
        """Resolve a tool's owner from the active installed plugin registry."""
        module = tool.handler_module_path or ""
        for star in self.context.get_all_stars():
            if not star.activated or star.reserved or not star.module_path:
                continue
            root = star.module_path.rsplit(".", 1)[0]
            if module == root or module.startswith(root + "."):
                return root.rsplit(".", 1)[-1]
        return ""

    def allows(self, event: AstrMessageEvent, tool: FunctionTool) -> bool:
        """Check the real sender and the installed plugin owning the tool.

        Args:
            event: Current adapter event, including the actual sender and group.
            tool: Concrete tool about to be exposed or executed.

        Returns:
            Whether the tool belongs to this caller's permitted capabilities.
        """
        if event.get_platform_name() != "aiocqhttp" or not tool.active:
            return False
        owner = str(self.config.get("owner_qq", "")).strip()
        sender = str(event.get_sender_id())
        group = str(event.get_group_id())
        target = str(self.config.get("group_qq", "")).strip()
        owner_private = bool(owner) and sender == owner and not group
        if not owner or not (owner_private or bool(target) and group == target):
            return False
        plugin = self.plugin_name(tool)
        if not plugin:
            return False
        if plugin == "astrbot_plugin_local_codex":
            switch = {
                "send_voice": "voice_enabled",
                "generate_image": "image_generation_enabled",
                "search_web_image": "image_search_enabled",
                "send_douyin_video": "douyin_video_enabled",
                "sing_cover": "singing_enabled",
                "find_cover_music": "singing_enabled",
            }.get(tool.name)
            if switch and not self.config.get(switch, False):
                return False
            if tool.name == "generate_image" and not requests_image_generation(event):
                return False
        return owner_private or tool.name in PUBLIC_PLUGIN_TOOLS.get(plugin, set())


class PermissionCheckedTool(FunctionTool):
    """Recheck identity at execution, including after tool hooks have run."""

    def __init__(self, original: FunctionTool, policy: QQToolPolicy):
        super().__init__(
            name=original.name,
            description=original.description,
            parameters=original.parameters,
            handler_module_path=original.handler_module_path,
        )
        self.original = original
        self.policy = policy

    async def call(self, context, **kwargs):
        """Run the original tool only after the execution context is authorized.

        Args:
            context: AstrBot's execution context, carrying the current QQ event.
            kwargs: Model-selected arguments for the original tool.

        Yields:
            Original tool output, retaining AstrBot's normal result handling.

        Raises:
            PermissionError: The caller lacks permission or the plugin was disabled.
        """
        event = context.context.event
        if not self.policy.allows(event, self.original):
            raise PermissionError("This QQ sender is not allowed to use this tool.")
        if event.get_group_id():
            kwargs = await guard_public_arguments(
                event,
                self.policy.plugin_name(self.original),
                self.original.name,
                kwargs,
            )
            # Membership verification yields to the event loop; a disabled
            # plugin must not execute after that asynchronous check completes.
            if not self.policy.allows(event, self.original):
                raise PermissionError("This plugin tool is no longer authorized.")
        if self.original.handler:
            handler, method = self.original.handler, "decorator_handler"
        elif type(self.original).call is not FunctionTool.call:
            handler, method = self.original.call, "call"
        else:
            handler, method = self.original.run, "run"
        async for result in call_local_llm_tool(context, handler, method, **kwargs):
            yield result
