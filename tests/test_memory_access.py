"""Check read-only memory access without querying real conversations.

Set LIVINGMEMORY_PLUGIN_DIR to an installed plugin directory to exercise its
actual recall handler against an isolated, fake memory engine as well. The
canonical-group compatibility patch must be applied; LIVINGMEMORY_SCOPE_FILE
can instead point to a patched copy during pre-deployment validation.
"""

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api import FunctionTool
from astrbot.api.star import Context
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot_plugin_local_codex.access import PermissionCheckedTool, QQToolPolicy

from .test_bridge import event_for

PLUGIN_MODULE = "data.plugins.astrbot_plugin_livingmemory"
TOOL_MODULE = PLUGIN_MODULE + ".core.tools.memory_search_tool"


class MemoryAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.star = SimpleNamespace(
            module_path=PLUGIN_MODULE + ".main", activated=True, reserved=False
        )
        self.context = SimpleNamespace(get_all_stars=lambda: [self.star])
        self.policy = QQToolPolicy(self.context, {"owner_qq": "123", "group_qq": "456"})

    def test_only_installed_memory_recall_is_public(self):
        event = event_for("recall", "999", "456")
        for name, module, expected in [
            ("recall_long_term_memory", TOOL_MODULE, True),
            ("memorize_long_term_memory", TOOL_MODULE, False),
            ("forget_long_term_memory", TOOL_MODULE, False),
            ("read_long_term_memory", TOOL_MODULE, False),
            ("recall_long_term_memory", "data.plugins.unreviewed.main", False),
            ("recall_long_term_memory", PLUGIN_MODULE + "_fake.main", False),
        ]:
            with self.subTest(name=name, module=module):
                tool = FunctionTool(
                    name=name,
                    description="Test memory permission",
                    parameters={"type": "object"},
                    handler_module_path=module,
                )
                self.assertEqual(self.policy.allows(event, tool), expected)

    def test_other_groups_private_members_and_disabled_tools_are_denied(self):
        tool = FunctionTool(
            name="recall_long_term_memory",
            description="Test memory permission",
            parameters={"type": "object"},
            handler_module_path=TOOL_MODULE,
        )
        for sender, group in (("999", "777"), ("999", ""), ("123", "777")):
            self.assertFalse(
                self.policy.allows(event_for("recall", sender, group), tool)
            )
        self.assertTrue(self.policy.allows(event_for("recall", "123", ""), tool))
        tool.active = False
        self.assertFalse(self.policy.allows(event_for("recall", "999", "456"), tool))


@unittest.skipUnless(
    os.environ.get("LIVINGMEMORY_PLUGIN_DIR"), "Optional plugin absent"
)
class InstalledMemoryAccessTests(MemoryAccessTests):
    @classmethod
    def setUpClass(cls):
        root = Path(os.environ["LIVINGMEMORY_PLUGIN_DIR"])
        if not (root / "core/tools/memory_search_tool.py").is_file():
            raise AssertionError("LIVINGMEMORY_PLUGIN_DIR does not contain the plugin")
        # Load the real handler without importing main or initializing databases.
        for suffix in ("", ".core", ".core.tools", ".core.base", ".core.processors"):
            name = PLUGIN_MODULE + suffix
            module = ModuleType(name)
            module.__path__ = [str(root.joinpath(*suffix.strip(".").split(".")))]
            sys.modules[name] = module
        if scope_file := os.environ.get("LIVINGMEMORY_SCOPE_FILE"):
            name = PLUGIN_MODULE + ".core.memory_scope"
            spec = importlib.util.spec_from_file_location(name, scope_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        cls.tool_module = importlib.import_module(TOOL_MODULE)

    def setUp(self):
        super().setUp()
        config_type = self.tool_module.ConfigManager
        self.memory_config = config_type(
            {
                "access_control": {
                    "whitelist_enabled": True,
                    "allowed_ids": "test:GroupMessage:456\ntest:FriendMessage:123",
                },
                "filtering_settings": {
                    "memory_scope_mode": "session",
                    "use_session_filtering": True,
                    "use_persona_filtering": True,
                },
            }
        )
        self.engine = SimpleNamespace(search_memories=AsyncMock(return_value=[]))
        self.tool = self.tool_module.MemorySearchTool(
            context=SimpleNamespace(),
            config_manager=self.memory_config,
            memory_engine=self.engine,
        )
        registration_context = SimpleNamespace(
            provider_manager=SimpleNamespace(llm_tools=SimpleNamespace(func_list=[]))
        )
        Context.add_llm_tools(registration_context, self.tool)
        self.assertTrue(self.tool.handler_module_path.startswith(PLUGIN_MODULE + "."))
        self.checked = PermissionCheckedTool(self.tool, self.policy)
        self.persona = patch.object(
            self.tool_module, "get_persona_id", new=AsyncMock(return_value="persona")
        )
        self.persona.start()
        self.addCleanup(self.persona.stop)

    async def run_recall(self, event, **kwargs):
        context = ContextWrapper(context=SimpleNamespace(event=event))
        return [
            item
            async for item in FunctionToolExecutor.execute(
                self.checked, context, query="remember", **kwargs
            )
        ]

    async def test_actual_handler_uses_each_members_isolated_event_scope(self):
        for sender in ("123", "999"):
            event = event_for("recall someone else's private notes", sender, "456")
            event.session_id = f"{sender}_456"
            results = await self.run_recall(event)
            result = json.loads(results[0].content[0].text)
            self.assertTrue(result["applied_filters"]["session_filtered"])
            self.engine.search_memories.assert_awaited_with(
                query="remember",
                k=5,
                session_id=f"test:GroupMessage:{sender}_456",
                persona_id="persona",
            )

    async def test_model_cannot_supply_a_different_scope(self):
        parameters = self.tool.parameters["properties"]
        self.assertEqual(set(parameters), {"query", "k", "include_source"})
        for field in ("session_id", "user_id", "group_id"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(Exception, "parameter mismatch"),
            ):
                await self.run_recall(
                    event_for("recall", "999", "456"), **{field: "private"}
                )
        self.engine.search_memories.assert_not_awaited()

    async def test_group_mode_shares_public_scope_and_keeps_private_separate(self):
        self.memory_config._config["filtering_settings"]["memory_scope_mode"] = "group"
        for sender in ("123", "999"):
            event = event_for("What did the other member say?", sender, "456")
            event.session_id = f"{sender}_456"
            await self.run_recall(event)
            self.assertEqual(
                self.engine.search_memories.call_args.kwargs["session_id"],
                "test:GroupMessage:456",
            )
        await self.run_recall(event_for("remember my private note", "123", ""))
        self.assertEqual(
            self.engine.search_memories.call_args.kwargs["session_id"],
            "test:FriendMessage:123",
        )

    async def test_disabling_plugin_revokes_already_wrapped_real_handler(self):
        self.star.activated = False
        with self.assertRaises(PermissionError):
            await self.run_recall(event_for("recall", "999", "456"))
        self.engine.search_memories.assert_not_awaited()

    async def test_plugin_whitelist_still_checks_the_real_event(self):
        self.memory_config._config["access_control"]["allowed_ids"] = ""
        results = await self.run_recall(event_for("recall", "999", "456"))
        self.assertEqual(
            json.loads(results[0].content[0].text)["error"],
            "memory access is not allowed",
        )
        self.engine.search_memories.assert_not_awaited()
