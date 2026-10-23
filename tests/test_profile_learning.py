"""Exercise evidence, administrator review, persistence and event-loop boundaries."""

import asyncio
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
from astrbot.core.message.components import At
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.profile_reviews import (
    ProfileReviewStore,
    normalized_profile,
)

from .test_bridge import event_for


class ProfileLearningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.config = AstrBotConfig(str(self.root / "config.json"), schema=schema)
        self.config.update(
            owner_qq="123",
            group_qq="456",
            codex_exe=sys.executable,
            integrate_plugins=True,
            profile_learning_enabled=True,
            user_profiles=[
                {"qq": "123", "nickname": "Alpha"},
                {
                    "qq": "999",
                    "nickname": "Beta",
                    "tags": ["music"],
                    "relationship": "朋友",
                },
            ],
        )
        self.config.save_config()
        self.learning_meta = SimpleNamespace(activated=True, star_cls=object())
        context = SimpleNamespace(get_registered_star=lambda name: self.learning_meta)
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=self.root,
        ):
            self.bridge = LocalCodex(context, self.config)
        self.bridge.review_loop = asyncio.get_event_loop_policy().get_event_loop()
        self.store = self.bridge.profile_reviews
        self.evidence = [{"id": "event1", "text": "以后回复简短一点", "timestamp": 1.0}]

    async def asyncTearDown(self):
        await self.bridge.profile_learner.close()

    def change(self, field="reply_length", value="简短，通常 1–3 句"):
        return {
            "field": field,
            "value": value,
            "reason": "Explicit preference",
            "confidence": 0.95,
            "evidence_ids": [item["id"] for item in self.evidence],
        }

    def add(self, changes=None):
        config = self.store.current_config()
        return self.store.add(
            "999",
            normalized_profile(config["user_profiles"][1]),
            self.store.scope(config),
            changes or [self.change()],
            self.evidence,
        )

    def row(self):
        return self.store.list()[0]["id"]

    def test_approval_edits_one_member_and_keeps_unrelated_fresh_settings(self):
        original = deepcopy(dict(self.config))
        self.assertEqual(self.add(), 1)
        self.assertEqual(dict(self.config), original)
        row = self.row()
        self.config["model"] = "new-model"
        self.config.save_config()
        success, _ = self.store.review(row, "approve", "适中，按需展开")
        self.assertTrue(success)
        saved = self.store.current_config()
        self.assertEqual(saved["user_profiles"][1]["reply_length"], "适中，按需展开")
        self.assertEqual(saved["user_profiles"][0], original["user_profiles"][0])
        self.assertEqual(
            saved["relationship_presets"], original["relationship_presets"]
        )
        self.assertEqual(saved["model"], "new-model")
        self.assertIn(
            "适中，按需展开",
            self.bridge._profile_instructions(event_for("hi", "999", "456")),
        )
        self.assertNotIn(
            "适中，按需展开",
            self.bridge._profile_instructions(event_for("hi", "123", "456")),
        )
        self.assertEqual(self.store.list(True)[0]["status"], "approved")
        self.assertFalse(self.store.review(row, "approve")[0])
        self.assertTrue(self.store.review(row, "revert")[0])
        self.assertEqual(
            self.store.current_config()["user_profiles"][1]["reply_length"], "沿用人格"
        )

    async def test_mentioned_memory_appears_only_after_approval_and_reverts(self):
        memory = "Beta and the bot folded a paper boat named Cedar."
        self.evidence[0]["text"] = memory
        self.assertEqual(self.add([self.change("shared_memories", memory)]), 1)
        event = event_for("What happened with Beta?", "123", "456", "321")
        event.message_obj.message.append(At(qq="999"))
        pending_request = ProviderRequest()
        await self.bridge.personalize_request(event, pending_request)
        self.assertNotIn(memory, pending_request.system_prompt)

        row = self.row()
        self.assertTrue(self.store.review(row, "approve")[0])
        approved_request = ProviderRequest()
        await self.bridge.personalize_request(event, approved_request)
        self.assertIn(memory, approved_request.system_prompt)
        self.assertIn('"qq": "999"', approved_request.system_prompt)

        self.assertTrue(self.store.review(row, "revert")[0])
        reverted_request = ProviderRequest()
        await self.bridge.personalize_request(event, reverted_request)
        self.assertNotIn(memory, reverted_request.system_prompt)

    def test_manual_edits_and_preset_changes_invalidate_old_suggestions(self):
        self.add()
        row = self.row()
        self.config["user_profiles"][1]["custom_instructions"] = "Manual rule"
        self.config.save_config()
        self.assertFalse(self.store.review(row, "approve")[0])
        self.assertEqual(
            self.config["user_profiles"][1]["custom_instructions"], "Manual rule"
        )
        del self.config["user_profiles"][1]["custom_instructions"]
        self.config["relationship_presets"]["friend"] = "New preset"
        self.config.save_config()
        self.assertFalse(self.store.review(row, "approve")[0])

    def test_forbidden_fields_bad_options_and_fabricated_evidence_are_rejected(self):
        for change in [
            self.change("qq", "123"),
            self.change("owner_qq", "999"),
            self.change("persona_anchor", "erase"),
            self.change("reply_tone", "invented"),
            self.change("boundaries", ["invented"]),
            {**self.change(), "evidence_ids": ["forged"]},
        ]:
            with self.subTest(field=change["field"]), self.assertRaises(ValueError):
                self.add([change])
        self.assertEqual(self.store.list(), [])

    def test_rejection_deduplication_and_delete_survive_reload(self):
        self.add()
        row = self.row()
        self.assertTrue(self.store.review(row, "reject")[0])
        self.assertEqual(self.add(), 0)
        reloaded = ProfileReviewStore(
            self.config, self.root, self.bridge._refresh_profiles
        )
        self.assertEqual(reloaded.list(True)[0]["status"], "rejected")
        self.assertTrue(reloaded.review(row, "delete")[0])
        self.assertEqual(reloaded.list(True), [])

    def test_two_fields_in_same_batch_can_be_approved_without_losing_other_changes(
        self,
    ):
        self.add([self.change(), self.change("tags", ["music", "brief"])])
        rows = self.store.list()
        self.assertTrue(self.store.review(rows[0]["id"], "approve")[0])
        self.assertTrue(
            self.store.review(rows[1]["id"], "approve", "music\nconcise")[0]
        )
        profile = self.store.current_config()["user_profiles"][1]
        self.assertEqual(profile["reply_length"], "简短，通常 1–3 句")
        self.assertEqual(profile["tags"], ["music", "concise"])
        self.assertFalse(self.store.review(rows[0]["id"], "revert")[0])

    def test_relationship_requires_three_distinct_observations(self):
        self.assertEqual(self.add([self.change("relationship", "亲密")]), 0)
        self.evidence += [
            {"id": "event2", "text": "We talk every day", "timestamp": 2.0},
            {"id": "event3", "text": "We are close friends", "timestamp": 3.0},
        ]
        self.assertEqual(self.add([self.change("relationship", "亲密")]), 1)
        self.assertEqual(self.config["user_profiles"][1]["relationship"], "朋友")

    def test_disable_or_remove_member_prevents_approval(self):
        self.add()
        row = self.row()
        self.config["user_profiles"][1]["allow_learning"] = False
        self.config.save_config()
        self.assertFalse(self.store.review(row, "approve")[0])
        self.config["user_profiles"].pop()
        self.config.save_config()
        self.assertFalse(self.store.review(row, "approve")[0])

    def test_interrupted_write_is_recovered_from_disk_without_reapplying(self):
        self.add()
        row = self.row()
        original_save = self.store._save
        saves = 0

        def fail_after_write():
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("Simulated review journal failure")
            original_save()

        with (
            patch.object(self.store, "_save", side_effect=fail_after_write),
            self.assertRaises(OSError),
        ):
            self.store.review(row, "approve", "适中，按需展开")
        recovered = ProfileReviewStore(
            self.config, self.root, self.bridge._refresh_profiles
        )
        self.assertEqual(recovered.list(True)[0]["status"], "approved")
        self.assertEqual(recovered.list(True)[0]["new_value"], "适中，按需展开")
        self.assertFalse(recovered.review(row, "approve")[0])
        self.assertEqual(
            self.store.current_config()["user_profiles"][1]["reply_length"],
            "适中，按需展开",
        )

    def test_failed_configuration_write_does_not_report_success(self):
        self.add()
        row = self.row()
        before = self.store.current_config()
        with (
            patch.object(
                AstrBotConfig, "save_config", side_effect=OSError("disk full")
            ),
            self.assertRaises(OSError),
        ):
            self.store.review(row, "approve")
        recovered = ProfileReviewStore(
            self.config, self.root, self.bridge._refresh_profiles
        )
        self.assertEqual(recovered.list()[0]["status"], "pending")
        self.assertEqual(self.store.current_config(), before)

    async def test_worker_uses_only_senders_evidence_and_never_applies(self):
        async def propose(**kwargs):
            request = json.loads(kwargs["prompt"])
            self.assertEqual(request["profile"]["nickname"], "Beta")
            self.assertNotIn("Alpha", kwargs["prompt"])
            self.assertNotIn("qq", request["allowed_fields"])
            return json.dumps(
                {
                    "changes": [
                        {
                            **self.change(),
                            "evidence_ids": [request["messages"][0]["id"]],
                        }
                    ]
                }
            )

        adapter = SimpleNamespace(filter_chat_completion=AsyncMock(side_effect=propose))
        before = deepcopy(dict(self.config))
        with patch(
            "astrbot_plugin_local_codex.profile_learning.asyncio.sleep", new=AsyncMock()
        ):
            self.bridge.observe_profile_message(
                event_for("以后回复简短一点", "999", "456"), adapter
            )
            await asyncio.gather(*self.bridge.profile_learner.tasks.values())
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(dict(self.config), before)
        adapter.filter_chat_completion.assert_awaited_once()

    async def test_scope_inactive_plugin_and_shutdown_stop_learning(self):
        adapter = SimpleNamespace(filter_chat_completion=AsyncMock())
        for sender, group in [
            ("999", ""),
            ("999", "777"),
            ("777", "456"),
            ("321", "456"),
        ]:
            self.bridge.observe_profile_message(
                event_for("hello", sender, group), adapter
            )
        self.assertEqual(self.bridge.profile_learner.tasks, {})
        self.learning_meta.activated = False
        self.bridge.observe_profile_message(event_for("hello", "999", "456"), adapter)
        self.assertEqual(self.bridge.profile_learner.tasks, {})
        self.learning_meta.activated = True
        self.bridge.observe_profile_message(event_for("hello", "999", "456"), adapter)
        await self.bridge.profile_learner.close()
        adapter.filter_chat_completion.assert_not_awaited()

    async def test_cross_thread_review_runs_on_bridge_loop(self):
        self.add()
        self.bridge.review_loop = asyncio.get_running_loop()
        row = self.row()

        def web_thread():
            return asyncio.run(
                self.bridge.profile_review_api("review", row_id=row, action="approve")
            )

        self.assertTrue((await asyncio.to_thread(web_thread))[0])

    async def test_anchor_is_applied_each_request_without_changing_persona(self):
        self.config["persona_anchor"] = (
            "Stay playful; answer identity questions honestly."
        )
        for sender in ("123", "999", "123"):
            request = ProviderRequest(system_prompt="Configured character")
            await self.bridge.personalize_request(
                event_for("hello", sender, "456"), request
            )
            self.assertIn("Configured character", request.system_prompt)
            self.assertEqual(
                request.system_prompt.count(self.config["persona_anchor"]), 1
            )
