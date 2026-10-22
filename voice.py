"""Send bounded, on-demand speech through the current AstrBot TTS provider."""

import asyncio
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Record
from astrbot.core.provider.provider import TTSProvider

from .group_context import is_proactive_group


class VoiceService:
    """Expose speech text, never a destination, executable or arbitrary file."""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config
        self._lock = asyncio.Lock()
        self._last_sent = {}

    def _limit(self, key: str, default: int, low: int, high: int) -> int:
        return max(low, min(high, int(self.config.get(key, default))))

    def _allowed(self, event: AstrMessageEvent) -> bool:
        owner = str(self.config.get("owner_qq", "")).strip()
        target = str(self.config.get("group_qq", "")).strip()
        sender = str(event.get_sender_id() or "")
        group = str(event.get_group_id() or "")
        return (
            event.get_platform_name() == "aiocqhttp"
            and bool(owner)
            and (
                (not group and sender == owner)
                or bool(target)
                and group == target
                and (bool(sender) or is_proactive_group(event))
            )
        )

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        # Shared group cooldown prevents members bypassing it with isolated UMO.
        group = str(event.get_group_id() or "")
        if group:
            return f"{event.get_platform_id()}:GroupMessage:{group}"
        return str(event.unified_msg_origin)

    @staticmethod
    def _audio_path(value: str) -> Path:
        # Only provider-created local audio is accepted, never a URL to forward.
        path = Path(value)
        if (
            not path.is_absolute()
            or path.suffix.lower() not in {".wav", ".mp3", ".ogg", ".amr", ".silk"}
            or not path.is_file()
            or not 0 < path.stat().st_size <= 10 * 1024 * 1024
        ):
            raise ValueError("TTS provider did not return a valid local audio file.")
        return path

    async def send(self, event: AstrMessageEvent, text: str) -> str:
        """Synthesize short speech and send only to the trusted current event."""
        if not self._allowed(event):
            raise PermissionError("This QQ event is not allowed to send voice.")
        if not self.config.get("voice_enabled", False):
            return "语音功能未启用；请正常文字回复，不要声称已发送语音。"
        if not isinstance(text, str) or not text.strip():
            return "语音内容不能为空；请提供要朗读的简短文字。"
        text = text.strip()
        maximum = self._limit("voice_max_chars", 300, 40, 500)
        if len(text) > maximum:
            return f"语音内容超过 {maximum} 字，请缩短后再生成。"
        if event.get_extra("local_codex_voice_sent"):
            return "本次回复已经尝试发送语音，请勿重复发送。"
        if self._lock.locked():
            return "正在生成另一条语音，请稍后再试，不要声称已经发送。"

        scope = self._scope(event)
        cooldown = self._limit("voice_cooldown_seconds", 30, 5, 600)
        now = time.monotonic()
        self._last_sent = {
            key: when for key, when in self._last_sent.items() if now - when < 600
        }
        if now - self._last_sent.get(scope, float("-inf")) < cooldown:
            return "当前会话刚发送过语音，请稍后再试，不要重复发送。"

        async with self._lock:
            try:
                async with asyncio.timeout(30):
                    provider_id = str(
                        self.config.get("voice_tts_provider_id", "") or ""
                    ).strip()
                    if provider_id:
                        provider = self.context.get_provider_by_id(provider_id)
                        if provider is not None and not isinstance(
                            provider, TTSProvider
                        ):
                            raise TypeError("Configured voice provider is not TTS.")
                    else:
                        provider = await self.context.get_using_tts_provider_async(
                            umo=event.unified_msg_origin
                        )
                    if provider is None:
                        return "当前会话没有可用的 TTS 提供商；请正常文字回复。"
                    path = self._audio_path(await provider.get_audio(text))
            except TimeoutError as exc:
                raise RuntimeError("语音生成超时，尚未发送语音。") from exc
            except Exception as exc:
                raise RuntimeError("语音生成失败，尚未发送语音。") from exc

            # Reserve before sending: if delivery times out, avoid duplicate audio.
            self._last_sent[scope] = time.monotonic()
            event.set_extra("local_codex_voice_sent", True)
            try:
                async with asyncio.timeout(30):
                    sent = await event.send(
                        event.chain_result([Record.fromFileSystem(path, text=text)])
                    )
                if sent is False or getattr(event, "proactive_send_failed", False):
                    raise RuntimeError(
                        "The proactive adapter did not confirm delivery."
                    )
            except Exception as exc:
                raise RuntimeError(
                    "语音发送失败，未能确认送达；请不要声称已发送，也不要立即重发。"
                ) from exc
            event.set_extra("local_codex_voice_text", text)
            return "语音已发送到当前会话，无需再用文字重复整段内容。这是朗读语音，不是歌唱。"
