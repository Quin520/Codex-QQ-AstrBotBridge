"""Exercise voice delivery with fake TTS and senders; never contact QQ."""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.star import Context
from astrbot.core.message.components import Record
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.provider import TTSProvider
from astrbot_plugin_local_codex.voice import VoiceService

from .test_bridge import event_for


class FixtureTTS(TTSProvider):
    async def get_audio(self, text: str) -> str:
        raise AssertionError("Fixture must supply an isolated audio response.")


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.audio = Path(self.temporary.name) / "speech.wav"
        self.audio.write_bytes(b"RIFF isolated fake TTS output")
        self.provider = FixtureTTS({"id": "fixture_tts", "type": "fixture"}, {})
        self.provider.get_audio = AsyncMock(return_value=str(self.audio))
        self.context = SimpleNamespace(
            get_using_tts_provider_async=AsyncMock(return_value=self.provider)
        )
        self.config = {"voice_enabled": True, "owner_qq": "123", "group_qq": "456"}
        self.service = VoiceService(self.context, self.config)

    @staticmethod
    def event(sender="123", group="456"):
        event = event_for("Please say happy birthday", sender, group)
        event.send = AsyncMock()
        event.session_id = f"{sender}_{group}" if group else sender
        return event

    async def test_current_event_provider_and_record_receive_only_text(self):
        event = self.event()
        result = await self.service.send(event, "生日快乐，愿你今天开心！")
        self.assertIn("已发送到当前会话", result)
        self.context.get_using_tts_provider_async.assert_awaited_once_with(
            umo=event.unified_msg_origin
        )
        self.provider.get_audio.assert_awaited_once_with("生日快乐，愿你今天开心！")
        record = event.send.call_args.args[0].chain[0]
        self.assertIsInstance(record, Record)
        self.assertEqual(record.path, str(self.audio))
        self.assertEqual(record.text, "生日快乐，愿你今天开心！")
        self.assertEqual(event.get_extra("local_codex_voice_text"), record.text)

    async def test_explicit_provider_works_without_enabling_global_auto_tts(self):
        manager = ProviderManager.__new__(ProviderManager)
        manager.inst_map = {"fixture_tts": self.provider}
        manager.acm = SimpleNamespace(
            get_conf=lambda umo: {
                "provider_tts_settings": {"enable": False, "provider_id": "fixture_tts"}
            }
        )
        context = Context.__new__(Context)
        context.provider_manager = manager
        context.get_using_tts_provider_async = AsyncMock(return_value=None)
        event = self.event()
        self.assertIsNone(
            manager._resolve_using_provider(
                ProviderType.TEXT_TO_SPEECH, event.unified_msg_origin, None
            )
        )
        self.config["voice_tts_provider_id"] = "fixture_tts"
        service = VoiceService(context, self.config)
        self.assertIn("已发送到当前会话", await service.send(event, "hello"))
        context.get_using_tts_provider_async.assert_not_awaited()
        event.send.assert_awaited_once()
        self.assertFalse(manager.acm.get_conf(None)["provider_tts_settings"]["enable"])

    async def test_explicit_missing_or_non_tts_provider_cannot_send(self):
        self.config["voice_tts_provider_id"] = "fixture_tts"
        self.context.get_provider_by_id = lambda provider_id: None
        event = self.event()
        self.assertIn("没有可用", await self.service.send(event, "hello"))
        self.context.get_provider_by_id = lambda provider_id: SimpleNamespace()
        with self.assertRaisesRegex(RuntimeError, "尚未发送"):
            await self.service.send(event, "hello")
        self.context.get_using_tts_provider_async.assert_not_awaited()
        self.provider.get_audio.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_disabled_missing_provider_and_bad_text_never_send(self):
        event = self.event()
        self.config["voice_enabled"] = False
        self.assertIn("未启用", await self.service.send(event, "hello"))
        self.config["voice_enabled"] = True
        for text in ("", " ", None):
            self.assertIn("不能为空", await self.service.send(event, text))
        self.assertIn("超过", await self.service.send(event, "a" * 301))
        self.context.get_using_tts_provider_async.return_value = None
        self.assertIn("没有可用", await self.service.send(event, "hello"))
        self.provider.get_audio.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_same_event_and_same_group_cannot_spam_audio(self):
        event = self.event()
        await self.service.send(event, "hello")
        self.assertIn("已经尝试发送", await self.service.send(event, "different"))
        another = self.event("999")
        self.assertIn("刚请求过", await self.service.send(another, "hello"))
        self.provider.get_audio.assert_awaited_once()
        another.send.assert_not_awaited()

    async def test_public_group_and_owner_private_have_separate_cooldowns(self):
        for group in ("456", ""):
            event = self.event(group=group)
            self.assertIn("已发送", await self.service.send(event, "hello"))
            event.send.assert_awaited_once()
        self.assertEqual(self.provider.get_audio.await_count, 2)

    async def test_direct_calls_still_enforce_sender_and_group_scope(self):
        for sender, group in (("999", ""), ("123", "789"), ("", "456")):
            event = self.event(sender, group)
            with self.assertRaises(PermissionError):
                await self.service.send(event, "hello")
            event.send.assert_not_awaited()
        self.config["owner_qq"] = ""
        with self.assertRaises(PermissionError):
            await self.service.send(self.event(), "hello")
        self.provider.get_audio.assert_not_awaited()

    async def test_only_trusted_proactive_class_may_have_no_current_member(self):
        event = self.event("", "456")
        event.__class__ = type(
            "ProactiveMessageEvent",
            (type(event),),
            {
                "__module__": (
                    "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event"
                )
            },
        )
        self.assertIn("已发送到当前会话", await self.service.send(event, "hello"))
        event.send.assert_awaited_once()

    async def test_other_platform_is_not_allowed_even_with_matching_ids(self):
        event = self.event()
        event.platform_meta.name = "webchat"
        with self.assertRaises(PermissionError):
            await self.service.send(event, "hello")
        self.provider.get_audio.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_synthesis_failure_and_timeout_do_not_claim_delivery(self):
        for error in (RuntimeError("private provider detail"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.provider.get_audio.side_effect = error
                event = self.event()
                with self.assertRaisesRegex(RuntimeError, "尚未发送") as caught:
                    await self.service.send(event, "hello")
                self.assertNotIn("private provider detail", str(caught.exception))
                self.assertFalse(event.get_extra("local_codex_voice_sent"))
                event.send.assert_not_awaited()

    async def test_send_failure_does_not_claim_success_or_retry(self):
        event = self.event()
        event.send.side_effect = RuntimeError("private transport detail")
        with self.assertRaisesRegex(RuntimeError, "未能确认送达"):
            await self.service.send(event, "hello")
        self.assertIsNone(event.get_extra("local_codex_voice_text"))
        self.assertIn("已经尝试发送", await self.service.send(event, "hello"))
        event.send.assert_awaited_once()

    async def test_proactive_delivery_failure_is_not_success(self):
        event = self.event()
        event.proactive_send_failed = True
        with self.assertRaisesRegex(RuntimeError, "未能确认送达"):
            await self.service.send(event, "hello")
        self.assertIsNone(event.get_extra("local_codex_voice_text"))

    async def test_false_send_result_is_failure_even_without_extra_flag(self):
        event = self.event()
        event.send.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未能确认送达"):
            await self.service.send(event, "hello")
        self.assertIsNone(event.get_extra("local_codex_voice_text"))

    async def test_no_urls_missing_files_or_non_audio_paths_forwarded(self):
        bad_file = Path(self.temporary.name) / "private.txt"
        bad_file.write_text("do not send", encoding="utf-8")
        for output in ("https://example.com/audio.wav", "relative.wav", str(bad_file)):
            with self.subTest(output=output):
                self.provider.get_audio.return_value = output
                event = self.event()
                with self.assertRaisesRegex(RuntimeError, "尚未发送"):
                    await self.service.send(event, "hello")
                event.send.assert_not_awaited()

    async def test_parallel_calls_do_not_queue_unbounded_synthesis(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def synthesize(text):
            entered.set()
            await release.wait()
            return str(self.audio)

        self.provider.get_audio.side_effect = synthesize
        first = asyncio.create_task(self.service.send(self.event(), "hello"))
        await entered.wait()
        second_event = self.event(group="")
        self.assertIn("另一条语音", await self.service.send(second_event, "hello"))
        second_event.send.assert_not_awaited()
        release.set()
        self.assertIn("已发送", await first)

    def enable_character_speech(self):
        self.config["voice_use_rvc"] = True
        self.converted = Path(self.temporary.name) / "converted.mp3"
        self.converted_inputs = []

        @asynccontextmanager
        async def convert(source):
            self.converted_inputs.append(source)
            self.converted.write_bytes(b"ID3 isolated character audio")
            try:
                yield self.converted
            finally:
                self.converted.unlink()

        self.service.converter = SimpleNamespace(
            speech_error=lambda: "", converted_speech=convert
        )

    async def test_character_speech_sends_only_converted_audio_until_delivery_finishes(
        self,
    ):
        self.enable_character_speech()
        event = self.event()

        async def deliver(result):
            record = result.chain[0]
            self.assertEqual(record.path, str(self.converted))
            self.assertEqual(
                Path(record.path).read_bytes(), b"ID3 isolated character audio"
            )
            self.assertNotEqual(Path(record.path).read_bytes(), self.audio.read_bytes())

        event.send.side_effect = deliver
        result = await self.service.send(event, "生日快乐")
        self.assertIn("已发送", result)
        self.assertEqual(self.converted_inputs, [self.audio])
        self.assertEqual(event.get_extra("local_codex_voice_text"), "生日快乐")
        self.assertFalse(self.converted.exists())
        self.assertTrue(self.audio.exists())

    async def test_disabled_character_speech_retains_provider_voice(self):
        self.enable_character_speech()
        self.config["voice_use_rvc"] = False
        event = self.event()
        await self.service.send(event, "hello")
        self.assertEqual(self.converted_inputs, [])
        self.assertEqual(event.send.call_args.args[0].chain[0].path, str(self.audio))

    async def test_missing_unconfigured_or_busy_converter_never_calls_tts(self):
        self.config["voice_use_rvc"] = True
        event = self.event()
        self.assertIn("尚未接通", await self.service.send(event, "hello"))
        for message in ("角色音色模型未配置", "正在生成角色语音或翻唱"):
            with self.subTest(message=message):
                self.service.converter = SimpleNamespace(
                    speech_error=lambda message=message: message
                )
                self.assertEqual(await self.service.send(event, "hello"), message)
        self.provider.get_audio.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_conversion_failure_keeps_attempt_cooldown_without_original_fallback(
        self,
    ):
        self.enable_character_speech()
        for error in (RuntimeError("private model detail"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.service._last_sent.clear()

                @asynccontextmanager
                async def convert(source, error=error):
                    raise error
                    yield source

                self.service.converter.converted_speech = convert
                event = self.event()
                with self.assertRaisesRegex(RuntimeError, "尚未发送") as caught:
                    await self.service.send(event, "hello")
                self.assertNotIn("private model detail", str(caught.exception))
                self.assertTrue(event.get_extra("local_codex_voice_sent"))
                self.assertIsNone(event.get_extra("local_codex_voice_text"))
                event.send.assert_not_awaited()
                self.assertIn("已经尝试", await self.service.send(event, "retry"))
                self.assertIn(
                    "稍后", await self.service.send(self.event("999"), "retry")
                )

    async def test_character_speech_tts_failure_also_reserves_attempt_cooldown(self):
        self.enable_character_speech()
        self.provider.get_audio.side_effect = RuntimeError("private provider detail")
        event = self.event()
        with self.assertRaisesRegex(RuntimeError, "尚未发送"):
            await self.service.send(event, "hello")
        self.assertTrue(event.get_extra("local_codex_voice_sent"))
        self.assertIn("稍后", await self.service.send(self.event("999"), "retry"))
        self.assertEqual(self.converted_inputs, [])
        event.send.assert_not_awaited()

    async def test_converted_audio_is_cleaned_after_delivery_failure_or_cancellation(
        self,
    ):
        self.enable_character_speech()
        for error in (RuntimeError("transport failure"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.service._last_sent.clear()
                event = self.event()
                event.send.side_effect = error
                with self.assertRaises(type(error)):
                    await self.service.send(event, "hello")
                self.assertFalse(self.converted.exists())
                self.assertFalse(self.service._lock.locked())
                self.assertIsNone(event.get_extra("local_codex_voice_text"))
                event.send.assert_awaited_once()

    async def test_cancellation_during_conversion_sends_nothing_and_releases_resources(
        self,
    ):
        self.enable_character_speech()
        entered = asyncio.Event()
        released = asyncio.Event()

        @asynccontextmanager
        async def convert(source):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield source
            finally:
                released.set()

        self.service.converter.converted_speech = convert
        event = self.event()
        task = asyncio.create_task(self.service.send(event, "hello"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(released.is_set())
        self.assertFalse(self.service._lock.locked())
        self.assertIsNone(event.get_extra("local_codex_voice_text"))
        self.assertIn("稍后", await self.service.send(self.event("999"), "retry"))
        event.send.assert_not_awaited()
