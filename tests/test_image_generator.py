"""Verify image permissions and artifact isolation without QQ delivery."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_local_codex.image_generator import (
    ImageGenerator,
    _regular_path,
    collect_generated_image,
)
from astrbot_plugin_local_codex.runner import CodexRunner
from PIL import Image

from .test_bridge import OTHER_THREAD, THREAD, event_for, output_for


class ImageGeneratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.artifacts = self.root / "codex" / "generated_images"
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "codex_exe": sys.executable,
            "model": "configured-model",
            "fast_mode": True,
            "reasoning_effort": "medium",
            "image_generation_enabled": True,
        }
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")}):
            self.generator = ImageGenerator(self.config, self.root / "plugin")
        self.generator.runner.run = AsyncMock(return_value=(THREAD, "ignored"))

    def image(self, thread=THREAD, filename="generated.png"):
        folder = self.artifacts / thread
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        Image.new("RGB", (8, 8), "orange").save(path)
        return path

    async def test_generation_copies_only_native_thread_artifact(self):
        expected = self.image()
        other = self.image(OTHER_THREAD)
        self.generator.runner.run.return_value = (THREAD, str(other))
        outgoing = await self.generator.generate(
            event_for("画猫", "999", "456"), "橘猫"
        )
        self.assertEqual(outgoing.read_bytes(), expected.read_bytes())
        self.assertEqual(outgoing.parent, self.generator.data_dir / "outgoing")
        self.assertNotEqual(outgoing, expected)
        self.assertTrue(expected.exists())
        self.assertTrue(other.exists())
        args, kwargs = self.generator.runner.run.call_args
        self.assertIn('"image_description": "橘猫"', args[0])
        self.assertEqual(args[1], None)
        self.assertEqual(kwargs, {"ephemeral": True})

    async def test_owner_private_allowed_but_other_private_group_adapter_rejected(self):
        self.image()
        await self.generator.generate(event_for("画猫", "123"), "cat")
        for sender, group in (("999", ""), ("123", "789"), ("999", "789")):
            with (
                self.subTest(sender=sender, group=group),
                self.assertRaises(PermissionError),
            ):
                await self.generator.generate(event_for("draw", sender, group), "cat")
        foreign = event_for("draw", "123", "456")
        foreign.get_platform_name = lambda: "webchat"
        with self.assertRaises(PermissionError):
            await self.generator.generate(foreign, "cat")
        self.assertEqual(self.generator.runner.run.await_count, 1)

    async def test_disable_and_empty_owner_revoke_generation(self):
        self.config["image_generation_enabled"] = False
        with self.assertRaises(PermissionError):
            await self.generator.generate(event_for("draw"), "cat")
        self.config["image_generation_enabled"] = True
        self.config["owner_qq"] = ""
        with self.assertRaises(PermissionError):
            await self.generator.generate(event_for("draw", "999", "456"), "cat")
        self.generator.runner.run.assert_not_awaited()

    async def test_prompt_bound_busy_and_shared_group_cooldown(self):
        event = event_for("draw", "999", "456")
        for prompt in (None, "", " ", "x" * 1501):
            with self.assertRaises(ValueError):
                await self.generator.generate(event, prompt)
        await self.generator.lock.acquire()
        with self.assertRaisesRegex(RuntimeError, "already"):
            await self.generator.generate(event, "cat")
        self.generator.lock.release()
        self.image()
        await self.generator.generate(event, "cat")
        with self.assertRaisesRegex(RuntimeError, "cooling"):
            await self.generator.generate(event_for("draw", "123", "456"), "dog")
        self.assertEqual(self.generator.runner.run.await_count, 1)

    async def test_cli_failure_is_not_reported_as_image_and_worker_can_stop(self):
        self.generator.runner.run.side_effect = RuntimeError("CLI failed")
        with self.assertRaisesRegex(RuntimeError, "CLI failed"):
            await self.generator.generate(event_for("draw"), "cat")
        self.assertFalse((self.generator.data_dir / "outgoing").exists())
        self.generator.runner.stop = AsyncMock()
        await self.generator.stop()
        self.generator.runner.stop.assert_awaited_once()

    def test_missing_invalid_and_ambiguous_artifacts_are_rejected(self):
        output = self.root / "out"
        self.image(OTHER_THREAD)
        with self.assertRaises(RuntimeError):
            collect_generated_image(self.artifacts, THREAD, output)
        for thread in ("../secret", "", "not-a-thread"):
            with self.assertRaises(RuntimeError):
                collect_generated_image(self.artifacts, thread, output)
        first = self.image()
        first.write_bytes(b"this is not an image")
        with self.assertRaises(RuntimeError):
            collect_generated_image(self.artifacts, THREAD, output)
        self.image()
        self.image(filename="second.png")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            collect_generated_image(self.artifacts, THREAD, output)
        self.assertFalse(output.exists())

    def test_jpeg_verified_and_oversize_rejected(self):
        self.image(filename="generated.jpg")
        result = collect_generated_image(self.artifacts, THREAD, self.root / "out")
        self.assertEqual(result.suffix, ".jpg")
        with (
            patch("astrbot_plugin_local_codex.image_generator.MAX_IMAGE_BYTES", 10),
            self.assertRaisesRegex(RuntimeError, "size limit"),
        ):
            collect_generated_image(self.artifacts, THREAD, self.root / "large")

    def test_hardlinks_and_windows_reparse_points_rejected(self):
        path = self.image()
        link = self.root / "hardlink.png"
        os.link(path, link)
        with self.assertRaisesRegex(RuntimeError, "regular"):
            collect_generated_image(self.artifacts, THREAD, self.root / "out")
        fake = SimpleNamespace(st_mode=path.stat().st_mode, st_file_attributes=0x400)
        with (
            patch.object(Path, "lstat", return_value=fake),
            self.assertRaisesRegex(RuntimeError, "links"),
        ):
            _regular_path(path)

    async def test_worker_profile_denies_host_reads_and_keeps_configured_model(self):
        login = AsyncMock(returncode=0)
        login.communicate.return_value = (b"Logged in using ChatGPT", b"")
        turn = AsyncMock(returncode=0)
        turn.communicate.return_value = (output_for(), b"")
        runner = CodexRunner(
            Path(sys.executable),
            self.root,
            30,
            "configured-model",
            reasoning_effort="medium",
            image_generation=True,
        )
        with patch(
            "astrbot_plugin_local_codex.runner.asyncio.create_subprocess_exec",
            side_effect=[login, turn],
        ) as spawn:
            await runner.run("new image only", None, ephemeral=True)
        argv = spawn.call_args_list[1].args
        self.assertNotIn("--sandbox", argv)
        for flag in (
            "--strict-config",
            "--ignore-rules",
            "--ignore-user-config",
            "--ephemeral",
        ):
            self.assertIn(flag, argv)
        self.assertIn('default_permissions="qq_image_generation"', argv)
        self.assertIn(
            'permissions.qq_image_generation.filesystem={":workspace_roots"="read"}',
            argv,
        )
        self.assertIn("permissions.qq_image_generation.network.enabled=false", argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertIn("features.skip_host_skill_discovery=true", argv)
        self.assertIn("features.hooks=false", argv)
        self.assertIn("features.memories=false", argv)
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertIn('service_tier="fast"', argv)
        self.assertEqual(argv[argv.index("--model") + 1], "configured-model")
        self.assertEqual(argv[argv.index("image_generation") - 1], "--enable")
        for feature in (
            "shell_tool",
            "unified_exec",
            "view_image",
            "apps",
            "plugins",
            "multi_agent",
            "browser_use",
            "computer_use",
        ):
            self.assertEqual(argv[argv.index(feature) - 1], "--disable")

    async def test_worker_cannot_resume_attach_or_use_web(self):
        runner = self.generator.runner
        for kwargs in (
            {"thread_id": THREAD, "ephemeral": True},
            {"thread_id": None, "ephemeral": False},
            {
                "thread_id": None,
                "ephemeral": True,
                "images": [self.root / "secret.png"],
            },
            {
                "thread_id": None,
                "ephemeral": True,
                "output_schema": self.root / "schema.json",
            },
        ):
            with self.assertRaises(ValueError):
                await CodexRunner.run(runner, "test", **kwargs)
        runner.web_search = True
        with self.assertRaises(ValueError):
            await CodexRunner.run(runner, "test", None, ephemeral=True)


if __name__ == "__main__":
    unittest.main()
