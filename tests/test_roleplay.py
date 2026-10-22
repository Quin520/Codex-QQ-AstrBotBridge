"""Check dialogue styling through the actual bridge request and direct paths."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import ProviderRequest
from astrbot_plugin_local_codex.main import LocalCodex

from .test_bridge import THREAD, event_for


class RoleplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "codex_exe": sys.executable,
            "integrate_plugins": True,
            "natural_chat_enabled": True,
            "persona_anchor": "Keep the owner's character.",
            "roleplay_examples": "Fictional style example only.",
            "user_profiles": [{"qq": "999", "nickname": "Beta", "tags": ["brief"]}],
        }
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=Path(self.directory.name),
        ):
            self.plugin = LocalCodex(SimpleNamespace(get_all_stars=list), self.config)

    async def test_style_keeps_real_persona_profile_and_user_request(self):
        event = event_for("Please explain in detail.", "999", "456", "321")
        request = ProviderRequest(
            system_prompt="Original personality.", prompt=event.get_message_str()
        )
        await self.plugin.personalize_request(event, request)
        self.assertTrue(request.system_prompt.startswith("Original personality."))
        for expected in [
            "Keep the owner's character.",
            "Beta",
            "brief",
            "Fictional style example only.",
        ]:
            self.assertIn(expected, request.system_prompt)
        self.assertEqual(request.prompt, event.get_message_str())
        self.assertFalse(event.is_stopped())

    async def test_disabling_style_keeps_the_original_profile_pipeline(self):
        self.config["natural_chat_enabled"] = False
        request = ProviderRequest(system_prompt="Original personality.")
        await self.plugin.personalize_request(event_for("hi", "999", "456"), request)
        self.assertIn("Beta", request.system_prompt)
        self.assertIn(self.config["persona_anchor"], request.system_prompt)
        self.assertNotIn(self.config["roleplay_examples"], request.system_prompt)

    async def test_editing_or_clearing_examples_takes_effect_next_request(self):
        event = event_for("hi", "999", "456")
        self.config["roleplay_examples"] = "New owner-approved example."
        request = ProviderRequest()
        await self.plugin.personalize_request(event, request)
        self.assertIn("New owner-approved example.", request.system_prompt)
        self.assertNotIn("Fictional style example only.", request.system_prompt)
        self.config["roleplay_examples"] = ""
        request = ProviderRequest()
        await self.plugin.personalize_request(event, request)
        self.assertNotIn("<style_examples>", request.system_prompt)

    async def test_direct_mode_passes_examples_without_changing_session_identity(self):
        self.config["integrate_plugins"] = False
        self.plugin.runner.run = AsyncMock(return_value=(THREAD, "Hello"))
        event = event_for("hi", "123")
        replies = [reply async for reply in self.plugin._ask(event, "hi")]
        self.assertEqual(len(replies), 1)
        prompt, previous = self.plugin.runner.run.await_args.args
        self.assertIn(self.config["roleplay_examples"], prompt)
        self.assertIsNone(previous)
        self.assertEqual(
            self.plugin.sessions[f"{event.unified_msg_origin}|123"], THREAD
        )

    async def test_outside_group_never_receives_roleplay_or_member_data(self):
        request = ProviderRequest(system_prompt="Original personality.")
        event = event_for("hi", "999", "987")
        await self.plugin.personalize_request(event, request)
        self.assertTrue(event.is_stopped())
        self.assertEqual(request.system_prompt, "Original personality.")
