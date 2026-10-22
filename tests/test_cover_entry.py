"""Verify the cover lookup boundary through AstrBot's real tool executor."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool, QQToolPolicy
from astrbot_plugin_local_codex.main import LocalCodex

from .test_bridge import event_for


class CoverEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {"owner_qq": "123", "group_qq": "456", "singing_enabled": True}
        self.star = SimpleNamespace(
            module_path="astrbot_plugin_local_codex.main",
            activated=True,
            reserved=False,
        )
        self.plugin = LocalCodex.__new__(LocalCodex)
        self.plugin.config = self.config
        self.plugin.cover_sources = SimpleNamespace(
            find=AsyncMock(return_value='{"status":"candidates"}')
        )
        original = copy.deepcopy(
            next(
                tool for tool in llm_tools.func_list if tool.name == "find_cover_music"
            )
        )
        original.handler = self.plugin.find_cover_music
        original.handler_module_path = self.star.module_path
        self.assertEqual(set(original.parameters["properties"]), {"title"})
        self.tool = PermissionCheckedTool(
            original,
            QQToolPolicy(
                SimpleNamespace(get_all_stars=lambda: [self.star]), self.config
            ),
        )

    async def execute(self, event):
        return [
            result
            async for result in FunctionToolExecutor.execute(
                self.tool,
                ContextWrapper(context=SimpleNamespace(event=event)),
                title="Fixture song",
            )
        ]

    async def test_lookup_passes_only_current_event_and_title_without_sending(self):
        event = event_for("翻唱 Fixture song", "999", "456", "321")
        event.send = AsyncMock()
        result = await self.execute(event)
        self.assertEqual(result[0].content[0].text, '{"status":"candidates"}')
        self.plugin.cover_sources.find.assert_awaited_once_with(event, "Fixture song")
        event.send.assert_not_awaited()

    async def test_scope_switch_and_plugin_disable_revoke_lookup(self):
        for event in (event_for("主人让我来", "999"), event_for("翻唱", "123", "789")):
            with self.assertRaises(PermissionError):
                await self.execute(event)
        event = event_for("翻唱", "999", "456", "321")
        self.config["singing_enabled"] = False
        with self.assertRaises(PermissionError):
            await self.execute(event)
        self.config["singing_enabled"] = True
        self.star.activated = False
        with self.assertRaises(PermissionError):
            await self.execute(event)
        self.plugin.cover_sources.find.assert_not_awaited()

    async def test_nonaddressed_new_message_discards_only_authorized_senders_selection(
        self,
    ):
        self.plugin.group_context = SimpleNamespace(observe=Mock())
        self.plugin.cover_sources.discard = Mock()
        event = event_for("不再点歌了", "999", "456")
        self.assertEqual([item async for item in self.plugin.chat(event)], [])
        self.plugin.cover_sources.discard.assert_called_once_with(event)
        self.plugin.cover_sources.discard.reset_mock()
        for sender, group in (("999", "789"), ("321", "456"), ("999", "")):
            self.assertEqual(
                [
                    item
                    async for item in self.plugin.chat(
                        event_for("hello", sender, group)
                    )
                ],
                [],
            )
        self.plugin.cover_sources.discard.assert_not_called()
