"""Check Douyin authorization, parser reuse and delivery without real QQ sends."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Plain, Reply, Video
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool
from astrbot_plugin_local_codex.douyin_video import (
    DouyinVideoError,
    copy_parser_video,
    direct_douyin_links,
    normalize_douyin_url,
)
from astrbot_plugin_local_codex.music_routing import MusicRouting

from . import test_image_tool_integration
from .test_bridge import event_for
from .test_music_routing import handler, music_event

URL = "https://www.douyin.com/video/1234567890123456789"
OTHER_URL = "https://www.douyin.com/video/1234567890123456790"
PARSER_MODULE = "data.plugins.astrbot_plugin_parser.main"


@dataclass
class VideoContent:
    path_task: Path | asyncio.Task
    cover: asyncio.Task | None = None

    async def get_path(self):
        if isinstance(self.path_task, asyncio.Task):
            self.path_task = await self.path_task
        return self.path_task


@dataclass
class ParseResult:
    contents: list
    platform: object
    title: str = "Public fixture title"
    author: object = None


class DouyinUrlTests(unittest.TestCase):
    def test_normalize_video_and_share_urls_without_tracking(self):
        self.assertEqual(normalize_douyin_url(URL + "?source=fixture#section"), URL)
        self.assertEqual(
            normalize_douyin_url(URL.replace("https://www.", "http://")), URL
        )
        self.assertEqual(
            normalize_douyin_url("https://v.douyin.com/AbC_123?tracking=1"),
            "https://v.douyin.com/AbC_123/",
        )
        self.assertEqual(
            normalize_douyin_url(
                "https://www.iesdouyin.com/share/video/1234567890123456789/"
            ),
            URL,
        )

    def test_non_video_and_private_targets_are_rejected(self):
        for value in [
            "file:///C:/secret.mp4",
            "http://127.0.0.1:7833/",
            URL + "/secret",
            "https://www.douyin.com.evil.example/video/1234567890123456789",
            "https://www.douyin.com@evil.example/video/1234567890123456789",
            URL.replace(".com/", ".com:443/"),
            URL.replace("/video/", "/note/"),
            "https://www.douyin.com/search/cat",
            "https://live.douyin.com/123456",
            "https://www.douyin.com\\@evil.example/video/1234567890123456789",
        ]:
            with self.subTest(value=value), self.assertRaises(DouyinVideoError):
                normalize_douyin_url(value)

    def test_quotes_cannot_become_direct_sender_links(self):
        event = event_for("看这个")
        event.message_obj.message.append(Reply(id="5", chain=[Plain(URL)]))
        self.assertEqual(direct_douyin_links(event), [])

    def test_copy_rejects_outside_cache_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            source = root / "private.mp4"
            source.write_bytes(b"private")
            with self.assertRaises(DouyinVideoError):
                copy_parser_video(source, cache, root / "out.mp4")
            self.assertFalse((root / "out.mp4").exists())
            source = cache / "movie.mp4"
            source.write_bytes(b"0123456789")
            with (
                patch("astrbot_plugin_local_codex.douyin_video.MAX_VIDEO_BYTES", 5),
                self.assertRaises(DouyinVideoError),
            ):
                copy_parser_video(source, cache, root / "out.mp4")


class DouyinDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_image_tool_integration.ImageToolIntegrationTests.setUp(self)
        self.config["douyin_video_enabled"] = True
        self.cache = self.directory / "parser-cache"
        self.cache.mkdir()
        self.source = self.cache / "fixture.mp4"
        self.source.write_bytes(b"isolated video fixture; no upload")
        self.module = ModuleType(PARSER_MODULE)
        self.module.ParserPlugin = type("ParserPlugin", (), {})
        self.parser = self.module.ParserPlugin()
        self.parser.cfg = SimpleNamespace(
            parser=SimpleNamespace(douyin=SimpleNamespace(enable=True)),
            cache_dir=self.cache,
            whitelist=[],
            blacklist=[],
            max_duration=900,
        )
        self.parser.parser_map = {"douyin": object()}
        self.parser.parse_url = AsyncMock(
            return_value=ParseResult(
                [VideoContent(self.source)],
                SimpleNamespace(name="douyin"),
            )
        )
        self.parser_star = SimpleNamespace(
            module_path=PARSER_MODULE,
            activated=True,
            reserved=False,
            star_cls=self.parser,
        )
        self.context.get_registered_star = lambda name: self.parser_star
        modules = patch.dict(sys.modules, {PARSER_MODULE: self.module})
        modules.start()
        self.addCleanup(modules.stop)
        self.probe = patch(
            "astrbot_plugin_local_codex.douyin_video._run_media",
            AsyncMock(
                return_value=json.dumps(
                    {"format": {"duration": "8"}, "streams": [{"codec_type": "video"}]}
                ).encode()
            ),
        )
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)
        self.plugin.video_context._executables = Mock(
            return_value=("fixture-ffmpeg", "fixture-ffprobe")
        )
        self.plugin.group_context.observe_parsed_link = Mock()

    def event(self, text="从抖音找一个小熊猫视频", sender="999", group="456"):
        event = event_for(text, sender, group, "321" if group else None)
        event.send = AsyncMock()
        return event

    def tool(self):
        tool = copy.deepcopy(
            next(t for t in llm_tools.func_list if t.name == "send_douyin_video")
        )
        tool.handler = self.plugin.send_douyin_video
        tool.handler_module_path = "astrbot_plugin_local_codex.main"
        return PermissionCheckedTool(tool, self.plugin.tool_policy)

    async def test_registered_tool_delivers_current_video_and_source_then_cleans_copy(
        self,
    ):
        event = self.event()
        sent_paths = []

        async def sent(result):
            chain = result.chain
            self.assertIsInstance(chain[0], Video)
            # The component's file field is a file URI for a verified local copy.
            self.assertIn("video.mp4", chain[0].file)
            self.assertIn(URL, chain[1].text)
            sent_paths.extend(
                self.plugin.douyin_video.data_dir.glob("send-*/video.mp4")
            )
            self.assertEqual(len(sent_paths), 1)
            self.assertEqual(sent_paths[0].read_bytes(), self.source.read_bytes())

        event.send.side_effect = sent
        result = [
            value
            async for value in FunctionToolExecutor.execute(
                self.tool(),
                ContextWrapper(context=SimpleNamespace(event=event)),
                url=URL,
            )
        ]
        self.assertEqual(result, [None])
        self.parser.parse_url.assert_awaited_once_with(URL)
        self.assertFalse(sent_paths[0].exists())
        self.assertTrue(self.source.exists())
        self.plugin.group_context.observe_parsed_link.assert_called_once()

    async def test_invalid_or_changed_exact_source_never_calls_parser(self):
        for event, url in [
            (self.event(), "http://127.0.0.1/"),
            (self.event("发这个 " + URL), OTHER_URL),
        ]:
            with self.assertRaises(DouyinVideoError):
                await self.plugin.send_douyin_video(event, url)
            event.send.assert_not_awaited()
        self.parser.parse_url.assert_not_awaited()

    async def test_authorization_and_disabled_features_fail_before_network(self):
        for event in [self.event(sender="999", group=""), self.event(group="789")]:
            with self.assertRaises(PermissionError):
                await self.plugin.send_douyin_video(event, URL)
        self.config["douyin_video_enabled"] = False
        with self.assertRaises(PermissionError):
            await self.plugin.send_douyin_video(self.event(), URL)
        self.config["douyin_video_enabled"] = True
        self.parser.cfg.parser.douyin.enable = False
        with self.assertRaises(DouyinVideoError):
            await self.plugin.send_douyin_video(self.event(), URL)
        self.parser.parse_url.assert_not_awaited()

    async def test_parser_session_restrictions_are_preserved(self):
        event = self.event()
        self.parser.cfg.whitelist = ["some-other-session"]
        with self.assertRaises(PermissionError):
            await self.plugin.send_douyin_video(event, URL)
        self.parser.cfg.whitelist = []
        self.parser.cfg.blacklist = [event.unified_msg_origin]
        with self.assertRaises(PermissionError):
            await self.plugin.send_douyin_video(event, URL)
        self.parser.parse_url.assert_not_awaited()

    async def test_non_video_result_cannot_report_success(self):
        self.parser.parse_url.return_value.contents = []
        event = self.event()
        with self.assertRaisesRegex(DouyinVideoError, "没有可发送"):
            await self.plugin.send_douyin_video(event, URL)
        event.send.assert_not_awaited()

    async def test_outside_cache_and_invalid_video_never_send(self):
        self.parser.parse_url.return_value.contents = [VideoContent(self.image)]
        event = self.event()
        with self.assertRaises(DouyinVideoError):
            await self.plugin.send_douyin_video(event, URL)
        event.send.assert_not_awaited()

    async def test_audio_only_and_long_files_never_send(self):
        event = self.event()
        self.probe_mock.return_value = json.dumps(
            {"format": {"duration": "8"}, "streams": [{"codec_type": "audio"}]}
        ).encode()
        with self.assertRaisesRegex(DouyinVideoError, "不是有效视频"):
            await self.plugin.send_douyin_video(event, URL)
        event.send.assert_not_awaited()
        self.plugin.douyin_video.last_started.clear()
        self.probe_mock.return_value = json.dumps(
            {"format": {"duration": "901"}, "streams": [{"codec_type": "video"}]}
        ).encode()
        with self.assertRaises(DouyinVideoError):
            await self.plugin.send_douyin_video(event, URL)
        event.send.assert_not_awaited()

    async def test_failed_send_does_not_update_group_context(self):
        event = self.event()
        event.send.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未确认发送成功"):
            await self.plugin.send_douyin_video(event, URL)
        self.plugin.group_context.observe_parsed_link.assert_not_called()
        self.assertEqual(list(self.plugin.douyin_video.data_dir.iterdir()), [])

    async def test_reload_during_download_revokes_delivery(self):
        result = self.parser.parse_url.return_value

        async def reload(url):
            self.parser_star.activated = False
            return result

        self.parser.parse_url.side_effect = reload
        event = self.event()
        with self.assertRaises(DouyinVideoError):
            await self.plugin.send_douyin_video(event, URL)
        event.send.assert_not_awaited()

    async def test_parser_error_hides_signed_urls_and_credentials(self):
        self.parser.parse_url.side_effect = RuntimeError(
            "SECRET_COOKIE fixture/path?token=fixture"
        )
        with self.assertRaises(DouyinVideoError) as caught:
            await self.plugin.send_douyin_video(self.event(), URL)
        self.assertNotIn("SECRET_COOKIE", str(caught.exception))
        self.assertNotIn("token=", str(caught.exception))

    async def test_unneeded_cover_task_is_cancelled_and_collected(self):
        task = asyncio.create_task(asyncio.sleep(3600))
        self.parser.parse_url.return_value.contents[0].cover = task
        await self.plugin.send_douyin_video(self.event(), URL)
        self.assertTrue(task.cancelled())

    async def test_proactive_event_does_not_download(self):
        event = self.event()
        event.__class__ = type(
            "ProactiveMessageEvent",
            (type(event),),
            {
                "__module__": "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event",
            },
        )
        with self.assertRaises(PermissionError):
            await self.plugin.send_douyin_video(event, URL)
        self.parser.parse_url.assert_not_awaited()


class DouyinRoutingTests(unittest.TestCase):
    def test_direct_mention_removes_only_parser_to_avoid_duplicate_video(self):
        event = music_event("发这个视频 " + URL)
        other = handler(module="another_plugin.main")
        handlers = event.get_extra("activated_handlers")
        handlers.append(other)
        self.assertTrue(MusicRouting().route(event, douyin_video=True))
        self.assertIs(handlers, event.get_extra("activated_handlers"))
        self.assertEqual(handlers, [other])

    def test_disabled_unmentioned_and_other_platform_links_keep_auto_parser(self):
        for event, enabled in [
            (music_event(URL), False),
            (music_event(URL, mention=None), True),
            (music_event("https://example.com/video/123456"), True),
        ]:
            self.assertFalse(MusicRouting().route(event, douyin_video=enabled))
            self.assertEqual(len(event.get_extra("activated_handlers")), 1)
