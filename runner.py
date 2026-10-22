"""Run the installed Codex CLI without passing prompts through a shell."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path


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
        if thread_id:
            uuid.UUID(thread_id)
        workspace = self.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            env.pop(key, None)
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
            [
                "exec",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "--color",
                "never",
                "-C",
                str(workspace),
            ]
        )
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
            stdout, stderr = await asyncio.wait_for(
                self.process.communicate(prompt.encode("utf-8")),
                self.timeout,
            )
            (self.data_dir / "last-run.jsonl").write_bytes(stdout)
            (self.data_dir / "last-error.log").write_bytes(stderr)
            if self.process.returncode:
                raise RuntimeError(
                    f"Codex exited with code {self.process.returncode}; check the local run log."
                )
            return parse_result(stdout)
        finally:
            await self.stop()
            self.process = None
