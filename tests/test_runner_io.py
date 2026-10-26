"""Exercise real child pipes and cancellation without Codex or QQ traffic."""

import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_local_codex.runner import (
    CodexRateLimitError,
    collect_process_output,
    rate_limit_from_log,
)

WARNING = (
    "WARN codex_core::responses_retry: stream disconnected - retrying sampling "
    "request (1/5 in 292.9998434s)... sampling_error=exceeded retry limit, "
    "last status: 429 Too Many Requests"
)


class ProcessOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.process = None

    async def asyncTearDown(self):
        await self.stop()

    async def stop(self):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()

    async def start(self, code):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    async def exchange(self, timeout=5):
        return await collect_process_output(
            self.process, b"private-test-prompt", self.directory, timeout,
            stop=self.stop,
        )

    async def test_both_large_pipes_are_drained_and_prompt_is_stdin(self):
        await self.start(
            "import sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data+b'x'*200000); "
            "sys.stderr.buffer.write(b'y'*200000)"
        )
        stdout, stderr = await self.exchange()
        self.assertEqual(stdout, b"private-test-prompt" + b"x" * 200000)
        self.assertEqual(stderr, b"y" * 200000)
        self.assertEqual((self.directory / "last-run.jsonl").read_bytes(), stdout)
        self.assertEqual((self.directory / "last-error.log").read_bytes(), stderr)

    async def test_timeout_retains_partial_output_and_stops_child(self):
        await self.start(
            "import sys,time; print('turn.started'); "
            "print('diagnostic-before-timeout',file=sys.stderr); time.sleep(60)"
        )
        with self.assertRaisesRegex(TimeoutError, "1 秒"):
            await self.exchange(timeout=1)
        self.assertIsNotNone(self.process.returncode)
        self.assertIn(b"turn.started", (self.directory / "last-run.jsonl").read_bytes())
        self.assertIn(b"diagnostic", (self.directory / "last-error.log").read_bytes())

    async def test_native_429_stops_retry_sleep_and_preserves_log(self):
        await self.start(
            "import sys,time; print('turn.started'); "
            f"sys.stderr.write({WARNING[:80]!r}); sys.stderr.flush(); "
            "time.sleep(.05); "
            f"print({WARNING[80:]!r},file=sys.stderr); time.sleep(60)"
        )
        started = time.monotonic()
        with self.assertRaises(CodexRateLimitError) as caught:
            await self.exchange()
        self.assertEqual(caught.exception.retry_after_seconds, 293)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(self.process.returncode)
        self.assertIn(WARNING.encode(), (self.directory / "last-error.log").read_bytes())

    async def test_model_text_and_other_errors_do_not_trigger_cooldown(self):
        await self.start(
            "import sys; "
            f"print({WARNING!r}); "
            "print('tool says 429 Too Many Requests',file=sys.stderr)"
        )
        stdout, _ = await self.exchange()
        self.assertIn(WARNING.encode(), stdout)
        self.assertIsNone(rate_limit_from_log(b"tool says 429 Too Many Requests"))
        self.assertIsNone(rate_limit_from_log(WARNING.replace("429", "500").encode()))

    async def test_cancel_drains_and_stops_owned_child(self):
        await self.start("import time; print('started'); time.sleep(60)")
        task = asyncio.create_task(self.exchange())
        await asyncio.sleep(.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(self.process.returncode)
        self.assertTrue((self.directory / "last-run.jsonl").exists())

    def test_unknown_delay_uses_conservative_default(self):
        error = rate_limit_from_log(WARNING.replace("292.9998434s", "unknown").encode())
        self.assertEqual(error.retry_after_seconds, 300)
