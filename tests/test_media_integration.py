"""Exercise real bridge handlers and tool loops with no QQ delivery."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Record, Reply, Video
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    LocalCodexProvider,
)
from astrbot_plugin_local_codex.video_context import VideoContextError

from .test_bridge import event_for
from .test_voice import FixtureTTS


class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "codex_exe": sys.executable,
            "integrate_plugins": True,
            "group_context_enabled": True,
            "video_summary_enabled": True,
            "voice_enabled": True,
        }
        self.model = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text="蓝色背景中有红色方块。")
            )
        )
        self.audio = self.directory / "fixture.wav"
        self.audio.write_bytes(b"RIFF isolated fixture")
        self.tts = FixtureTTS({"id": "fixture_tts", "type": "fixture"}, {})
        self.tts.get_audio = AsyncMock(return_value=str(self.audio))
        self.star = SimpleNamespace(
            module_path="astrbot_plugin_local_codex.main",
            activated=True,
            reserved=False,
        )
        self.context = SimpleNamespace(
            get_provider_by_id=lambda provider_id: (
                self.model if provider_id == PROVIDER_ID else None
            ),
            get_using_tts_provider_async=AsyncMock(return_value=self.tts),
            get_all_stars=lambda: [self.star],
        )
        with patch(
            "astrbot_plugin_local_codex.main.StarTools.get_data_dir",
            return_value=self.directory,
        ):
            self.plugin = LocalCodex(self.context, self.config)

        async def download(url, destination, maximum_bytes):
            destination.write_bytes(b"isolated synthetic fixture")

        async def frames(video, directory):
            frame = directory / "frame-01.jpg"
            frame.write_bytes(b"isolated frame fixture")
            return [frame], 2.0, False

        self.patch_download = patch(
            "astrbot_plugin_local_codex.video_context.download_video",
            side_effect=download,
        )
        self.download = self.patch_download.start()
        self.addCleanup(self.patch_download.stop)
        self.plugin.video_context._frames = AsyncMock(side_effect=frames)

    @staticmethod
    def video_event(sender="123", group="456", mention="321"):
        event = event_for("请解释视频", sender, group, mention)
        event.session_id = f"{sender}_{group}" if group else sender
        event.message_obj.message.append(
            Video(file="https://video.example/fixture.mp4")
        )
        event.send = AsyncMock(
            side_effect=AssertionError("Video tests must not send QQ messages")
        )
        return event

    async def test_accepted_video_is_summarized_before_core_and_visible_to_other_members(
        self,
    ):
        event = self.video_event()
        self.assertEqual([value async for value in self.plugin.chat(event)], [])
        self.model.text_chat.assert_awaited_once()
        self.assertFalse(
            any(isinstance(component, Video) for component in event.get_messages())
        )
        self.assertEqual(event.get_extra("selected_provider"), PROVIDER_ID)
        self.assertFalse(event.call_llm)
        self.assertIn("蓝色背景中有红色方块", event.message_str)
        question = event_for("刚才那个视频是什么？", "999", "456", "321")
        request = ProviderRequest(prompt=question.message_str)
        await self.plugin.personalize_request(question, request)
        self.assertIn("蓝色背景中有红色方块", request.system_prompt)
        self.assertIn('"qq": "123"', request.system_prompt)
        self.assertEqual(len(self.plugin.group_context.rows), 2)
        event.send.assert_not_awaited()

    async def test_quoted_video_shared_record_retains_original_sender(self):
        event = self.video_event()
        original_video = event.message_obj.message.pop()
        event.message_obj.message.append(
            Reply(
                id="42", chain=[original_video], sender_id="999", sender_nickname="Beta"
            )
        )
        event.bot = SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "message_type": "group",
                    "group_id": "456",
                    "sender": {"user_id": "999", "nickname": "Beta"},
                    "message": [
                        {
                            "type": "video",
                            "data": {"file": "https://video.example/fixture.mp4"},
                        }
                    ],
                }
            )
        )
        self.assertEqual([value async for value in self.plugin.chat(event)], [])
        video_rows = [
            row for row in self.plugin.group_context.rows if "抽帧摘要" in row["text"]
        ]
        self.assertEqual(len(video_rows), 1)
        self.assertEqual((video_rows[0]["qq"], video_rows[0]["name"]), ("999", "Beta"))
        self.assertIn('"source_qq": "999"', event.message_str)
        self.assertFalse(
            any(
                isinstance(component, Video)
                for component in event.get_messages()[-2].chain
            )
        )
        self.assertIsInstance(original_video, Video)

    async def test_unmentioned_or_unauthorized_video_never_downloads(self):
        for sender, group, mention in (
            ("999", "456", None),
            ("123", "789", "321"),
            ("999", "", None),
        ):
            event = self.video_event(sender, group, mention)
            self.assertEqual([value async for value in self.plugin.chat(event)], [])
            self.assertTrue(event.call_llm)
        self.download.assert_not_awaited()
        self.model.text_chat.assert_not_awaited()

    async def test_analysis_failure_removes_video_and_preserves_question_without_false_memory(
        self,
    ):
        self.plugin.video_context._frames.side_effect = VideoContextError(
            "视频解析失败。"
        )
        event = self.video_event()
        self.assertEqual([value async for value in self.plugin.chat(event)], [])
        self.assertIn("请解释视频", event.message_str)
        self.assertIn("不能声称看过", event.message_str)
        self.assertFalse(
            any(isinstance(component, Video) for component in event.get_messages())
        )
        self.assertFalse(
            any("抽帧摘要" in row["text"] for row in self.plugin.group_context.rows)
        )
        self.model.text_chat.assert_not_awaited()

    def voice_tool(self):
        # Reuse the decorator's real schema, then bind it as the plugin loader does.
        registered = next(
            tool for tool in llm_tools.func_list if tool.name == "send_voice"
        )
        tool = copy.deepcopy(registered)
        tool.handler = self.plugin.send_voice
        tool.handler_module_path = "astrbot_plugin_local_codex.main"
        self.assertEqual(set(tool.parameters["properties"]), {"text"})
        return PermissionCheckedTool(tool, self.plugin.tool_policy)

    async def test_registered_voice_executor_finishes_turn_and_records_spoken_text(
        self,
    ):
        event = event_for("用语音祝我生日快乐", "999", "456", "321")
        event.send = AsyncMock()
        result = [
            value
            async for value in FunctionToolExecutor.execute(
                self.voice_tool(),
                ContextWrapper(context=SimpleNamespace(event=event)),
                text="生日快乐，愿你每天开心！",
            )
        ]
        self.assertEqual(result, [None])
        event.send.assert_awaited_once()
        record = event.send.call_args.args[0].chain[0]
        self.assertIsInstance(record, Record)
        self.assertEqual(record.text, "生日快乐，愿你每天开心！")
        rows = self.plugin.group_context.rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["role"], rows[0]["qq"], rows[0]["reply_to"]),
            ("assistant", "321", "999"),
        )
        self.assertIn(record.text, rows[0]["text"])

    async def test_voice_tool_none_stops_actual_agent_loop_without_second_model_reply(
        self,
    ):
        event = event_for("语音祝福", "999", "456", "321")
        event.send = AsyncMock()
        with patch(
            "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
            return_value=self.directory,
        ):
            provider = LocalCodexProvider(
                {"id": PROVIDER_ID, "type": PROVIDER_TYPE, "codex_exe": sys.executable},
                {},
            )
        provider.text_chat = AsyncMock(
            return_value=LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["send_voice"],
                tools_call_args=[{"text": "生日快乐！"}],
                tools_call_ids=["fixture-voice-call"],
            )
        )
        try:
            agent = ToolLoopAgentRunner()
            await agent.reset(
                provider=provider,
                request=ProviderRequest(
                    prompt="用语音祝我生日快乐",
                    func_tool=ToolSet(tools=[self.voice_tool()]),
                ),
                run_context=ContextWrapper(context=SimpleNamespace(event=event)),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=BaseAgentRunHooks(),
                request_max_retries=1,
            )
            async for _ in agent.step_until_done(max_step=3):
                pass
            self.assertTrue(agent.done())
            provider.text_chat.assert_awaited_once()
            event.send.assert_awaited_once()
            self.assertEqual(len(self.plugin.group_context.rows), 1)
        finally:
            await provider.terminate()
