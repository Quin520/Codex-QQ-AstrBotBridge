"""Exercise video boundaries with fictional messages and no QQ delivery."""

import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.message.components import At, Image, Node, Nodes, Plain, Reply, Video
from astrbot_plugin_local_codex.video_context import (
    PublicVideoResolver,
    VideoContext,
    VideoContextError,
    _run_media,
    apply_summary,
    download_video,
    validate_video_url,
)


def event_for(components, sender="123", group="456", platform="fixture"):
    """Supply only verified event accessors; no bot sender is implemented."""
    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=components, raw_message={"fixture": "unchanged"}
        ),
        message_str="请看看这个视频",
        get_sender_id=lambda: sender,
        get_sender_name=lambda: "Alpha",
        get_group_id=lambda: group,
        get_platform_id=lambda: platform,
        get_self_id=lambda: "321",
    )
    event.get_messages = lambda: event.message_obj.message
    return event


class Response:
    """Small aiohttp response fixture for redirect and streamed size limits."""

    def __init__(self, status=200, chunks=(), headers=None, length=None):
        self.status = status
        self.headers = headers or {}
        self.content_length = length
        self.url = "https://video.example/video"
        self.chunks = chunks
        self.content = self

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class VideoNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_timeout_kills_hidden_process_without_using_shell(self):
        process = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=[TimeoutError(), (b"", b"")]),
            kill=Mock(),
        )
        with (
            patch(
                "astrbot_plugin_local_codex.video_context.asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ) as spawn,
            self.assertRaises(TimeoutError),
        ):
            await _run_media(["trusted-ffmpeg", "-i", "owned-temp-video"], 1)
        process.kill.assert_called_once()
        self.assertNotIn("shell", spawn.call_args.kwargs)
        self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertEqual(
                spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW
            )

    def test_only_public_attachment_urls_are_accepted(self):
        self.assertEqual(
            validate_video_url("https://video.example/clip.mp4"),
            "https://video.example/clip.mp4",
        )
        rejected = (
            "file:///C:/private/video.mp4",
            "C:\\private\\video.mp4",
            "base64://123",
            "http://127.0.0.1/video",
            "http://[::1]/video",
            "http://10.0.0.1/video",
            "http://169.254.169.254/latest",
            "http://[::ffff:127.0.0.1]/video",
            "http://224.0.0.1/video",
            "http://localhost/video",
            "http://2130706433/video",
            "https://user:password@video.example/file",
            "https://video.example:9000/file",
            "https://video.example\\@127.0.0.1/file",
            "https://video.example/\nfile",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(VideoContextError):
                validate_video_url(url)

    async def test_dns_results_are_checked_and_pinned_including_mixed_answers(self):
        resolver = PublicVideoResolver()
        public = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
        private = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("192.168.1.2", 443),
        )
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[public])):
            records = await resolver.resolve("video.example", 443)
            self.assertEqual(records[0]["host"], "93.184.216.34")
            self.assertEqual(records[0]["flags"], socket.AI_NUMERICHOST)
        for addresses in ([private], [public, private]):
            with (
                patch.object(loop, "getaddrinfo", AsyncMock(return_value=addresses)),
                self.assertRaises(VideoContextError),
            ):
                await resolver.resolve("video.example", 443)

    async def _download(self, responses, limit=8):
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.get = unittest.mock.Mock(side_effect=responses)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video.bin"
            with (
                patch(
                    "astrbot_plugin_local_codex.video_context.aiohttp.ClientSession",
                    return_value=session,
                ),
                patch("astrbot_plugin_local_codex.video_context.aiohttp.TCPConnector"),
            ):
                await download_video("https://video.example/video", path, limit)
            return path.read_bytes(), session

    async def test_redirect_to_private_endpoint_is_blocked_before_second_request(self):
        response = Response(302, headers={"Location": "http://127.0.0.1/secret"})
        with self.assertRaises(VideoContextError):
            await self._download([response])

    async def test_streaming_download_is_bounded_even_without_content_length(self):
        with self.assertRaises(VideoContextError):
            await self._download([Response(chunks=[b"123456", b"789"])])
        with self.assertRaises(VideoContextError):
            await self._download([Response(length=9)])
        data, session = await self._download([Response(chunks=[b"123", b"456"])])
        self.assertEqual(data, b"123456")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])


class VideoContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.context = VideoContext({"video_summary_enabled": True}, self.directory)
        self.provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text="画面中有红色方块。")
            )
        )
        self.video = Video(file="attachment.mp4", url="https://video.example/clip.mp4")

        async def download(url, destination, maximum_bytes):
            destination.write_bytes(b"fictional-video")

        async def frames(video, directory):
            image = directory / "frame-01.jpg"
            image.write_bytes(b"fictional-image")
            return [image], 3.0, False

        self.download = patch(
            "astrbot_plugin_local_codex.video_context.download_video",
            side_effect=download,
        )
        self.download_mock = self.download.start()
        self.addCleanup(self.download.stop)
        self.context._frames = AsyncMock(side_effect=frames)

    async def test_video_summary_requires_explicit_enablement(self):
        context = VideoContext({}, self.directory)
        self.assertIsNone(
            await context.summarize(event_for([self.video]), self.provider)
        )
        self.download_mock.assert_not_awaited()
        self.provider.text_chat.assert_not_awaited()

    async def test_text_urls_do_not_become_attachments_and_local_paths_are_never_opened(
        self,
    ):
        event = event_for([Plain("分析 https://video.example/clip.mp4")])
        self.assertIsNone(await self.context.summarize(event, self.provider))
        for video in (
            Video(file="C:\\private\\video.mp4"),
            Video(file="file:///C:/private/video.mp4"),
        ):
            with self.assertRaises(VideoContextError):
                await self.context.summarize(event_for([video]), self.provider)
        self.download_mock.assert_not_called()
        self.provider.text_chat.assert_not_called()

    async def test_visual_summary_uses_owned_frames_without_tools_and_cleans_temporary_files(
        self,
    ):
        summary = await self.context.summarize(event_for([self.video]), self.provider)
        call = self.provider.text_chat.call_args.kwargs
        self.assertIsNone(call["func_tool"])
        self.assertEqual(call["tool_choice"], "none")
        self.assertEqual(call["contexts"], [])
        self.assertTrue(all(not Path(path).exists() for path in call["image_urls"]))
        self.assertIn("无音频", summary.text)
        self.assertNotIn("https://", summary.text)
        self.assertEqual(summary.source_qq, "123")
        self.assertFalse(summary.quoted)

    async def test_provider_failure_is_explicit_and_still_cleans_frames(self):
        self.provider.text_chat.side_effect = RuntimeError("fixture provider failure")
        with self.assertRaisesRegex(VideoContextError, "Codex"):
            await self.context.summarize(event_for([self.video]), self.provider)
        self.assertEqual(list(self.context.data_dir.iterdir()), [])
        self.assertEqual(len(self.context.cache), 0)

    async def test_quoted_video_is_verified_against_current_group_and_uses_original_sender(
        self,
    ):
        quoted = Reply(id="42", chain=[self.video], sender_id="999")
        event = event_for([quoted])
        raw = {
            "message_type": "group",
            "group_id": "456",
            "sender": {"user_id": "999", "nickname": "Beta"},
            "message": [
                {
                    "type": "video",
                    "data": {"file": self.video.file, "url": self.video.url},
                }
            ],
        }
        event.bot = SimpleNamespace(call_action=AsyncMock(return_value=raw))
        summary = await self.context.summarize(event, self.provider)
        self.assertEqual(
            (summary.source_qq, summary.source_name, summary.quoted),
            ("999", "Beta", True),
        )
        self.assertEqual(event.bot.call_action.call_args.kwargs["message_id"], 42)
        raw["group_id"] = "789"
        with self.assertRaises(VideoContextError):
            await self.context.summarize(event, self.provider)
        self.assertEqual(self.provider.text_chat.await_count, 1)

    async def test_private_quote_cannot_import_another_users_or_groups_video(self):
        event = event_for([Reply(id="42", chain=[self.video])], group="")
        raw = {"message_type": "private", "sender": {"user_id": "999"}}
        event.bot = SimpleNamespace(call_action=AsyncMock(return_value=raw))
        with self.assertRaises(VideoContextError):
            await self.context.summarize(event, self.provider)
        raw.update(message_type="group", group_id="456")
        with self.assertRaises(VideoContextError):
            await self.context.summarize(event, self.provider)

    async def test_content_deduplication_is_scoped_by_platform_and_group_or_private_sender(
        self,
    ):
        for event in (
            event_for([self.video]),
            event_for([self.video], sender="999"),
            event_for([self.video], group="789"),
            event_for([self.video], group=""),
            event_for([self.video], platform="another"),
        ):
            await self.context.summarize(event, self.provider)
        self.assertEqual(self.provider.text_chat.await_count, 4)
        self.assertEqual(len(self.context.cache), 4)

    async def test_summary_replaces_video_without_modifying_original_or_unrelated_components(
        self,
    ):
        mention, question, image = (
            At(qq="321"),
            Plain("这是怎么回事？"),
            Image.fromURL("https://image.example/photo.png"),
        )
        original_reply = Reply(
            id="42", chain=[Plain("原视频标题"), self.video], sender_id="999"
        )
        nested = Nodes(
            nodes=[Node(content=[self.video, image], name="Beta", uin="999")]
        )
        original = [mention, question, self.video, original_reply, nested]
        event = event_for(original)
        original_message = event.message_obj
        summary = await self.context.summarize(event, self.provider)
        self.assertTrue(apply_summary(event, summary))
        self.assertIs(event.get_messages()[0], mention)
        self.assertIs(event.get_messages()[1], question)
        self.assertIs(
            event.get_messages()[4].nodes[0].content[1], nested.nodes[0].content[1]
        )
        self.assertIs(event.message_obj.raw_message, original_message.raw_message)
        self.assertIs(original_message.message, original)
        self.assertIsInstance(original_reply.chain[1], Video)
        self.assertIsInstance(event.get_messages()[2], Plain)
        self.assertIsInstance(event.get_messages()[3].chain[1], Plain)
        self.assertIsInstance(event.get_messages()[4].nodes[0].content[0], Plain)
        self.assertIn("请看看这个视频", event.message_str)
        self.assertIn('"source_qq": "123"', event.message_str)
        self.assertNotIn(self.video.url, event.message_str)

    async def test_failure_strips_nested_video_and_preserves_question_without_faking_summary(
        self,
    ):
        event = event_for([Plain("问题仍然保留"), Reply(id="42", chain=[self.video])])
        self.assertTrue(apply_summary(event, None, error="附件已过期，请重新发送。"))
        self.assertEqual(event.get_messages()[0].text, "问题仍然保留")
        self.assertIsInstance(event.get_messages()[1].chain[0], Plain)
        self.assertIn("不能声称看过", event.message_str)
        self.assertIn("附件已过期", event.message_str)
        self.assertFalse(apply_summary(event, None, error="duplicate"))

    async def test_forwarded_video_is_explicitly_rejected_and_can_be_sanitized(self):
        event = event_for([Nodes(nodes=[Node(content=[self.video])])])
        with self.assertRaises(VideoContextError):
            await self.context.summarize(event, self.provider)
        self.assertTrue(apply_summary(event, None, error="请单独发送视频。"))
        self.assertIsInstance(event.get_messages()[0].nodes[0].content[0], Plain)


class RealVideoExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_mp4_is_clipped_and_decoded_to_bounded_frames(self):
        ffmpeg = Path(sys.executable).parent / "ffmpeg.exe"
        if not ffmpeg.is_file():
            found = shutil.which("ffmpeg")
            if not found:
                self.skipTest("ffmpeg is required for the real media fixture")
            ffmpeg = Path(found)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            video = directory / "source.mp4"
            await asyncio.to_thread(
                subprocess.run,
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=960x540:rate=10",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "2",
                    str(video),
                ],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            context = VideoContext(
                {"video_ffmpeg_path": str(ffmpeg), "video_max_seconds": 1}, directory
            )
            frames, seconds, clipped = await context._frames(video, directory)
            self.assertEqual(len(frames), 6)
            self.assertEqual(seconds, 1)
            self.assertTrue(clipped)
            self.assertTrue(
                all(path.read_bytes().startswith(b"\xff\xd8") for path in frames)
            )
            unsafe = directory / "external.m3u8"
            unsafe.write_text(
                "#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1/private\n", encoding="utf-8"
            )
            with self.assertRaises(VideoContextError):
                await context._frames(unsafe, directory)
