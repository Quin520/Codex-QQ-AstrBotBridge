"""Exercise API routing and media without CLI workers or real QQ delivery."""

import base64
import io
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot_plugin_local_codex.api_services import (
    generate_api_image,
    search_public_web,
)
from astrbot_plugin_local_codex.backend import provider_id
from astrbot_plugin_local_codex.image_generator import ImageGenerator
from astrbot_plugin_local_codex.integration import configure_provider
from astrbot_plugin_local_codex.main import LocalCodex
from PIL import Image

from .test_bridge import event_for


class APIBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "model_backend": "api",
            "api_provider_id": "api-test",
            "image_generation_enabled": True,
            "web_search": True,
            "image_api_model": "gpt-image-2.5-flare",
        }
        self.client = SimpleNamespace(
            images=SimpleNamespace(generate=AsyncMock()),
            responses=SimpleNamespace(create=AsyncMock()),
        )
        self.provider = SimpleNamespace(
            client=SimpleNamespace(with_options=Mock(return_value=self.client)),
            get_model=lambda: "api-model",
        )
        self.context = SimpleNamespace(
            get_provider_by_id=Mock(return_value=self.provider)
        )

    def plugin(self):
        with (
            patch(
                "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
                return_value=self.root,
            ),
            patch(
                "astrbot_plugin_local_codex.main.CodexRunner",
                side_effect=AssertionError("CLI must not be constructed"),
            ),
            patch(
                "astrbot_plugin_local_codex.image_generator.CodexRunner",
                side_effect=AssertionError("No image CLI"),
            ),
        ):
            return LocalCodex(self.context, self.config)

    async def test_api_route_always_uses_pipeline_without_cli_workers(self):
        plugin = self.plugin()
        event = event_for("hello", "999", "456", "321")
        self.assertEqual([x async for x in plugin.chat(event)], [])
        self.assertEqual(event.get_extra("selected_provider"), "api-test")
        self.assertFalse(event.call_llm)
        self.assertIsNone(plugin.runner)
        self.assertIsNone(plugin.image_generator.runner)

    async def test_nonmentioned_group_does_not_trigger_api(self):
        event = event_for("hello", "999", "456")
        self.assertEqual([x async for x in self.plugin().chat(event)], [])
        self.assertTrue(event.call_llm)
        self.assertIsNone(event.get_extra("selected_provider"))

    async def test_selection_preserves_credentials_and_owner_permissions(self):
        class Config(dict):
            save_config_async = AsyncMock()

        global_config = Config(deepcopy(DEFAULT_CONFIG))
        api = {
            "id": "api-test",
            "enable": True,
            "provider_source_id": "api-source",
            "model": "api-model",
        }
        source = {
            "id": "api-source",
            "type": "openai_responses",
            "key": ["fixture-secret"],
        }
        global_config.update(provider=[api], provider_sources=[source])
        global_config["agent_runner"]["runner_type"] = "local"
        self.context.get_config = lambda: global_config
        self.context.provider_manager = SimpleNamespace(default_chat_provider_id="old")
        self.config["enforce_owner_permissions"] = True
        await configure_provider(self.context, self.config, self.root)
        self.assertEqual(global_config["provider_sources"], [source])
        self.assertEqual(global_config["provider"], [api])
        self.assertEqual(
            global_config["agent_runner"]["config"]["model"]["provider_id"], "api-test"
        )
        self.assertTrue(global_config["platform_settings"]["unique_session"])
        self.assertTrue(
            global_config["provider_settings"]["computer_use_require_admin"]
        )
        self.assertFalse(
            global_config["provider_settings"]["computer_use_local_permissions"][
                "member"
            ]["allow_execution"]
        )

    async def test_missing_api_selection_cannot_fall_back_to_cli(self):
        for selected in ("", "local_codex"):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                provider_id({"model_backend": "api", "api_provider_id": selected})

    async def test_image_request_writes_verified_file_and_uses_api_only(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
        self.client.images.generate.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(output.getvalue()).decode("ascii")
                )
            ]
        )
        generator = ImageGenerator(self.config, self.root, context=self.context)
        result = await generator.generate(
            event_for("draw", "999", "456", "321"), "A blue square"
        )
        self.assertEqual(result.read_bytes(), output.getvalue())
        self.assertTrue(result.resolve().is_relative_to(self.root.resolve()))
        self.assertEqual(self.client.images.generate.await_args.kwargs["n"], 1)
        self.assertIsNone(generator.runner)
        await generator.stop()

    async def test_api_image_rejects_remote_url_without_image_bytes(self):
        self.client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=None)]
        )
        with self.assertRaises(RuntimeError):
            await generate_api_image(self.context, self.config, "fixture")

    async def test_api_image_rejects_invalid_base64(self):
        self.client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json="%%%")]
        )
        with self.assertRaises(RuntimeError):
            await generate_api_image(self.context, self.config, "fixture")

    async def test_search_has_no_chat_history_and_keeps_source_urls(self):
        self.client.responses.create.return_value = SimpleNamespace(
            output_text="A fact",
            output=[
                SimpleNamespace(type="web_search_call", status="completed"),
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            annotations=[
                                SimpleNamespace(
                                    type="url_citation",
                                    title="Source",
                                    url="https://example.com/fact",
                                )
                            ]
                        )
                    ],
                ),
            ],
        )
        result = await search_public_web(self.context, self.config, "public topic")
        self.assertIn("https://example.com/fact", result)
        request = self.client.responses.create.await_args.kwargs
        self.assertEqual(request["input"], "public topic")
        self.assertFalse(request["store"])
        self.assertEqual(request["tools"], [{"type": "web_search"}])
        self.assertNotIn("previous_response_id", request)

    async def test_search_without_tool_execution_is_not_reported_as_success(self):
        self.client.responses.create.return_value = SimpleNamespace(
            output_text="Guess", output=[]
        )
        with self.assertRaises(RuntimeError):
            await search_public_web(self.context, self.config, "public topic")

    async def test_video_summary_uses_the_selected_api_provider(self):
        plugin = self.plugin()
        plugin.video_context.summarize = AsyncMock(return_value=None)
        event = event_for("video", "999", "456", "321")
        await plugin._prepare_video(event)
        self.context.get_provider_by_id.assert_called_once_with("api-test")
        plugin.video_context.summarize.assert_awaited_once_with(event, self.provider)

    async def test_new_conversation_does_not_reset_group_memory(self):
        self.config["group_context_enabled"] = True
        plugin = self.plugin()
        self.context.conversation_manager = SimpleNamespace(
            new_conversation=AsyncMock()
        )
        plugin.group_context.observe(event_for("Public fact", "999", "456", "321"))
        before = deepcopy(plugin.group_context.rows)
        self.assertTrue(before)
        event = event_for("/new", "999", "456", "321")
        replies = [x async for x in plugin.reset(event)]
        self.assertTrue(replies)
        self.context.conversation_manager.new_conversation.assert_awaited_once()
        self.assertEqual(plugin.group_context.rows, before)
