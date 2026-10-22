"""Local regression checks; these do not send messages to QQ."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.message.components import At, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.filter.command import CommandFilter
from astrbot_plugin_local_codex.main import LocalCodex, is_allowed
from astrbot_plugin_local_codex.runner import CodexRunner, parse_result

THREAD = "e5e2c191-b0fa-4f7d-91e2-3cb04e4e5650"
OTHER_THREAD = "a15bb6d7-ff45-4ea5-9b75-9a24867e56f1"


def event_for(text, sender="123", group="", mention=None):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.group_id = group
    message.session_id = group or sender
    message.self_id = "321"
    message.sender = MessageMember(user_id=sender, nickname="Test sender")
    message.message = ([At(qq=mention)] if mention else []) + [Plain(text)]
    return AstrMessageEvent(
        text.lstrip("/"),
        message,
        PlatformMetadata(name="aiocqhttp", description="test", id="test"),
        group or sender,
    )


def output_for(answer="hello", complete=True):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
    ]
    if complete:
        events.append({"type": "turn.completed"})
    return "\n".join(json.dumps(event) for event in events).encode("utf-8")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=Path(self.directory.name),
        ):
            self.plugin = LocalCodex(
                None,
                {"owner_qq": "123", "group_qq": "456", "codex_exe": sys.executable},
            )
        self.plugin.runner.run = AsyncMock(return_value=(THREAD, "Test answer"))

    def test_allowlist_allows_group_members_but_keeps_private_owner_only(self):
        config = {"owner_qq": "123", "group_qq": "456"}
        self.assertTrue(is_allowed(config, "aiocqhttp", "123", ""))
        self.assertTrue(is_allowed(config, "aiocqhttp", "123", "456"))
        self.assertTrue(is_allowed(config, "aiocqhttp", "999", "456"))
        self.assertFalse(is_allowed(config, "aiocqhttp", "999", ""))
        self.assertFalse(is_allowed(config, "aiocqhttp", "123", "999"))
        self.assertFalse(is_allowed(config, "webchat", "123", "456"))
        self.assertFalse(is_allowed({}, "aiocqhttp", "123", ""))
        self.assertFalse(is_allowed({"owner_qq": "123"}, "aiocqhttp", "123", "456"))

    def test_incomplete_or_failed_turn_is_not_success(self):
        self.assertEqual(parse_result(output_for("你好")), (THREAD, "你好"))
        with self.assertRaises(RuntimeError):
            parse_result(output_for(complete=False))
        with self.assertRaises(RuntimeError):
            parse_result(b'{"type":"turn.failed","error":{"message":"quota"}}')

    async def test_prompt_uses_stdin_and_resume_uses_explicit_id(self):
        login = AsyncMock(returncode=0)
        login.communicate.return_value = (b"", b"Logged in using ChatGPT")
        turn = AsyncMock(returncode=0)
        turn.communicate.return_value = (output_for(), b"")
        prompt = 'hello & echo SHOULD_NOT_RUN | powershell $(whoami) "中文"'
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(Path(sys.executable), Path(directory), 30, "")
            with patch(
                "astrbot_plugin_local_codex.runner.asyncio.create_subprocess_exec",
                side_effect=[login, turn],
            ) as spawn:
                self.assertEqual(await runner.run(prompt, THREAD), (THREAD, "hello"))
            argv = spawn.call_args_list[1].args
            self.assertNotIn(prompt, argv)
            self.assertEqual(argv[-3:], ("resume", THREAD, "-"))
            self.assertNotIn("--last", argv)
            self.assertIn("read-only", argv)
            self.assertIn('service_tier="fast"', argv)
            self.assertIn("features.fast_mode=true", argv)
            for feature in ("shell_tool", "unified_exec", "apps", "plugins"):
                self.assertEqual(argv[argv.index(feature) - 1], "--disable")
            turn.communicate.assert_awaited_once_with(prompt.encode("utf-8"))

    async def test_fast_mode_can_be_disabled_without_loading_personal_config(self):
        login = AsyncMock(returncode=0)
        login.communicate.return_value = (b"Logged in using ChatGPT", b"")
        turn = AsyncMock(returncode=0)
        turn.communicate.return_value = (output_for(), b"")
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(
                Path(sys.executable), Path(directory), 30, "", fast_mode=False
            )
            with patch(
                "astrbot_plugin_local_codex.runner.asyncio.create_subprocess_exec",
                side_effect=[login, turn],
            ) as spawn:
                await runner.run("hello", None)
            argv = spawn.call_args_list[1].args
            self.assertIn('service_tier="default"', argv)
            self.assertNotIn('service_tier="fast"', argv)
            self.assertIn("--ignore-user-config", argv)

    async def test_api_key_login_is_rejected_before_turn(self):
        login = AsyncMock(returncode=0)
        login.communicate.return_value = (b"", b"Logged in using an API key")
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(Path(sys.executable), Path(directory), 30, "")
            with patch(
                "astrbot_plugin_local_codex.runner.asyncio.create_subprocess_exec",
                return_value=login,
            ) as spawn:
                with self.assertRaisesRegex(RuntimeError, "ChatGPT"):
                    await runner.run("hello", None)
                self.assertEqual(spawn.call_count, 1)

    async def test_timeout_stops_owned_process(self):
        login = AsyncMock(returncode=0)
        login.communicate.return_value = (b"", b"Logged in using ChatGPT")
        turn = AsyncMock(returncode=None)
        turn.communicate.side_effect = TimeoutError
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(Path(sys.executable), Path(directory), 30, "")
            with (
                patch(
                    "astrbot_plugin_local_codex.runner.asyncio.create_subprocess_exec",
                    side_effect=[login, turn],
                ),
                patch.object(runner, "stop", new_callable=AsyncMock) as stop,
            ):
                with self.assertRaises(TimeoutError):
                    await runner.run("hello", None)
                self.assertEqual(stop.await_count, 2)
                self.assertIsNone(runner.process)

    async def test_real_astrbot_event_handler_and_conversation_reset(self):
        from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.platform import PlatformMetadata

        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.group_id = "456"
        message.session_id = "456"
        message.self_id = "321"
        message.message = [At(qq="321"), Plain("/codex explain UTF-8")]
        message.sender = MessageMember(user_id="123", nickname="Owner")
        event = AstrMessageEvent(
            "codex explain UTF-8",
            message,
            PlatformMetadata(name="aiocqhttp", description="test", id="test"),
            "456",
        )
        config = {"owner_qq": "123", "group_qq": "456", "codex_exe": sys.executable}
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
                return_value=Path(directory),
            ):
                plugin = LocalCodex(None, config)
            plugin.runner.run = AsyncMock(return_value=(THREAD, "Unicode answer"))
            replies = [result async for result in plugin.codex(event)]
            self.assertEqual(len(replies), 1)
            self.assertTrue(event.is_stopped())
            self.assertFalse(event.call_llm)
            self.assertEqual(list(plugin.sessions.values()), [THREAD])
            reset_event = AstrMessageEvent(
                "codex_reset", message, event.platform_meta, "456"
            )
            replies = [result async for result in plugin.reset(reset_event)]
            self.assertEqual(len(replies), 1)
            self.assertEqual(plugin.sessions, {})
            self.assertEqual(
                json.loads(plugin.session_file.read_text(encoding="utf-8")), {}
            )
            message.sender = MessageMember(user_id="999", nickname="Other")
            other_event = AstrMessageEvent(
                "codex hello", message, event.platform_meta, "456"
            )
            self.assertEqual(
                len([result async for result in plugin.codex(other_event)]), 1
            )
            self.assertEqual(plugin.runner.run.await_count, 2)

    async def test_plain_private_text_continues_the_same_conversation(self):
        for prompt in ["First question", "Follow up"]:
            event = event_for(prompt)
            self.assertEqual(
                len([result async for result in self.plugin.chat(event)]), 1
            )
            self.assertTrue(event.is_stopped())
            self.assertFalse(event.call_llm)
        self.assertEqual(
            self.plugin.runner.run.await_args_list[0].args, ("First question", None)
        )
        self.plugin.runner.run.assert_awaited_with("Follow up", THREAD)

    async def test_group_requires_explicit_bot_mention_and_ignores_self(self):
        for sender, group, mention in [
            ("999", "456", None),
            ("999", "456", "all"),
            ("999", "456", "777"),
            ("999", "987", "321"),
            ("999", "", None),
            ("321", "456", "321"),
        ]:
            event = event_for("Hello", sender, group, mention)
            self.assertEqual([result async for result in self.plugin.chat(event)], [])
            self.assertFalse(event.is_stopped())
        self.plugin.runner.run.assert_not_awaited()
        event = event_for("Hello", "999", "456", "321")
        self.assertEqual(len([result async for result in self.plugin.chat(event)]), 1)
        self.plugin.runner.run.assert_awaited_once_with("Hello", None)

    async def test_new_resets_only_the_sender_in_the_current_chat(self):
        self.plugin.runner.run.side_effect = [
            (THREAD, "First"),
            (OTHER_THREAD, "Second"),
            (OTHER_THREAD, "Private"),
            (THREAD, "New"),
        ]
        for event in [
            event_for("A", "123", "456", "321"),
            event_for("B", "999", "456", "321"),
            event_for("Private"),
        ]:
            _ = [result async for result in self.plugin.chat(event)]
        reset = event_for("/new", "123", "456", "321")
        self.assertEqual(len([result async for result in self.plugin.reset(reset)]), 1)
        self.assertEqual(len(self.plugin.sessions), 2)
        self.assertEqual(
            json.loads(self.plugin.session_file.read_text(encoding="utf-8")),
            self.plugin.sessions,
        )
        event = event_for("Start again", "123", "456", "321")
        _ = [result async for result in self.plugin.chat(event)]
        self.plugin.runner.run.assert_awaited_with("Start again", None)

    async def test_commands_are_not_swallowed_by_plain_chat(self):
        slash_event = event_for("/help")
        bare_event = event_for("help")
        bare_event.set_extra(
            "activated_handlers",
            [SimpleNamespace(event_filters=[CommandFilter("help")])],
        )
        for event in [slash_event, bare_event, event_for("")]:
            self.assertEqual([result async for result in self.plugin.chat(event)], [])
            self.assertFalse(event.is_stopped())
        self.plugin.runner.run.assert_not_awaited()

    async def test_busy_new_does_not_erase_an_in_progress_conversation(self):
        event = event_for("/new")
        key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
        self.plugin.sessions[key] = THREAD
        async with self.plugin.lock:
            self.assertEqual(
                len([result async for result in self.plugin.reset(event)]), 1
            )
        self.assertEqual(self.plugin.sessions[key], THREAD)

    async def test_builtin_new_is_renamed_through_the_command_api_once(self):
        module = "astrbot.builtin_stars.builtin_commands.main"
        command = {
            "handler_full_name": f"{module}_new_conv",
            "effective_command": "new",
            "aliases": [],
        }
        with (
            patch(
                "astrbot_plugin_local_codex.main.list_commands",
                AsyncMock(return_value=[command]),
            ),
            patch(
                "astrbot_plugin_local_codex.main.rename_command", new_callable=AsyncMock
            ) as rename,
        ):
            await self.plugin.configure_new_command(SimpleNamespace(module_path=module))
            rename.assert_awaited_once_with(
                f"{module}_new_conv", "astrbot_new", aliases=[]
            )
            command["effective_command"] = "astrbot_new"
            await self.plugin.configure_new_command(SimpleNamespace(module_path=module))
            self.assertEqual(rename.await_count, 1)
        backup = json.loads(
            (self.plugin.data_dir / "builtin-new-command.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(backup["effective_command"], "new")

    async def test_group_new_passes_real_waking_stage_after_command_migration(self):
        from copy import deepcopy

        from astrbot.builtin_stars.builtin_commands.main import Main
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.db.sqlite import SQLiteDatabase
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.star import command_management
        from astrbot.core.star.star import StarMetadata, star_map
        from astrbot.core.star.star_handler import star_handlers_registry

        builtin_module = Main.__module__
        plugin_module = LocalCodex.__module__
        database = SQLiteDatabase(str(Path(self.directory.name) / "commands.db"))
        await database.initialize()
        try:
            with (
                patch.dict(
                    star_map,
                    {
                        builtin_module: StarMetadata(
                            name="builtin", module_path=builtin_module, reserved=True
                        ),
                        plugin_module: StarMetadata(
                            name="astrbot_plugin_local_codex", module_path=plugin_module
                        ),
                    },
                ),
                patch.object(command_management, "db_helper", database),
            ):
                await self.plugin.configure_new_command(
                    SimpleNamespace(module_path=builtin_module)
                )
                saved = await database.get_command_config(f"{builtin_module}_new_conv")
                self.assertEqual(saved.resolved_command, "astrbot_new")
                handlers = [
                    star_handlers_registry.get_handler_by_full_name(name)
                    for name in [f"{plugin_module}_reset", f"{builtin_module}_new_conv"]
                ]
                config = deepcopy(DEFAULT_CONFIG)
                config["admins_id"] = []
                stage = WakingCheckStage()
                await stage.initialize(
                    SimpleNamespace(
                        astrbot_config=config,
                        db_helper=database,
                        astrbot_config_id="test",
                    )
                )
                stage._umo_auto_name_recorder = SimpleNamespace(
                    schedule=lambda event: None
                )
                event = event_for("/new", "999", "456", "321")
                event.send = AsyncMock()
                with (
                    patch.object(
                        star_handlers_registry,
                        "get_handlers_by_event_type",
                        return_value=handlers,
                    ),
                    patch(
                        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
                        new=AsyncMock(side_effect=lambda event, handlers: handlers),
                    ),
                ):
                    await stage.process(event)
                event.send.assert_not_awaited()
                self.assertFalse(event.is_stopped())
                self.assertEqual(event.get_extra("activated_handlers"), [handlers[0]])
        finally:
            await database.engine.dispose()


if __name__ == "__main__":
    unittest.main()
