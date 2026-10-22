"""Run real Codex through AstrBot's actual tool loop without sending QQ messages."""

import argparse
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot_plugin_local_codex.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    LocalCodexProvider,
)
from PIL import Image

from .test_bridge import event_for


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-exe", required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    event = event_for("lookup", "999", "456", "321")
    event.send = AsyncMock(
        side_effect=AssertionError("Smoke test must not send QQ messages")
    )
    calls = []

    async def lookup(event, key):
        assert key == "sample"
        calls.append(key)
        return "NATIVE_TOOL_READY"

    tool = FunctionTool(
        name="lookup_test_value",
        description="Read the test value for a key. Use the returned value in the final answer.",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        handler=lookup,
    )
    with tempfile.TemporaryDirectory(prefix="codex-provider-smoke-") as directory:
        with patch(
            "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
            return_value=Path(directory),
        ):
            provider = LocalCodexProvider(
                {
                    "id": PROVIDER_ID,
                    "type": PROVIDER_TYPE,
                    "codex_exe": args.codex_exe,
                    "model": args.model,
                },
                {},
            )
        try:
            agent = ToolLoopAgentRunner()
            await agent.reset(
                provider=provider,
                request=ProviderRequest(
                    prompt="Call lookup_test_value with key sample. Then reply with exactly the returned value and nothing else.",
                    func_tool=ToolSet(tools=[tool]),
                ),
                run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=BaseAgentRunHooks(),
                request_max_retries=1,
            )
            async for _ in agent.step_until_done(max_step=3):
                pass
            assert calls == ["sample"], calls
            assert agent.final_llm_resp.completion_text.strip() == "NATIVE_TOOL_READY"
            event.send.assert_not_awaited()
            print(
                "[OK] Real Codex selected a tool; AstrBot executed it; Codex used its result"
            )
            sample = Path(directory) / "color.png"
            Image.new("RGB", (64, 64), color="red").save(sample)
            image_answer = await provider.text_chat(
                prompt="What is the dominant color of this image? Reply with one English word.",
                image_urls=[str(sample)],
            )
            assert "red" in image_answer.completion_text.lower(), (
                image_answer.completion_text
            )
            print("[OK] A real image attachment reached Codex and was recognized")
        finally:
            await provider.terminate()


if __name__ == "__main__":
    asyncio.run(main())
