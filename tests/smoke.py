"""Exercise real Codex turns with synthetic AstrBot events, without sending to QQ."""

import argparse
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.message.components import At, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_local_codex.main import LocalCodex


async def exercise(plugin: LocalCodex) -> None:
    """Verify a first turn, a remembered follow-up, and an explicit new session."""
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.group_id = "local-group"
    message.session_id = "local-group"
    message.self_id = "local-bot"
    message.sender = MessageMember(user_id="local-member", nickname="Local smoke test")
    platform = PlatformMetadata(
        name="aiocqhttp", description="local test", id="local-test"
    )
    prompts = [
        "Reply with exactly LOCAL_CODEX_READY. Do not use any tools.",
        "Repeat the exact text in your previous answer and nothing else. Do not use tools.",
    ]
    for index, prompt in enumerate(prompts, 1):
        message.message = [At(qq="local-bot"), Plain(prompt)]
        event = AstrMessageEvent(prompt, message, platform, "local-group")
        results = [result async for result in plugin.chat(event)]
        texts = ["".join(part.text for part in result.chain) for result in results]
        assert texts[-1].strip() == "LOCAL_CODEX_READY", texts
        print(f"[OK] Turn {index}: real Codex reply and AstrBot session persistence")

    old_thread = next(iter(plugin.sessions.values()))
    message.message = [At(qq="local-bot"), Plain("/new")]
    event = AstrMessageEvent("new", message, platform, "local-group")
    results = [result async for result in plugin.reset(event)]
    assert len(results) == 1 and not plugin.sessions

    prompt = "Reply with exactly NEW_SESSION_READY. Do not use any tools."
    message.message = [At(qq="local-bot"), Plain(prompt)]
    event = AstrMessageEvent(prompt, message, platform, "local-group")
    results = [result async for result in plugin.chat(event)]
    assert (
        "".join(part.text for part in results[-1].chain).strip() == "NEW_SESSION_READY"
    )
    assert next(iter(plugin.sessions.values())) != old_thread
    print("[OK] /new: the subsequent request starts a different Codex thread")


async def main() -> None:
    """Read local CLI settings without loading real QQ configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-exe", required=True, type=Path)
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-qq-smoke-") as directory:
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=Path(directory),
        ):
            plugin = LocalCodex(
                None,
                {
                    "owner_qq": "local-owner",
                    "group_qq": "local-group",
                    "codex_exe": str(args.codex_exe),
                    "model": args.model,
                },
            )
        try:
            await exercise(plugin)
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    asyncio.run(main())
