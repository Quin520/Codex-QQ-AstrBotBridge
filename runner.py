"""Run the installed Codex CLI without passing prompts through a shell."""

import asyncio
import json
import math
import os
import re
import subprocess
import uuid
from pathlib import Path


class CodexRateLimitError(RuntimeError):
    """A real upstream 429, with the CLI's advertised retry delay."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"Codex 服务限流（HTTP 429），请约 {self.retry_after_seconds} 秒后重试。"
        )


def rate_limit_from_log(line: bytes) -> CodexRateLimitError | None:
    """Accept only the native retry logger, never a model answer or tool output."""
    text = line.decode("utf-8", errors="replace")
    if not (
        "codex_core::responses_retry:" in text
        and "stream disconnected - retrying sampling request" in text
        and re.search(r"\b429 Too Many Requests\b", text)
    ):
        return None
    delay = re.search(r"\(\d+/\d+ in (\d+(?:\.\d+)?)s\)", text)
    return CodexRateLimitError(math.ceil(float(delay[1])) if delay else 300)


async def collect_process_output(
    process, prompt: bytes, directory: Path, timeout: int, *, stop
):
    """Drain both pipes, retaining partial diagnostics on timeout/cancellation.

    The caller supplies its owned-process terminator. A native 429 is surfaced immediately so
    its multi-minute retry sleep cannot outlive the bridge's request deadline.
    """
    limited = asyncio.get_running_loop().create_future()

    async def read_stream(stream, path: Path, watch_retry: bool):
        output = bytearray()
        pending = b""
        with path.open("wb") as log:
            while chunk := await stream.read(65536):
                log.write(chunk)
                log.flush()
                output.extend(chunk)
                if not watch_retry:
                    continue
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    error = rate_limit_from_log(line)
                    if error and not limited.done():
                        limited.set_result(error)
                # Retry messages are short; do not retain an unlimited partial line.
                pending = pending[-16384:]
            if watch_retry and pending and not limited.done():
                error = rate_limit_from_log(pending)
                if error:
                    limited.set_result(error)
        return bytes(output)

    async def write_prompt():
        try:
            process.stdin.write(prompt)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Like Process.communicate(), let the child's exit/log explain this.
            pass
        finally:
            process.stdin.close()

    async def exchange():
        async with asyncio.TaskGroup() as group:
            stdout = group.create_task(read_stream(process.stdout, directory / "last-run.jsonl", False))
            stderr = group.create_task(read_stream(process.stderr, directory / "last-error.log", True))
            group.create_task(write_prompt())
            group.create_task(process.wait())
        return stdout.result(), stderr.result()

    communication = asyncio.create_task(exchange())
    try:
        done, _ = await asyncio.wait(
            {communication, limited}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if limited in done:
            raise limited.result()
        if communication not in done:
            raise TimeoutError(f"Codex 模型请求超过 {timeout} 秒，已保留本次诊断日志。")
        return await communication
    finally:
        limited.cancel()
        if not communication.done():
            # Keep draining while killing the child; a full pipe must not block
            # Process.wait() during timeout or plugin unload.
            try:
                await stop()
            finally:
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)


def parse_result(output: bytes) -> tuple[str, str]:
    """Extract a successful turn and its explicit thread ID.

    Args:
        output: Codex JSONL output encoded as UTF-8.

    Returns:
        The thread ID and the final assistant message.

    Raises:
        RuntimeError: The turn failed or did not produce a complete answer.
    """
    thread_id = ""
    answer = ""
    completed = False
    for line in output.decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("type") == "thread.started":
            thread_id = item["thread_id"]
        elif item.get("type") == "item.completed":
            content = item.get("item", {})
            if content.get("type") == "agent_message":
                answer = content.get("text", "")
        elif item.get("type") == "turn.completed":
            completed = True
        elif item.get("type") in {"turn.failed", "error"}:
            raise RuntimeError("Codex reported a failed turn; check the local run log.")
    if not (thread_id and answer.strip() and completed):
        raise RuntimeError(
            "Codex did not return a complete answer; check the local run log."
        )
    uuid.UUID(thread_id)
    return thread_id, answer


class CodexRunner:
    """Keep subprocess ownership so timeout and plugin unload stop our child."""

    def __init__(
        self,
        executable: Path,
        data_dir: Path,
        timeout: int,
        model: str,
        fast_mode: bool = True,
        reasoning_effort: str = "low",
        *,
        workspace: Path | None = None,
        web_search: bool = False,
        image_generation: bool = False,
    ):
        self.executable = executable
        self.data_dir = data_dir
        self.timeout = timeout
        self.model = model
        self.fast_mode = fast_mode
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Reasoning effort must be low, medium, high, or xhigh.")
        self.reasoning_effort = reasoning_effort
        self.web_search = web_search
        self.image_generation = image_generation
        self.workspace = workspace or data_dir / "workspace"
        self.process = None

    async def stop(self) -> None:
        """Terminate only the process tree started by this runner."""
        process = self.process
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            process.kill()
        await process.wait()

    async def run(
        self,
        prompt: str,
        thread_id: str | None,
        *,
        output_schema: Path | None = None,
        images: list[Path] | None = None,
        ephemeral: bool = False,
    ) -> tuple[str, str]:
        """Run one turn using the current Windows user's ChatGPT login.

        Args:
            prompt: User text delivered exclusively through stdin.
            thread_id: Explicit conversation ID, or None for a new conversation.
            output_schema: Optional schema for an AstrBot provider response.
            images: Explicit message attachments, never paths chosen by the model.
            ephemeral: Let AstrBot own history instead of persisting a CLI session.

        Returns:
            The conversation ID and assistant answer.

        Raises:
            RuntimeError: Login, execution, or output validation failed.
            TimeoutError: The configured timeout was reached.
        """
        if not self.executable.is_file() or self.executable.suffix.lower() != ".exe":
            raise RuntimeError("Configure the absolute path to the native codex.exe.")
        if self.image_generation and (
            thread_id or images or output_schema or not ephemeral or self.web_search
        ):
            raise ValueError(
                "Image generation requires a new, text-only ephemeral turn."
            )
        if thread_id:
            uuid.UUID(thread_id)
        workspace = self.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            env.pop(key, None)
        # CLI exec normally hides WARN diagnostics, including 429 retry sleeps.
        # Enable only this target; broad INFO/DEBUG logs include account metadata.
        env["RUST_LOG"] = "error,codex_core::responses_retry=warn"
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **options,
        )
        try:
            stdout, stderr = await asyncio.wait_for(self.process.communicate(), 15)
            if self.process.returncode or b"ChatGPT" not in stdout + stderr:
                raise RuntimeError(
                    "Run codex login and choose Sign in with ChatGPT first."
                )
        finally:
            await self.stop()
            self.process = None

        args = [
            str(self.executable),
            "--no-daemon",
            "--ask-for-approval",
            "never",
        ]
        # QQ members use this as a chatbot, without local execution or app tools.
        for feature in (
            "shell_tool",
            "unified_exec",
            "apps",
            "browser_use",
            "computer_use",
            "view_image",
            "plugins",
            "skill_search",
            "multi_agent",
            "tool_suggest",
        ):
            args.extend(["--disable", feature])
        args.extend(
            ["--enable" if self.image_generation else "--disable", "image_generation"]
        )
        args.extend(
            [
                "exec",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--json",
                "--color",
                "never",
                "-C",
                str(workspace),
            ]
        )
        if self.image_generation:
            # Unlike read-only sandbox mode, this denies reads outside the empty
            # task workspace, including native image-gen reference-image reads.
            # An unsupported Windows sandbox must fail closed, not run unsandboxed.
            args.extend(["--strict-config", "--ignore-rules"])
            for setting in (
                'default_permissions="qq_image_generation"',
                'permissions.qq_image_generation.filesystem={":workspace_roots"="read"}',
                "permissions.qq_image_generation.network.enabled=false",
                "project_doc_max_bytes=0",
                "features.skip_host_skill_discovery=true",
                "features.hooks=false",
                "features.memories=false",
            ):
                args.extend(["-c", setting])
        else:
            args.extend(["--sandbox", "read-only"])
        if self.model:
            args.extend(["--model", self.model])
        args.extend(
            [
                "-c",
                'service_tier="fast"' if self.fast_mode else 'service_tier="default"',
                "-c",
                "features.fast_mode=true",
                "-c",
                f'model_reasoning_effort="{self.reasoning_effort}"',
                "-c",
                'web_search="live"' if self.web_search else 'web_search="disabled"',
            ]
        )
        if output_schema:
            args.extend(["--output-schema", str(output_schema)])
        if ephemeral:
            args.append("--ephemeral")
        for attachment in images or []:
            args.extend(["--image", str(attachment)])
        if thread_id:
            args.extend(["resume", thread_id])
        args.append("-")
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **options,
        )
        try:
            stdout, stderr = await collect_process_output(
                self.process, prompt.encode("utf-8"), self.data_dir, self.timeout,
                stop=self.stop,
            )
            if self.process.returncode:
                raise RuntimeError(
                    f"Codex exited with code {self.process.returncode}; check the local run log."
                )
            return parse_result(stdout)
        finally:
            await self.stop()
            self.process = None
