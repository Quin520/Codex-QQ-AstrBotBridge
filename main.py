"""Bridge private chat and allowed QQ group mentions to the local Codex CLI."""

import asyncio
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import At, Plain
from astrbot.core.star.command_management import list_commands, rename_command
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star import StarMetadata

from .runner import CodexRunner


def is_allowed(config: dict, platform: str, sender: str, group: str) -> bool:
    """Allow the owner's private chat or members of the configured group.

    Args:
        config: Plugin configuration.
        platform: AstrBot platform adapter name.
        sender: Sender ID reported by the QQ adapter.
        group: QQ group ID, or an empty string for private messages.

    Returns:
        True only for the owner's private chat or the configured group.
    """
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


class LocalCodex(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_local_codex")
        self.session_file = self.data_dir / "sessions.json"
        self.sessions = (
            json.loads(self.session_file.read_text(encoding="utf-8"))
            if self.session_file.exists()
            else {}
        )
        self.lock = asyncio.Lock()
        self.runner = CodexRunner(
            Path(str(config.get("codex_exe", ""))).expanduser(),
            self.data_dir,
            max(30, min(600, int(config.get("timeout_seconds", 180)))),
            str(config.get("model", "")).strip(),
        )

    @filter.on_plugin_loaded()
    async def configure_new_command(self, plugin: StarMetadata) -> None:
        """Reserve /new using AstrBot's command configuration API.

        Args:
            plugin: Metadata for the plugin that just finished loading.
        """
        builtin_module = "astrbot.builtin_stars.builtin_commands.main"
        if plugin.module_path not in {__name__, builtin_module}:
            return
        for command in await list_commands():
            if command["handler_full_name"] != f"{builtin_module}_new_conv":
                continue
            if (
                command["effective_command"] != "new"
                and "new" not in command["aliases"]
            ):
                continue
            backup = self.data_dir / "builtin-new-command.json"
            if not backup.exists():
                backup.write_text(
                    json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            await rename_command(
                command["handler_full_name"],
                "astrbot_new",
                aliases=[alias for alias in command["aliases"] if alias != "new"],
            )
            logger.info("Local Codex reserved /new; AstrBot's command is /astrbot_new")

    def _accepts_event(self, event: AstrMessageEvent) -> bool:
        """Require an allowed sender and an explicit bot mention in groups.

        Args:
            event: Incoming message event.

        Returns:
            Whether this message is addressed to the Codex bridge.
        """
        return (
            event.get_sender_id() != event.get_self_id()
            and is_allowed(
                self.config,
                event.get_platform_name(),
                event.get_sender_id(),
                event.get_group_id(),
            )
            and (
                not event.get_group_id()
                or any(
                    isinstance(component, At)
                    and str(component.qq) == str(event.get_self_id())
                    for component in event.get_messages()
                )
            )
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE,
        priority=50,
    )
    async def chat(self, event: AstrMessageEvent):
        """Answer ordinary text while leaving registered commands to their handlers.

        Args:
            event: Incoming private message or group mention.
        """
        if not self._accepts_event(event):
            return
        prompt = "".join(
            component.text
            for component in event.get_messages()
            if isinstance(component, Plain)
        ).strip()
        if not prompt or prompt.startswith("/"):
            return
        if any(
            isinstance(handler_filter, CommandFilter)
            for handler in event.get_extra("activated_handlers", [])
            for handler_filter in handler.event_filters
        ):
            return
        async for result in self._ask(event, prompt):
            yield result

    @filter.command("codex", priority=100)
    async def codex(self, event: AstrMessageEvent):
        """Ask the local Codex CLI: /codex followed by a question.

        Args:
            event: Incoming command event.
        """
        if not self._accepts_event(event):
            return
        parts = event.get_message_str().strip().split(maxsplit=1)
        if len(parts) != 2:
            event.should_call_llm(False)
            yield event.plain_result("直接发文字即可聊天；群里请 @我。新对话：/new")
            event.stop_event()
            return
        async for result in self._ask(event, parts[1]):
            yield result

    async def _ask(self, event: AstrMessageEvent, prompt: str):
        """Run a turn in the sender's conversation and persist its thread ID.

        Args:
            event: An already authorized incoming message.
            prompt: Text to send to Codex.
        """
        event.should_call_llm(False)
        try:
            if self.lock.locked():
                yield event.plain_result(
                    "Codex 正在处理上一条请求，请等它完成后再发送。"
                )
                return
            async with self.lock:
                key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
                yield event.plain_result("稍等，我想一下。")
                try:
                    thread_id, answer = await self.runner.run(
                        prompt, self.sessions.get(key)
                    )
                    self.sessions[key] = thread_id
                    temporary = self.session_file.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps(self.sessions, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    temporary.replace(self.session_file)
                except TimeoutError:
                    yield event.plain_result(
                        "Codex 请求超时，已停止本次进程。可以稍后重试。"
                    )
                    return
                except (OSError, RuntimeError, ValueError) as error:
                    logger.exception("Local Codex request failed")
                    yield event.plain_result(
                        f"Codex 调用失败：{error}\n请查看 AstrBot 日志及插件数据目录；未返回成功结果。"
                    )
                    return
                for start in range(0, len(answer), 1500):
                    yield event.plain_result(answer[start : start + 1500])
        finally:
            event.stop_event()

    @filter.command("new", alias={"codex_reset"}, priority=100)
    async def reset(self, event: AstrMessageEvent):
        """Start a new Codex conversation on the next question.

        Args:
            event: Incoming reset command event.
        """
        if not self._accepts_event(event):
            return
        event.should_call_llm(False)
        try:
            if self.lock.locked():
                yield event.plain_result("请等当前 Codex 请求完成后再重置对话。")
                return
            key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
            self.sessions.pop(key, None)
            temporary = self.session_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.sessions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.session_file)
            yield event.plain_result("已为你开启新对话。直接发文字即可，群里请 @我。")
        finally:
            event.stop_event()

    async def terminate(self):
        """Stop the owned Codex process when this plugin is unloaded."""
        await self.runner.stop()
