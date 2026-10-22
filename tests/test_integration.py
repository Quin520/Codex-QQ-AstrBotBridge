"""Regression checks for AstrBot routing, personal profiles, and model tools."""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jsonschema
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot_plugin_local_codex.integration import configure_provider
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.provider import PROVIDER_ID, LocalCodexProvider

from .test_bridge import THREAD, event_for


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.context = SimpleNamespace(
            get_all_stars=lambda: [
                SimpleNamespace(
                    module_path="data.plugins.example.main",
                    reserved=False,
                    activated=True,
                ),
                SimpleNamespace(
                    module_path="data.plugins.disabled.main",
                    reserved=False,
                    activated=False,
                ),
            ],
            conversation_manager=SimpleNamespace(new_conversation=AsyncMock()),
        )
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=self.root,
        ):
            self.plugin = LocalCodex(
                self.context,
                {
                    "owner_qq": "123",
                    "group_qq": "456",
                    "codex_exe": sys.executable,
                    "integrate_plugins": True,
                    "user_profiles": [
                        {"qq": "123", "nickname": "Alpha", "tags": ["brief"]},
                        {"qq": "999", "nickname": "Beta", "tags": ["beginner"]},
                    ],
                },
            )

    async def test_pipeline_keeps_other_handlers_and_emits_no_waiting_reply(self):
        event = event_for("hello", "999", "456", "321")
        self.assertEqual([reply async for reply in self.plugin.chat(event)], [])
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertEqual(event.get_extra("selected_provider"), PROVIDER_ID)

    async def test_group_without_mention_does_not_enable_model(self):
        event = event_for("ordinary group text", "999", "456")
        self.assertEqual([reply async for reply in self.plugin.chat(event)], [])
        self.assertFalse(event.call_llm)
        self.assertFalse(event.is_stopped())

    async def test_only_current_sender_profile_and_plugin_tools_reach_model(self):
        toolset = ToolSet()
        for name, module in [
            ("plugin_tool", "data.plugins.example.tools"),
            ("shell_tool", "astrbot.core.tools.shell"),
            ("external_mcp", None),
            ("disabled_tool", "data.plugins.disabled.tools"),
        ]:
            toolset.add_tool(
                FunctionTool(
                    name=name,
                    description=name,
                    parameters={"type": "object"},
                    handler_module_path=module,
                )
            )
        request = ProviderRequest(system_prompt="Original persona", func_tool=toolset)
        await self.plugin.personalize_request(
            event_for("hi", "999", "456", "321"), request
        )
        self.assertIn("Original persona", request.system_prompt)
        self.assertIn("Beta", request.system_prompt)
        self.assertIn("beginner", request.system_prompt)
        self.assertNotIn("Alpha", request.system_prompt)
        self.assertNotIn("brief", request.system_prompt)
        self.assertEqual(request.func_tool.names(), ["plugin_tool"])

    async def test_profile_hook_blocks_model_requests_outside_allowlist(self):
        event = event_for("hi", "999", "987", "321")
        request = ProviderRequest(system_prompt="Original")
        await self.plugin.personalize_request(event, request)
        self.assertTrue(event.is_stopped())
        self.assertEqual(request.system_prompt, "Original")

    async def test_new_uses_current_astrbot_session_and_keeps_profile(self):
        event = event_for("/new", "999", "456", "321")
        event.session_id = "999_456"
        replies = [reply async for reply in self.plugin.reset(event)]
        self.assertEqual(len(replies), 1)
        self.context.conversation_manager.new_conversation.assert_awaited_once_with(
            event.unified_msg_origin
        )
        self.assertEqual(self.plugin.profiles["999"]["nickname"], "Beta")

    async def test_proactive_group_needs_no_sender_and_never_borrows_a_profile(self):
        event_type = type(event_for(""))
        proactive_type = type(
            "ProactiveMessageEvent",
            (event_type,),
            {
                "__module__": "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event"
            },
        )
        for sender in ("", "999"):
            event = event_for("", sender, "456")
            event.__class__ = proactive_type
            request = ProviderRequest(system_prompt="Group persona")
            await self.plugin.personalize_request(event, request)
            self.assertFalse(event.is_stopped())
            self.assertEqual(request.system_prompt, "Group persona")
        outside = event_for("", "", "987")
        outside.__class__ = proactive_type
        await self.plugin.personalize_request(outside, ProviderRequest())
        self.assertTrue(outside.is_stopped())

    async def test_provider_configuration_preserves_unrelated_settings(self):
        class Config(dict):
            save_config_async = AsyncMock()

        config = Config(deepcopy(DEFAULT_CONFIG))
        config["provider"] = [{"id": "existing", "type": "openai_chat_completion"}]
        manager = SimpleNamespace(
            _mcp_init_task=None, reload=AsyncMock(), set_provider=AsyncMock()
        )
        context = SimpleNamespace(get_config=lambda: config, provider_manager=manager)
        await configure_provider(context, self.plugin.config, self.root)
        self.assertEqual(config["provider"][0]["id"], "existing")
        self.assertEqual(config["provider"][1]["id"], PROVIDER_ID)
        self.assertTrue(config["platform_settings"]["unique_session"])
        self.assertEqual(
            config["agent_runner"]["config"]["model"]["provider_id"], PROVIDER_ID
        )
        manager.reload.assert_not_awaited()
        manager._mcp_init_task = object()
        await configure_provider(context, self.plugin.config, self.root)
        self.assertEqual(len(config["provider"]), 2)
        manager.reload.assert_awaited_once()

    async def test_actual_provider_manager_load_reload_and_unload(self):
        from astrbot.core.provider.manager import ProviderManager

        class Config(dict):
            save_config_async = AsyncMock()

        config = Config(deepcopy(DEFAULT_CONFIG))
        config["provider"] = []
        manager = ProviderManager(
            SimpleNamespace(confs={"default": config}),
            None,
            SimpleNamespace(default_persona=""),
        )
        context = SimpleNamespace(get_config=lambda: config, provider_manager=manager)
        with (
            patch("astrbot.core.provider.manager.astrbot_config", config),
            patch("astrbot.core.provider.manager.sp.put_async", new=AsyncMock()),
            patch(
                "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
                return_value=self.root,
            ),
        ):
            await configure_provider(context, self.plugin.config, self.root)
            self.assertEqual(manager.provider_insts, [])
            await manager.load_provider(config["provider"][0])
            first = manager.inst_map[PROVIDER_ID]
            self.assertIsInstance(first, LocalCodexProvider)
            self.assertIs(manager.curr_provider_inst, first)
            manager._mcp_init_task = object()
            await configure_provider(context, self.plugin.config, self.root)
            self.assertEqual(len(manager.provider_insts), 1)
            self.assertIsNot(manager.inst_map[PROVIDER_ID], first)
            await manager.terminate_provider(PROVIDER_ID)
            self.assertEqual(manager.provider_insts, [])
            self.assertNotIn(PROVIDER_ID, manager.inst_map)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch(
            "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
            return_value=Path(self.directory.name),
        ):
            self.provider = LocalCodexProvider(
                {"codex_exe": sys.executable, "model": "default"}, {}
            )
        self.provider.runner.run = AsyncMock(
            return_value=(THREAD, '{"answer":"Hello","tool_calls":[]}')
        )
        self.tool = FunctionTool(
            name="lookup",
            description="Look up a value",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        )
        self.tools = ToolSet(tools=[self.tool])

    async def test_persona_history_and_plugin_extra_context_are_preserved(self):
        contexts = [{"role": "assistant", "content": "Earlier answer"}]
        answer = await self.provider.text_chat(
            prompt="Follow up",
            system_prompt="Persona",
            contexts=contexts,
            extra_user_content_parts=[TextPart(text="Learned user preferences")],
        )
        self.assertEqual(answer.completion_text, "Hello")
        sent = self.provider.runner.run.await_args.args[0]
        for value in [
            "Persona",
            "Earlier answer",
            "Follow up",
            "Learned user preferences",
        ]:
            self.assertIn(value, sent)
        self.assertEqual(contexts, [{"role": "assistant", "content": "Earlier answer"}])
        self.assertTrue(self.provider.runner.run.await_args.kwargs["ephemeral"])
        self.assertIsNone(self.provider.runner.run.await_args.args[1])

    async def test_validated_tool_call_is_returned_to_astrbot(self):
        payload = {
            "answer": "unwanted progress text",
            "tool_calls": [{"name": "lookup", "arguments_json": '{"key":"sample"}'}],
        }
        self.provider.runner.run.return_value = (THREAD, json.dumps(payload))
        answer = await self.provider.text_chat(
            prompt="Find sample", func_tool=self.tools
        )
        self.assertEqual(answer.role, "tool")
        self.assertEqual(answer.completion_text, "")
        self.assertEqual(answer.tools_call_name, ["lookup"])
        self.assertEqual(answer.tools_call_args, [{"key": "sample"}])
        self.assertEqual(len(answer.tools_call_ids), 1)

    async def test_unlisted_tools_and_invalid_arguments_are_rejected(self):
        for name, arguments in [("invented", "{}"), ("lookup", '{"key":1}')]:
            self.provider.runner.run.return_value = (
                THREAD,
                json.dumps(
                    {
                        "answer": "",
                        "tool_calls": [{"name": name, "arguments_json": arguments}],
                    }
                ),
            )
            with self.assertRaises(jsonschema.ValidationError):
                await self.provider.text_chat(
                    prompt="Find sample", func_tool=self.tools
                )

    async def test_audio_is_not_silently_discarded(self):
        with self.assertRaisesRegex(ValueError, "STT"):
            await self.provider.text_chat(prompt="Listen", audio_urls=["example.wav"])
        self.provider.runner.run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
