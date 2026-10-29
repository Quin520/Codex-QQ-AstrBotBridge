"""Verify dialogue configuration, scoped activity cues, and real tool filtering."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot_plugin_local_codex.capabilities import capability_instructions

from . import test_roleplay as fixtures
from .test_group_context import group_event, proactive_event


class ConversationStyleTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RoleplayTests.setUp

    async def test_selected_preset_changes_next_request_without_rewriting_persona(self):
        for preset, expected in [
            ("自然群友", "平等相处"),
            ("角色感更鲜明", "喜好"),
            ("简短克制", "一句说清"),
        ]:
            self.config["natural_chat_preset"] = preset
            request = ProviderRequest(
                system_prompt="Original personality.", prompt="original request"
            )
            await self.plugin.personalize_request(group_event("hi", "999"), request)
            self.assertTrue(request.system_prompt.startswith("Original personality."))
            self.assertEqual(request.prompt, "original request")
            if preset == "角色感更鲜明":
                self.assertIn("偏好、脾气和立场更鲜明", request.system_prompt)
            else:
                self.assertIn(expected, request.system_prompt)

    async def test_narration_setting_can_follow_persona_without_forced_action_rule(
        self,
    ):
        self.config["action_narration"] = "沿用人格"
        request = ProviderRequest()
        await self.plugin.personalize_request(group_event("hi", "999"), request)
        self.assertNotIn("动作描写：", request.system_prompt)
        self.config["action_narration"] = "尽量不用"
        request = ProviderRequest()
        await self.plugin.personalize_request(group_event("hi", "999"), request)
        self.assertIn("省去括号动作", request.system_prompt)

    async def test_disabled_voice_is_not_advertised_even_when_tool_was_offered(self):
        module = "data.plugins.astrbot_plugin_local_codex.main"
        self.plugin.context.get_all_stars = lambda: [
            SimpleNamespace(module_path=module, activated=True, reserved=False)
        ]
        self.config["voice_enabled"] = False
        request = ProviderRequest(
            func_tool=ToolSet(
                tools=[
                    FunctionTool(
                        name="send_voice",
                        description="voice",
                        parameters={"type": "object", "properties": {}},
                        handler_module_path=module,
                    )
                ]
            )
        )
        await self.plugin.personalize_request(group_event("hello", "999"), request)
        self.assertEqual(list(request.func_tool), [])
        self.assertNotIn("send_voice", request.system_prompt)

    def test_guidance_only_describes_available_media_and_keeps_voice_distinct_from_song(
        self,
    ):
        text = capability_instructions({"voice_use_rvc": True}, {"send_voice"})
        self.assertIn("RVC", text)
        self.assertIn("朗读，不是唱歌", text)
        for absent in ("find_cover_music", "generate_image", "send_douyin_video"):
            self.assertNotIn(absent, text)
        self.assertEqual(capability_instructions({}, set()), "")

    def test_activity_excludes_current_message_private_other_group_and_parsed_material(
        self,
    ):
        memory = self.plugin.group_context
        self.config["group_context_enabled"] = True
        now = time.time()

        def row(identifier, role="user", qq="123", age=60, **extra):
            return {
                "id": identifier,
                "scope": "test:GroupMessage:456",
                "time": now - age,
                "role": role,
                "qq": qq,
                "name": "fixture",
                "text": "fixture text",
                **extra,
            }

        memory.rows = [
            row("prior", age=120),
            row("current", qq="999", age=0),
            row("answer", "assistant", "321", 90, reply_to="999"),
            row("link", qq="777", age=1, source_kind="parsed_link"),
            row("video", qq="888", age=1, source_kind="video_summary"),
            row("other", age=0, scope="other:GroupMessage:456"),
            row("private", age=0, scope="test:FriendMessage:123"),
        ]
        event = group_event("current", "999")
        event.set_extra("local_codex_group_row", "current")
        with patch(
            "astrbot_plugin_local_codex.group_context.time.time", return_value=now
        ):
            state = memory.activity_snapshot(event)
            self.assertEqual(state["近五分钟发言人数"], 1)
            self.assertEqual(state["距上一条群员发言分钟"], 2)
            self.assertEqual(state["距我上次回复当前成员分钟"], 1.5)
            self.assertIsNone(
                memory.activity_snapshot(proactive_event())["距我上次回复当前成员分钟"]
            )
        self.assertEqual(memory.activity_snapshot(group_event("private", group="")), {})

    def test_activity_does_not_claim_old_expired_or_future_messages_are_recent(self):
        self.config["group_context_enabled"] = True
        self.config["group_context_max_minutes"] = 5
        now = time.time()
        self.plugin.group_context.rows = [
            {
                "id": str(age),
                "scope": "test:GroupMessage:456",
                "time": now - age,
                "role": "user",
                "qq": "123",
                "name": "fixture",
                "text": "old",
            }
            for age in (301, -60)
        ]
        with patch(
            "astrbot_plugin_local_codex.group_context.time.time", return_value=now
        ):
            state = self.plugin.group_context.activity_snapshot(group_event("hi"))
        self.assertEqual(state["近五分钟发言人数"], 0)
        self.assertIsNone(state["距上一条群员发言分钟"])
