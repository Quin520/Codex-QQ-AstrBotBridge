"""Exercise profile persistence and sender isolation without sending QQ messages."""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import ProviderRequest
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot_plugin_local_codex.main import LocalCodex

from .test_bridge import THREAD, event_for


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.config_path = self.root / "settings.json"
        self.context = SimpleNamespace(
            conversation_manager=SimpleNamespace(new_conversation=AsyncMock())
        )
        self.config = AstrBotConfig(str(self.config_path), schema=self.schema)
        self.config.update(
            {
                "owner_qq": "123",
                "group_qq": "456",
                "codex_exe": sys.executable,
                "integrate_plugins": True,
                "user_profiles": [
                    {
                        "__template_key": "person",
                        "qq": "123",
                        "nickname": "Alpha",
                        "tags": ["brief"],
                    },
                    {
                        "__template_key": "person",
                        "qq": "999",
                        "nickname": "Beta",
                        "tags": ["beginner"],
                    },
                ],
            }
        )

    def plugin(self, config):
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=self.root,
        ):
            return LocalCodex(self.context, config)

    def preferences(self, plugin, sender="999"):
        instructions = plugin._profile_instructions(event_for("hi", sender, "456"))
        return json.loads(instructions.splitlines()[-1])

    def test_old_profiles_survive_schema_upgrade_without_assigned_relationships(self):
        legacy = {
            key: deepcopy(self.config[key])
            for key in ("owner_qq", "group_qq", "codex_exe", "user_profiles")
        }
        self.config_path.write_text(json.dumps(legacy), encoding="utf-8")
        upgraded = AstrBotConfig(str(self.config_path), schema=self.schema)
        for key, value in legacy.items():
            self.assertEqual(upgraded[key], value)
        self.assertEqual(
            self.preferences(self.plugin(upgraded)),
            {"称呼": "Beta", "标签": ["beginner"]},
        )
        self.assertEqual(len(upgraded["relationship_presets"]), 4)
        reloaded = AstrBotConfig(str(self.config_path), schema=self.schema)
        self.assertEqual(reloaded["user_profiles"], legacy["user_profiles"])

    async def test_saved_options_and_custom_text_reach_only_the_current_sender(self):
        from astrbot.dashboard.services.config_service import ConfigFileService

        profile = self.config["user_profiles"][1]
        profile.update(
            {
                "relationship": "朋友",
                "reply_tone": "自然口语，少套话，不固定反问",
                "reply_length": "简短，通常 1–3 句",
                "teasing": "允许轻度互怼，认真时停止",
                "serious_response": "先给结论，再按需解释",
                "upset_response": "先倾听，不急着给建议",
                "boundaries": ["收入与工作", "家庭"],
                "custom_instructions": "不催睡觉；认真时不调侃。",
                "shared_memories": "对方说正在学吉他。",
                "response_examples": "对方：终于下班了。回复：可算放你出来了。",
            }
        )
        self.config["relationship_presets"]["friend"] = "平等相处，可以聊音乐。"
        self.config["reasoning_effort"] = "high"
        reload_plugin = AsyncMock()
        service = ConfigFileService(
            SimpleNamespace(plugin_manager=SimpleNamespace(reload=reload_plugin))
        )
        with patch.object(
            service,
            "get_plugin_metadata_by_name",
            return_value=SimpleNamespace(config=self.config),
        ):
            await service.save_plugin_configs_from_dashboard_payload(
                deepcopy(dict(self.config)), plugin_name="astrbot_plugin_local_codex"
            )
        reload_plugin.assert_awaited_once_with("astrbot_plugin_local_codex")
        reloaded = AstrBotConfig(str(self.config_path), schema=self.schema)
        self.assertEqual(reloaded["user_profiles"][1], profile)
        self.assertEqual(reloaded["reasoning_effort"], "high")
        plugin = self.plugin(reloaded)
        request = ProviderRequest(system_prompt="Original persona")
        await plugin.personalize_request(event_for("hi", "999", "456"), request)
        self.assertTrue(request.system_prompt.startswith("Original persona\n"))
        preferences = self.preferences(plugin)
        self.assertEqual(preferences["专属相处方式"], "平等相处，可以聊音乐。")
        for key in (
            "reply_tone",
            "reply_length",
            "teasing",
            "serious_response",
            "upset_response",
            "custom_instructions",
            "shared_memories",
            "response_examples",
        ):
            self.assertIn(profile[key], preferences.values())
        self.assertEqual(preferences["不用于玩笑的话题"], ["收入与工作", "家庭"])
        other_request = ProviderRequest(system_prompt="Original persona")
        await plugin.personalize_request(event_for("hi", "123", "456"), other_request)
        for private_text in (
            "Beta",
            "平等相处，可以聊音乐。",
            "吉他",
            "可算放你出来了",
        ):
            self.assertIn(private_text, request.system_prompt)
            self.assertNotIn(private_text, other_request.system_prompt)
        self.assertEqual(
            plugin._profile_instructions(event_for("hi", "777", "456")), ""
        )

    def test_all_four_relationships_use_their_editable_presets(self):
        for label, key in (
            ("亲密", "intimate"),
            ("朋友", "friend"),
            ("讨厌", "disliked"),
            ("不熟悉", "unfamiliar"),
        ):
            with self.subTest(relationship=label):
                self.config["user_profiles"][1]["relationship"] = label
                default_text = self.preferences(self.plugin(self.config))[
                    "专属相处方式"
                ]
                self.assertTrue(default_text)
                self.config["relationship_presets"][key] = "管理员改写：" + label
                self.config.save_config()
                reloaded = AstrBotConfig(str(self.config_path), schema=self.schema)
                preferences = self.preferences(self.plugin(reloaded))
                self.assertEqual(preferences["关系"], label)
                self.assertEqual(preferences["专属相处方式"], "管理员改写：" + label)

    def test_personal_description_overrides_preset_and_blank_restores_inheritance(self):
        profile = self.config["user_profiles"][1]
        profile.update(
            {"relationship": "朋友", "relationship_description": "只聊技术。"}
        )
        self.config["relationship_presets"]["friend"] = "一起聊音乐。"
        self.config.save_config()
        reloaded = AstrBotConfig(str(self.config_path), schema=self.schema)
        preferences = self.preferences(self.plugin(reloaded))
        self.assertEqual(preferences["专属相处方式"], "只聊技术。")
        self.assertNotIn("一起聊音乐。", preferences.values())
        reloaded["relationship_presets"]["friend"] = "一起聊游戏。"
        self.assertEqual(
            self.preferences(self.plugin(reloaded))["专属相处方式"], "只聊技术。"
        )
        reloaded["user_profiles"][1]["relationship_description"] = "  "
        self.assertEqual(
            self.preferences(self.plugin(reloaded))["专属相处方式"], "一起聊游戏。"
        )

    def test_unselected_options_and_unused_presets_are_not_injected(self):
        items = self.schema["user_profiles"]["templates"]["person"]["items"]
        profile = self.config["user_profiles"][1]
        for key, metadata in items.items():
            profile.setdefault(key, deepcopy(metadata["default"]))
        self.config["relationship_presets"]["disliked"] = "Unused relationship text"
        self.assertEqual(
            self.preferences(self.plugin(self.config)),
            {"称呼": "Beta", "标签": ["beginner"]},
        )

    async def test_custom_relationship_reaches_direct_mode_and_survives_new(self):
        self.config["integrate_plugins"] = False
        profile = self.config["user_profiles"][1]
        profile.update(
            {"relationship": "自定义", "relationship_description": "像棋友一样交流。"}
        )
        plugin = self.plugin(self.config)
        plugin.runner.run = AsyncMock(return_value=(THREAD, "A reply"))
        event = event_for("hello", "999", "456", "321")
        replies = [reply async for reply in plugin.chat(event)]
        self.assertEqual(len(replies), 1)
        prompt = plugin.runner.run.await_args.args[0]
        self.assertIn("像棋友一样交流。", prompt)
        self.assertNotIn("Alpha", prompt)
        before = deepcopy(self.config["user_profiles"])
        replies = [
            reply
            async for reply in plugin.reset(event_for("/new", "999", "456", "321"))
        ]
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.config["user_profiles"], before)
        self.assertEqual(self.preferences(plugin)["专属相处方式"], "像棋友一样交流。")

    async def test_proactive_group_does_not_inherit_a_members_relationship_or_memory(
        self,
    ):
        self.config["user_profiles"][1].update(
            {"relationship": "亲密", "shared_memories": "Only this member's memory."}
        )
        event = event_for("hi", "999", "456")
        event.__class__ = type(
            "ProactiveMessageEvent",
            (type(event),),
            {
                "__module__": "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event"
            },
        )
        request = ProviderRequest(system_prompt="Group persona")
        await self.plugin(self.config).personalize_request(event, request)
        self.assertFalse(event.is_stopped())
        self.assertEqual(request.system_prompt, "Group persona")


if __name__ == "__main__":
    unittest.main()
