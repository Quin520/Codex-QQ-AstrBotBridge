"""Validate image tool dispatch and terminal delivery without running CLI or QQ."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Image
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    LocalCodexProvider,
)

from .test_bridge import event_for


class ImageToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.image = self.directory / "fixture.png"
        self.image.write_bytes(b"isolated fixture; no real media upload")
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "codex_exe": sys.executable,
            "image_generation_enabled": True,
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

    @staticmethod
    def event(sender="999", group="456"):
        event = event_for("画一只猫", sender, group, "321" if group else None)
        event.send = AsyncMock(return_value=None)
        return event

    def tool(self):
        registered = next(
            tool for tool in llm_tools.func_list if tool.name == "generate_image"
        )
        tool = copy.deepcopy(registered)
        tool.handler = self.plugin.generate_image
        tool.handler_module_path = "astrbot_plugin_local_codex.main"
        self.assertEqual(set(tool.parameters["properties"]), {"prompt"})
        return PermissionCheckedTool(tool, self.plugin.tool_policy)

    async def execute(self, event, tool=None):
        return [
            result
            async for result in FunctionToolExecutor.execute(
                tool or self.tool(),
                ContextWrapper(context=SimpleNamespace(event=event)),
                prompt="A small cartoon cat",
            )
        ]

    async def test_registered_handler_delivers_current_event_and_returns_terminal_none(
        self,
    ):
        event = self.event()
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)
        self.assertEqual(await self.execute(event), [None])
        self.plugin.image_generator.generate.assert_awaited_once_with(
            event, "A small cartoon cat"
        )
        event.send.assert_awaited_once()
        component = event.send.call_args.args[0].chain[0]
        self.assertIsInstance(component, Image)
        self.assertEqual(component.path, str(self.image))

    async def test_actual_agent_loop_finishes_without_second_llm_or_text_ack(self):
        event = self.event()
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)
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
                tools_call_name=["generate_image"],
                tools_call_args=[{"prompt": "A small cartoon cat"}],
                tools_call_ids=["fixture-image-call"],
            )
        )
        try:
            agent = ToolLoopAgentRunner()
            await agent.reset(
                provider=provider,
                request=ProviderRequest(
                    prompt="画一只猫", func_tool=ToolSet(tools=[self.tool()])
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
            self.assertEqual(len(event.send.call_args.args[0].chain), 1)
        finally:
            await provider.terminate()

    async def test_disabled_feature_and_unauthorized_direct_calls_never_run_worker(
        self,
    ):
        self.plugin.image_generator.runner.run = AsyncMock()
        self.config["image_generation_enabled"] = False
        event = self.event()
        with self.assertRaises(PermissionError):
            await self.plugin.generate_image(event, "cat")
        self.config["image_generation_enabled"] = True
        for sender, group in (("999", ""), ("123", "789")):
            event = self.event(sender, group)
            with self.assertRaises(PermissionError):
                await self.plugin.generate_image(event, "cat")
            event.send.assert_not_awaited()
        self.plugin.image_generator.runner.run.assert_not_awaited()

    async def test_wrapped_tool_rechecks_plugin_disable_and_destination(self):
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)
        tool = self.tool()
        for event in (self.event("999", ""), self.event("123", "789")):
            with self.assertRaises(PermissionError):
                await self.execute(event, tool)
            event.send.assert_not_awaited()
        self.star.activated = False
        event = self.event()
        with self.assertRaises(PermissionError):
            await self.execute(event, tool)
        self.plugin.image_generator.generate.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_failed_false_or_unconfirmed_send_never_returns_terminal_success(
        self,
    ):
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)
        for mode in ("exception", "false", "flag"):
            with self.subTest(mode=mode):
                event = self.event()
                if mode == "exception":
                    event.send.side_effect = RuntimeError("fixture delivery failure")
                elif mode == "false":
                    event.send.return_value = False
                else:
                    event.proactive_send_failed = True
                with self.assertRaises(RuntimeError):
                    await self.execute(event)
                event.send.assert_awaited_once()

    async def test_generation_failure_does_not_send(self):
        self.plugin.image_generator.generate = AsyncMock(
            side_effect=RuntimeError("fixture generation failed")
        )
        event = self.event()
        with self.assertRaisesRegex(RuntimeError, "fixture generation failed"):
            await self.execute(event)
        event.send.assert_not_awaited()

    async def test_group_proactive_event_does_not_generate_unrequested_images(self):
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)
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
        self.assertIn("明确要求", await self.plugin.generate_image(event, "cat"))
        self.plugin.image_generator.generate.assert_not_awaited()
        event.send.assert_not_awaited()
