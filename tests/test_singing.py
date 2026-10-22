"""Validate bounded cover requests with isolated audio, workers and QQ senders."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Record
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    LocalCodexProvider,
)
from astrbot_plugin_local_codex.singing import SingingService

from .test_bridge import event_for


class SingingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "selected-song.mp3"
        self.source.write_bytes(b"ID3 isolated song fixture")
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "singing_enabled": True,
            "singing_python": sys.executable,
            "singing_model": str(self.directory / "fixture.pth"),
            "singing_index": "",
            "singing_rvc_root": str(self.directory / "rvc"),
            "singing_cooldown_seconds": 300,
        }
        Path(self.config["singing_model"]).write_bytes(b"isolated model placeholder")
        cli = Path(self.config["singing_rvc_root"]) / "infer/cli.py"
        cli.parent.mkdir(parents=True)
        cli.write_text("# isolated fixture\n", encoding="utf-8")
        self.context = SimpleNamespace()
        self.service = SingingService(self.context, self.config, self.directory)
        self.leases = []
        self.released = []
        self.jobs = []

        @asynccontextmanager
        async def song(context, event, search_id, position):
            self.leases.append((context, event, search_id, position))
            try:
                yield self.source, "Fixture song"
            finally:
                self.released.append(search_id)

        async def render(source, directory):
            self.assertEqual(source.read_bytes(), self.source.read_bytes())
            self.assertEqual(self.released, [lease[2] for lease in self.leases])
            self.jobs.append(directory)
            output = directory / "cover.mp3"
            output.write_bytes(b"ID3 isolated converted audio")
            return output

        self.service._render = AsyncMock(side_effect=render)
        self.patch_song = patch(
            "astrbot_plugin_local_codex.singing.prepared_song", side_effect=song
        )
        self.prepare = self.patch_song.start()
        self.addCleanup(self.patch_song.stop)
        self.patch_which = patch(
            "astrbot_plugin_local_codex.singing.shutil.which",
            side_effect=lambda name: str(self.directory / f"{name}.exe"),
        )
        self.patch_which.start()
        self.addCleanup(self.patch_which.stop)
        self.patch_log = patch("astrbot_plugin_local_codex.singing.logger.exception")
        self.log_error = self.patch_log.start()
        self.addCleanup(self.patch_log.stop)

    @staticmethod
    def event(sender="999", group="456"):
        event = event_for("请用你的声音翻唱这首歌", sender, group)
        event.session_id = f"{sender}_{group}" if group else sender
        event.send = AsyncMock(return_value=None)
        return event

    async def test_selected_song_becomes_current_chat_record_and_work_is_cleaned(self):
        event = self.event()
        result = await self.service.send(event, "fixture-search", 1)
        self.assertIn("已发送到当前会话", result)
        self.assertEqual(self.leases, [(self.context, event, "fixture-search", 1)])
        self.assertEqual(self.released, ["fixture-search"])
        self.service._render.assert_awaited_once()
        event.send.assert_awaited_once()
        record = event.send.call_args.args[0].chain[0]
        self.assertIsInstance(record, Record)
        self.assertEqual(record.path, str(self.jobs[0] / "cover.mp3"))
        self.assertIn("Fixture song", record.text)
        self.assertTrue(event.get_extra("local_codex_cover_sent"))
        self.assertEqual(event.get_extra("local_codex_cover_text"), record.text)
        self.assertFalse(self.jobs[0].exists())
        self.assertIsNone(self.service._active_task)

    async def test_disabled_or_missing_model_never_downloads_or_claims_delivery(self):
        self.config["singing_enabled"] = False
        event = self.event()
        self.assertIn("未启用", await self.service.send(event, "fixture-search", 1))
        self.config["singing_enabled"] = True
        self.config["singing_model"] = ""
        result = await self.service.send(event, "fixture-search", 1)
        self.assertIn("音色模型", result)
        self.assertIn("不能", result)
        self.prepare.assert_not_called()
        self.service._render.assert_not_awaited()
        event.send.assert_not_awaited()
        self.assertFalse(event.get_extra("local_codex_cover_sent"))

    async def test_invalid_operator_paths_reject_before_song_download(self):
        invalid = (
            ("singing_python", "relative-python.exe"),
            ("singing_model", str(self.directory / "missing.pth")),
            ("singing_model", str(self.source)),
            ("singing_index", str(self.directory / "missing.index")),
            ("singing_rvc_root", str(self.directory / "missing-rvc")),
        )
        for key, value in invalid:
            with (
                self.subTest(setting=key, value=value),
                patch.dict(self.config, {key: value}),
            ):
                event = self.event()
                result = await self.service.send(event, "fixture-search", 1)
                self.assertTrue(result)
                event.send.assert_not_awaited()
        self.prepare.assert_not_called()
        self.service._render.assert_not_awaited()

    async def test_private_owner_only_target_group_and_platform_are_enforced(self):
        for sender, group in (("999", ""), ("123", "999"), ("", "456")):
            with self.subTest(sender=sender, group=group):
                event = self.event(sender, group)
                with self.assertRaises(PermissionError):
                    await self.service.send(event, "fixture-search", 1)
                event.send.assert_not_awaited()
        event = self.event()
        event.platform_meta.name = "webchat"
        with self.assertRaises(PermissionError):
            await self.service.send(event, "fixture-search", 1)
        self.config["owner_qq"] = ""
        with self.assertRaises(PermissionError):
            await self.service.send(self.event(), "fixture-search", 1)
        self.prepare.assert_not_called()

    async def test_proactive_group_cannot_spend_resources_on_unrequested_covers(self):
        event = self.event()
        event.__class__ = type(
            "ProactiveMessageEvent",
            (type(event),),
            {
                "__module__": (
                    "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event"
                )
            },
        )
        with self.assertRaises(PermissionError):
            await self.service.send(event, "fixture-search", 1)
        self.prepare.assert_not_called()
        event.send.assert_not_awaited()

    async def test_duplicate_event_and_other_group_member_share_cooldown(self):
        event = self.event()
        await self.service.send(event, "fixture-search", 1)
        self.assertIn("已经尝试", await self.service.send(event, "another-search", 2))
        another = self.event("123", "456")
        self.assertIn("稍后", await self.service.send(another, "another-search", 2))
        self.assertEqual(len(self.leases), 1)
        self.service._render.assert_awaited_once()
        another.send.assert_not_awaited()

    async def test_owner_private_and_public_group_have_independent_cooldowns(self):
        for event in (self.event(), self.event("123", "")):
            self.assertIn(
                "已发送到当前会话", await self.service.send(event, "fixture-search", 1)
            )
            event.send.assert_awaited_once()
        self.assertEqual(self.service._render.await_count, 2)

    async def test_cooldown_expires_after_configured_interval(self):
        with patch(
            "astrbot_plugin_local_codex.singing.time.monotonic", return_value=100
        ):
            await self.service.send(self.event(), "fixture-search", 1)
        with patch(
            "astrbot_plugin_local_codex.singing.time.monotonic", return_value=399
        ):
            self.assertIn("稍后", await self.service.send(self.event(), "next", 1))
        with patch(
            "astrbot_plugin_local_codex.singing.time.monotonic", return_value=400
        ):
            self.assertIn("已发送", await self.service.send(self.event(), "next", 1))
        self.assertEqual(self.service._render.await_count, 2)

    async def test_global_busy_state_does_not_queue_another_scope(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        render = self.service._render.side_effect

        async def blocking_render(source, directory):
            entered.set()
            await release.wait()
            return await render(source, directory)

        self.service._render.side_effect = blocking_render
        first = asyncio.create_task(
            self.service.send(self.event(), "fixture-search", 1)
        )
        await entered.wait()
        event = self.event("123", "")
        self.assertIn("另一首", await self.service.send(event, "another-search", 1))
        event.send.assert_not_awaited()
        release.set()
        self.assertIn("已发送", await first)
        self.assertEqual(len(self.leases), 1)

    async def test_render_failure_and_timeout_never_claim_delivery_and_remove_job(self):
        for error in (RuntimeError("private worker secret"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.service._last_sent.clear()
                self.service._render.side_effect = error
                event = self.event()
                result = await self.service.send(event, "fixture-search", 1)
                self.assertIn("尚未发送", result)
                self.log_error.assert_called()
                self.assertNotIn("private worker secret", result)
                self.assertFalse(event.get_extra("local_codex_cover_sent"))
                self.assertIsNone(event.get_extra("local_codex_cover_text"))
                self.assertEqual(list(self.service.data_dir.glob("cover-*")), [])
                self.assertIsNone(self.service._active_task)
                self.assertFalse(self.service._lock.locked())
                event.send.assert_not_awaited()
                self.assertIn("稍后", await self.service.send(self.event(), "retry", 1))

    async def test_send_failure_keeps_attempt_marker_without_success_text_or_retry(
        self,
    ):
        for mode in ("exception", "false", "flag"):
            with self.subTest(mode=mode):
                self.service._last_sent.clear()
                event = self.event()
                if mode == "exception":
                    event.send.side_effect = RuntimeError("private transport secret")
                elif mode == "false":
                    event.send.return_value = False
                else:
                    event.proactive_send_failed = True
                result = await self.service.send(event, "fixture-search", 1)
                self.assertIn("未确认", result)
                self.assertNotIn("private transport secret", result)
                self.assertTrue(event.get_extra("local_codex_cover_sent"))
                self.assertIsNone(event.get_extra("local_codex_cover_text"))
                self.assertIn("已经尝试", await self.service.send(event, "retry", 1))
                event.send.assert_awaited_once()
                self.assertFalse(self.jobs[-1].exists())

    async def test_cancellation_and_unload_release_resources_without_delivery(self):
        entered = asyncio.Event()

        async def blocking_render(source, directory):
            self.jobs.append(directory)
            entered.set()
            await asyncio.Event().wait()

        self.service._render.side_effect = blocking_render
        event = self.event()
        task = asyncio.create_task(self.service.send(event, "fixture-search", 1))
        await entered.wait()
        await self.service.stop()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.released, ["fixture-search"])
        self.assertFalse(self.jobs[0].exists())
        self.assertFalse(self.service._lock.locked())
        self.assertIsNone(self.service._active_task)
        self.assertFalse(event.get_extra("local_codex_cover_sent"))
        event.send.assert_not_awaited()
        self.assertIn("未启用", await self.service.send(self.event(), "next", 1))

    async def test_worker_job_uses_only_admin_paths_and_bounds_audio_options(self):
        self.service.data_dir.mkdir()
        self.config.update(singing_pitch=99, singing_duration_seconds=999)
        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

        async def spawn(*args, **kwargs):
            job_path = Path(args[args.index("--job") + 1])
            self.job = json.loads(job_path.read_text(encoding="utf-8"))
            Path(self.job["output"]).write_bytes(b"ID3 isolated worker output")
            (job_path.parent / "worker.log").write_bytes(b"isolated stage details")
            self.assertEqual(args[0], sys.executable)
            self.assertEqual(Path(args[1]).name, "singing_worker.py")
            self.assertEqual(kwargs["cwd"], self.config["singing_rvc_root"])
            return process

        work = self.directory / "job"
        work.mkdir()
        with patch(
            "astrbot_plugin_local_codex.singing.asyncio.create_subprocess_exec",
            side_effect=spawn,
        ):
            result = await SingingService._render(self.service, self.source, work)
        self.assertEqual(result, work / "cover.mp3")
        self.assertEqual(self.job["pitch"], 12)
        self.assertEqual(self.job["duration"], 120)
        self.assertEqual(self.job["model"], self.config["singing_model"])
        self.assertIsNone(self.service._process)
        self.assertIn(
            b"isolated stage details",
            (self.service.data_dir / "last-worker.log").read_bytes(),
        )

    async def test_worker_success_without_audio_is_rejected_and_process_is_released(
        self,
    ):
        self.service.data_dir.mkdir()
        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
        work = self.directory / "empty-job"
        work.mkdir()
        with (
            patch(
                "astrbot_plugin_local_codex.singing.asyncio.create_subprocess_exec",
                return_value=process,
            ),
            self.assertRaisesRegex(RuntimeError, "MP3"),
        ):
            await SingingService._render(self.service, self.source, work)
        self.assertIsNone(self.service._process)

    async def test_worker_timeout_or_cancellation_stops_owned_process(self):
        self.service.data_dir.mkdir()
        for error in (TimeoutError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                process = SimpleNamespace(
                    returncode=None, wait=AsyncMock(side_effect=error)
                )
                with (
                    patch(
                        "astrbot_plugin_local_codex.singing.asyncio.create_subprocess_exec",
                        return_value=process,
                    ),
                    patch.object(
                        self.service, "_kill_worker", new_callable=AsyncMock
                    ) as kill,
                    self.assertRaises(type(error)),
                ):
                    await SingingService._render(
                        self.service, self.source, self.directory
                    )
                kill.assert_awaited_once()
                self.assertIsNone(self.service._process)


class SingingToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "codex_exe": sys.executable,
            "singing_enabled": True,
            "group_context_enabled": True,
        }
        self.star = SimpleNamespace(
            module_path="astrbot_plugin_local_codex.main",
            activated=True,
            reserved=False,
        )
        self.context = SimpleNamespace(get_all_stars=lambda: [self.star])
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=self.directory,
        ):
            self.plugin = LocalCodex(self.context, self.config)
        self.audio = self.directory / "fixture.mp3"
        self.audio.write_bytes(b"ID3 isolated cover output")

        async def deliver(event, search_id, position):
            text = "角色音色翻唱片段：Fixture song"
            await event.send(
                event.chain_result([Record.fromFileSystem(self.audio, text=text)])
            )
            event.set_extra("local_codex_cover_sent", True)
            event.set_extra("local_codex_cover_text", text)
            return "角色音色翻唱片段已发送到当前会话。"

        self.plugin.singing.send = AsyncMock(side_effect=deliver)

    def tool(self):
        registered = next(
            tool for tool in llm_tools.func_list if tool.name == "sing_cover"
        )
        tool = copy.deepcopy(registered)
        tool.handler = self.plugin.sing_cover
        tool.handler_module_path = "astrbot_plugin_local_codex.main"
        self.assertEqual(set(tool.parameters["properties"]), {"search_id", "position"})
        return PermissionCheckedTool(tool, self.plugin.tool_policy)

    async def execute(self, event, tool=None):
        return [
            value
            async for value in FunctionToolExecutor.execute(
                tool or self.tool(),
                ContextWrapper(context=SimpleNamespace(event=event)),
                search_id="fixture-search",
                position=1,
            )
        ]

    async def test_registered_tool_finishes_turn_and_remembers_successful_cover(self):
        event = SingingServiceTests.event()
        self.assertEqual(await self.execute(event), [None])
        self.plugin.singing.send.assert_awaited_once_with(event, "fixture-search", 1)
        event.send.assert_awaited_once()
        self.assertEqual(len(self.plugin.group_context.rows), 1)
        row = self.plugin.group_context.rows[0]
        self.assertEqual(
            (row["role"], row["qq"], row["reply_to"]), ("assistant", "321", "999")
        )
        self.assertIn("Fixture song", row["text"])
        self.assertIn("已发送翻唱的曲目说明", row["text"])
        self.assertNotIn("已发送语音的文字内容", row["text"])

    async def test_actual_tool_loop_stops_without_a_second_model_reply(self):
        event = SingingServiceTests.event()
        with patch(
            "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
            return_value=self.directory,
        ):
            provider = LocalCodexProvider(
                {"id": PROVIDER_ID, "type": PROVIDER_TYPE, "codex_exe": sys.executable},
                {},
            )
        provider.text_chat = AsyncMock(
            return_value=LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["sing_cover"],
                tools_call_args=[{"search_id": "fixture-search", "position": 1}],
                tools_call_ids=["fixture-cover-call"],
            )
        )
        try:
            agent = ToolLoopAgentRunner()
            await agent.reset(
                provider=provider,
                request=ProviderRequest(
                    prompt="请翻唱这首歌", func_tool=ToolSet(tools=[self.tool()])
                ),
                run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=BaseAgentRunHooks(),
                request_max_retries=1,
            )
            async for _ in agent.step_until_done(max_step=3):
                pass
            self.assertTrue(agent.done())
            provider.text_chat.assert_awaited_once()
            event.send.assert_awaited_once()
        finally:
            await provider.terminate()

    async def test_policy_rechecks_plugin_switch_scope_and_owning_module(self):
        tool = self.tool()
        for event in (
            SingingServiceTests.event("999", ""),
            SingingServiceTests.event("123", "999"),
        ):
            with self.assertRaises(PermissionError):
                await self.execute(event, tool)
        event = SingingServiceTests.event()
        self.config["singing_enabled"] = False
        with self.assertRaises(PermissionError):
            await self.execute(event, tool)
        self.config["singing_enabled"] = True
        self.star.activated = False
        with self.assertRaises(PermissionError):
            await self.execute(event, tool)
        self.star.activated = True
        tool.original.handler_module_path = "unrelated_plugin.main"
        with self.assertRaises(PermissionError):
            await self.execute(event, tool)
        other = SimpleNamespace(
            module_path="meme_manager.main", activated=True, reserved=False
        )
        self.context.get_all_stars = lambda: [self.star, other]
        tool.original.handler_module_path = "meme_manager.main"
        with self.assertRaises(PermissionError):
            await self.execute(event, tool)
        self.plugin.singing.send.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_unconfigured_or_failed_cover_returns_explanation_without_false_memory(
        self,
    ):
        for result in ("尚未配置角色音色模型。", "翻唱已生成，但未确认发送成功。"):
            with self.subTest(result=result):
                self.plugin.singing.send.side_effect = None
                self.plugin.singing.send.return_value = result
                event = SingingServiceTests.event()
                response = await self.execute(event)
                self.assertEqual(len(response), 1)
                self.assertEqual(response[0].content[0].text, result)
                self.assertEqual(self.plugin.group_context.rows, [])
                event.send.assert_not_awaited()
