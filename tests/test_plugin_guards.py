"""Verify public plugin argument guards without accessing QQ."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot_plugin_local_codex.plugin_guards import guard_public_arguments

from .test_bridge import event_for

POKE = "astrbot_plugin_pokepro"
DAILY = "astrbot_plugin_qq_group_daily_analysis"


class PluginGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = event_for("Please use the plugin", "999", "456", "321")
        self.event.bot = SimpleNamespace(
            get_group_member_info=AsyncMock(
                return_value={"group_id": 456, "user_id": 888, "nickname": "Member"}
            ),
            group_poke=AsyncMock(),
        )

    async def test_report_uses_current_group_and_preserves_request(self):
        args = {"date_range": "昨天", "report_section": "话题", "group": ""}
        actual = await guard_public_arguments(
            self.event, DAILY, "group_daily_analysis_get_report", args
        )
        self.assertEqual(actual, {**args, "group": "456"})
        self.assertEqual(args["group"], "")
        self.assertIsNot(actual, args)

    async def test_report_rejects_cross_group_and_name_search(self):
        for group in ("789", "Other group", "test:GroupMessage:789"):
            with self.subTest(group=group), self.assertRaises(PermissionError):
                await guard_public_arguments(
                    self.event,
                    DAILY,
                    "group_daily_analysis_get_report",
                    {"group": group},
                )

    async def test_unrelated_and_owner_only_tools_are_not_rewritten(self):
        for plugin, tool in (
            (DAILY, "group_daily_analysis_trigger"),
            ("unreviewed", "llm_poke_user"),
            (POKE, "other_tool"),
        ):
            args = {"group": "789"}
            result = await guard_public_arguments(self.event, plugin, tool, args)
            self.assertIs(result, args)

    async def test_public_tools_reject_private_or_other_platform_event(self):
        private = event_for("action", "999", "")
        with self.assertRaises(PermissionError):
            await guard_public_arguments(
                private, POKE, "llm_poke_user", {"user_id": "888"}
            )
        self.event.platform_meta.name = "webchat"
        with self.assertRaises(PermissionError):
            await guard_public_arguments(
                self.event, DAILY, "group_daily_analysis_get_report", {}
            )

    async def test_poke_validates_membership_and_clamps_repetitions(self):
        for requested, expected in ((999, 5), (0, 1), ("2", 2)):
            actual = await guard_public_arguments(
                self.event,
                POKE,
                "llm_poke_user",
                {"user_id": "888", "times": requested},
            )
            self.assertEqual(actual, {"user_id": "888", "times": expected})
        self.event.bot.get_group_member_info.assert_awaited_with(
            group_id=456, user_id=888
        )
        self.event.bot.group_poke.assert_not_awaited()

    async def test_poke_rejects_invalid_and_self_targets_before_api(self):
        for target in ("321", "nickname", "-1", "0", "", "８８８"):
            with (
                self.subTest(target=target),
                self.assertRaises((ValueError, PermissionError)),
            ):
                await guard_public_arguments(
                    self.event, POKE, "llm_poke_user", {"user_id": target}
                )
        self.event.bot.get_group_member_info.assert_not_awaited()

    async def test_failed_or_mismatching_member_lookup_cannot_send(self):
        for value in (None, {}, {"user_id": 777}, {"user_id": 888, "group_id": 789}):
            self.event.bot.get_group_member_info.return_value = value
            with self.subTest(value=value), self.assertRaises(PermissionError):
                await guard_public_arguments(
                    self.event, POKE, "llm_poke_user", {"user_id": "888"}
                )
        self.event.bot.get_group_member_info.side_effect = RuntimeError("Unavailable")
        with self.assertRaises(PermissionError):
            await guard_public_arguments(
                self.event, POKE, "llm_poke_user", {"user_id": "888"}
            )
        self.event.bot.group_poke.assert_not_awaited()

    async def test_invalid_count_fails_before_member_lookup(self):
        for count in (True, None, "many", float("nan"), float("inf")):
            with self.subTest(count=count), self.assertRaises((TypeError, ValueError)):
                await guard_public_arguments(
                    self.event,
                    POKE,
                    "llm_poke_user",
                    {"user_id": "888", "times": count},
                )
        self.event.bot.get_group_member_info.assert_not_awaited()

    async def test_guard_runs_with_real_astrbot_tool_executor_and_fake_qq_api(self):
        event = self.event

        class GuardedPoke(FunctionTool):
            async def call(self, context, **kwargs):
                args = await guard_public_arguments(
                    context.context.event, POKE, self.name, kwargs
                )
                for _ in range(args["times"]):
                    await context.context.event.bot.group_poke(
                        group_id=int(event.get_group_id()), user_id=int(args["user_id"])
                    )
                return "Poke delivered"

        tool = GuardedPoke(
            name="llm_poke_user", description="Guard test", parameters={}
        )
        context = ContextWrapper(context=SimpleNamespace(event=event))
        results = [
            result
            async for result in FunctionToolExecutor.execute(
                tool, context, user_id="888", times=2
            )
        ]
        self.assertEqual(results[0].content[0].text, "Poke delivered")
        self.assertEqual(event.bot.group_poke.await_count, 2)
        event.bot.group_poke.assert_awaited_with(group_id=456, user_id=888)
        event.bot.group_poke.reset_mock()
        event.bot.get_group_member_info.return_value = {"user_id": 777}
        with self.assertRaises(PermissionError):
            async for _ in FunctionToolExecutor.execute(tool, context, user_id="888"):
                pass
        event.bot.group_poke.assert_not_awaited()
