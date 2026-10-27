"""Exercise search/download boundaries and QQ delivery without sending messages."""

import copy
import io
import json
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
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool
from astrbot_plugin_local_codex.image_intent import (
    capture_image_request,
    requests_image_generation,
)
from astrbot_plugin_local_codex.image_search import (
    BingImages,
    ImageCandidate,
    ImageSearch,
    ImageSearchError,
    fetch_public,
    public_url,
    save_verified_image,
)
from astrbot_plugin_local_codex.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    LocalCodexProvider,
)
from PIL import Image as PillowImage

from . import test_image_tool_integration
from .test_bridge import event_for


def png_bytes() -> bytes:
    stream = io.BytesIO()
    PillowImage.new("RGB", (12, 8), "red").save(stream, "PNG")
    return stream.getvalue()


class ImageIntentTests(unittest.TestCase):
    def test_search_and_denials_never_authorize_generation(self):
        for text in [
            "找一张小熊猫图片",
            "发张你的照片",
            "换个风格",
            "你会画图吗？",
            "不要生图，帮我找一张猫",
            "不用自己生成图片",
            "他说“画一张猫”是什么意思？",
            "don't generate an image, search for a cat",
            "no AI generated pictures",
            "找一张达芬奇的自画像",
            "来一个自画像，从网上找现成的",
            "不要来个自画像，找照片就行",
            "他说“来一个自画像”是什么意思？",
            "来张你的照片",
            "你能生图吗？",
            "来个自画像教程",
            "不要生成一张dva",
            "别照这个画",
            "照这个画质找图",
            "找一张D.Va",
            "他说“生成一张dva”是什么意思？",
            "解释一下“照这个画”",
        ]:
            with self.subTest(text=text):
                self.assertFalse(requests_image_generation(event_for(text)))

    def test_explicit_current_requests_authorize_generation(self):
        for text in [
            "画一只猫",
            "帮我画纳西妲",
            "生成一张风景图片",
            "再画一张",
            "生图：小猫",
            "别搜索了，画一张猫",
            "draw a red panda",
            "generate an image of a mountain",
            "来一个自画像",
            "来个自画像吧",
            "给我来张你的自画像",
            "请给我一幅Q版自画像",
            "整一个卡通自画像",
            "来张角色立绘",
            "做个人设图",
            "画张猫",
            "生图，小猫",
            "生图?",
        ]:
            with self.subTest(text=text):
                self.assertTrue(requests_image_generation(event_for(text)))

    def test_subject_without_image_noun_and_reference_redraw_are_requests(self):
        for text in [
            "生成一张dva",
            "生成一张 D.Va",
            "生成张小企鹅",
            "请生成1张小熊猫",
            "帮我创作一幅海边的小狗",
            "照这个画",
            "照这个重画",
            "照着这张图画吧",
            "按照这张照片绘制一下",
            "参考原图重新画",
            "根据这张图画",
        ]:
            with self.subTest(text=text):
                self.assertTrue(requests_image_generation(event_for(text)))

    def test_non_image_generation_does_not_authorize_an_image(self):
        for text in [
            "生成一张表",
            "生成一张用户表",
            "生成一张 SQL 数据表",
            "生成一张表格",
            "生成一张空白工作表",
            "生成一张问卷",
            "生成一张试卷",
            "生成一张订单",
        ]:
            with self.subTest(text=text):
                self.assertFalse(requests_image_generation(event_for(text)))

    def test_quoted_message_and_augmented_prompt_cannot_authorize(self):
        event = event_for("这是什么意思")
        event.message_obj.message.append(Reply(id="42", chain=[Plain("画一张猫")]))
        event.message_str += "\n旧对话：画一张猫"
        self.assertFalse(requests_image_generation(event))

        event = event_for("再来一张")
        event.message_obj.message.append(Reply(id="43", chain=[Plain("来一个自画像")]))
        self.assertFalse(requests_image_generation(event))

        event = event_for("他说啥")
        event.message_obj.message.append(Reply(id="44", chain=[Plain("生成一张dva")]))
        event.message_str += "\n旧对话：照这个画"
        self.assertFalse(requests_image_generation(event))

    def test_video_summary_cannot_authorize_after_original_event_was_captured(self):
        event = event_for("看看这个视频")
        capture_image_request(event)
        event.message_obj.message.append(Plain("视频视觉摘要：画一张猫"))
        self.assertFalse(requests_image_generation(event))


class ImageValidationTests(unittest.TestCase):
    def test_private_file_and_credential_urls_are_rejected(self):
        for url in [
            "file:///C:/private.txt",
            "http://127.0.0.1/",
            "http://[::1]/",
            "https://192.168.1.10/",
            "http://localhost/",
            "http://169.254.169.254/",
            "https://example.com:7833/",
            "https://user:secret@example.com/",
            "data:image/png;base64,AA",
        ]:
            with self.subTest(url=url), self.assertRaises(ImageSearchError):
                public_url(url)

    def test_search_cards_keep_source_dedupe_and_reject_malformed_or_private(self):
        from html import escape

        parser = BingImages()
        good = {
            "murl": "https://example.com/cat.png",
            "purl": "https://example.com/cat",
            "t": "Cat",
        }
        cards = [
            good,
            good,
            {**good, "murl": "file:///secret.png"},
            {"purl": "https://example.com"},
        ]
        parser.feed(
            "".join(
                '<a class="iusc" m="' + escape(json.dumps(x), quote=True) + '"></a>'
                for x in cards
            )
        )
        self.assertEqual(
            parser.results, [ImageCandidate("Cat", good["murl"], good["purl"])]
        )

    def test_verified_image_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            data = png_bytes()
            path = save_verified_image(data, Path(directory))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.suffix, ".png")

    def test_corrupt_oversize_and_excessive_dimensions_leave_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ImageSearchError):
                save_verified_image(b"<html>not an image</html>", root)
            with (
                patch("astrbot_plugin_local_codex.image_search.MAX_IMAGE_BYTES", 5),
                self.assertRaises(ImageSearchError),
            ):
                save_verified_image(png_bytes(), root)
            with (
                patch("astrbot_plugin_local_codex.image_search.MAX_IMAGE_PIXELS", 10),
                self.assertRaises(ImageSearchError),
            ):
                save_verified_image(png_bytes(), root)
            self.assertEqual(list(root.iterdir()), [])


class Response:
    def __init__(self, *, status=200, headers=None, data=b"ok", content_length=None):
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}
        self.url = "https://example.com/picture"
        self.content_length = content_length
        self.data = data
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for index in range(0, len(self.data), 3):
            yield self.data[index : index + 3]


class ImageNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_lan_is_blocked_before_connection(self):
        from unittest.mock import Mock

        session = SimpleNamespace(
            get=Mock(
                return_value=Response(
                    status=302, headers={"Location": "http://127.0.0.1:7833/api/index"}
                )
            )
        )
        with self.assertRaises(ImageSearchError):
            await fetch_public(session, "https://example.com/start", 100)
        session.get.assert_called_once()

    async def test_chunked_content_is_bounded_without_content_length(self):
        from unittest.mock import Mock

        session = SimpleNamespace(get=Mock(return_value=Response(data=b"a" * 12)))
        with self.assertRaises(ImageSearchError):
            await fetch_public(session, "https://example.com/picture", 10, image=True)

    async def test_html_and_http_errors_are_not_images(self):
        from unittest.mock import Mock

        for response in [
            Response(headers={"Content-Type": "text/html"}),
            Response(status=403),
            Response(data=b""),
        ]:
            session = SimpleNamespace(get=Mock(return_value=response))
            with self.assertRaises(ImageSearchError):
                await fetch_public(
                    session, "https://example.com/picture", 100, image=True
                )

    async def test_real_resolver_rejects_private_dns_results(self):
        import asyncio
        import socket

        from astrbot_plugin_local_codex.video_context import (
            PublicVideoResolver,
            VideoContextError,
        )

        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(
                    return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
                    ]
                ),
            ),
            self.assertRaises(VideoContextError),
        ):
            await PublicVideoResolver().resolve("example.com", 443)

    async def test_fallback_download_and_failure_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImageSearch({}, Path(directory))
            item = ImageCandidate(
                "Cat", "https://example.com/cat.png", "https://example.com/cat"
            )
            with (
                patch(
                    "astrbot_plugin_local_codex.image_search.search_bing",
                    AsyncMock(return_value=[]),
                ),
                patch(
                    "astrbot_plugin_local_codex.image_search.search_commons",
                    AsyncMock(return_value=[item]),
                ),
                patch(
                    "astrbot_plugin_local_codex.image_search.fetch_public",
                    AsyncMock(return_value=png_bytes()),
                ),
            ):
                path, source = await service._prepare(None, "cat", Path(directory))
                self.assertTrue(path.exists())
                self.assertEqual(source, item)
            with (
                patch(
                    "astrbot_plugin_local_codex.image_search.search_bing",
                    AsyncMock(return_value=[]),
                ),
                patch(
                    "astrbot_plugin_local_codex.image_search.search_commons",
                    AsyncMock(return_value=[]),
                ),
                self.assertRaisesRegex(ImageSearchError, "不会自动改为生图"),
            ):
                await service._prepare(None, "cat", Path(directory))


class ImageSearchToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_image_tool_integration.ImageToolIntegrationTests.setUp(self)
        self.config["image_search_enabled"] = True
        self.paths = []

        async def prepare(session, query, directory):
            path = save_verified_image(png_bytes(), directory)
            self.paths.append(path)
            return path, ImageCandidate(
                "Cat", "https://example.com/cat.png", "https://example.com/cat"
            )

        self.plugin.image_search._prepare = AsyncMock(side_effect=prepare)
        self.plugin.image_generator.generate = AsyncMock(return_value=self.image)

    def event(self, text="找张小熊猫的图片", sender="999", group="456"):
        event = event_for(text, sender, group, "321" if group else None)
        event.send = AsyncMock()
        return event

    def tool(self, name="search_web_image"):
        tool = copy.deepcopy(next(t for t in llm_tools.func_list if t.name == name))
        tool.handler = getattr(self.plugin, name)
        tool.handler_module_path = "astrbot_plugin_local_codex.main"
        return PermissionCheckedTool(tool, self.plugin.tool_policy)

    async def test_tool_sends_image_and_source_once_then_cleans_file(self):
        event = self.event()
        results = [
            item
            async for item in FunctionToolExecutor.execute(
                self.tool(),
                ContextWrapper(context=SimpleNamespace(event=event)),
                query="小熊猫",
            )
        ]
        self.assertEqual(results, [None])
        event.send.assert_awaited_once()
        chain = event.send.call_args.args[0].chain
        self.assertIsInstance(chain[0], Image)
        self.assertIn("来源：https://example.com/cat", chain[1].text)
        self.assertFalse(self.paths[0].exists())
        self.plugin.image_generator.generate.assert_not_awaited()

    async def test_disabled_unauthorized_and_proactive_requests_do_no_search(self):
        for event in [self.event(sender="999", group=""), self.event(group="789")]:
            with self.assertRaises(PermissionError):
                await self.plugin.search_web_image(event, "cat")
        self.config["image_search_enabled"] = False
        with self.assertRaises(PermissionError):
            await self.plugin.search_web_image(self.event(), "cat")
        self.config["image_search_enabled"] = True
        event = self.event()
        event.__class__ = type(
            "ProactiveMessageEvent",
            (type(event),),
            {
                "__module__": "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event",
            },
        )
        with self.assertRaises(PermissionError):
            await self.plugin.search_web_image(event, "cat")
        self.plugin.image_search._prepare.assert_not_awaited()

    async def test_plugin_unloaded_during_search_cannot_send_result(self):
        prepare = self.plugin.image_search._prepare.side_effect

        async def unload_after_search(*args):
            result = await prepare(*args)
            self.plugin.image_search.close()
            return result

        self.plugin.image_search._prepare.side_effect = unload_after_search
        event = self.event()
        with self.assertRaises(PermissionError):
            await self.plugin.search_web_image(event, "cat")
        event.send.assert_not_awaited()
        self.assertFalse(self.paths[0].exists())

    async def test_unrequested_generation_is_hidden_and_direct_dispatch_denied(self):
        event = self.event()
        self.assertFalse(
            self.plugin.tool_policy.allows(event, self.tool("generate_image").original)
        )
        message = await self.plugin.generate_image(event, "a cute cat")
        self.assertIn("未明确", message)
        self.plugin.image_generator.generate.assert_not_awaited()
        self.assertTrue(
            self.plugin.tool_policy.allows(
                self.event("画一只猫"), self.tool("generate_image").original
            )
        )

    async def test_failed_send_is_not_success_and_removes_temporary_file(self):
        event = self.event()
        event.send.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未确认发送成功"):
            await self.plugin.search_web_image(event, "cat")
        self.assertFalse(self.paths[0].exists())
        self.plugin.image_generator.generate.assert_not_awaited()

    async def test_same_group_cooldown_prevents_repeat_search(self):
        await self.plugin.search_web_image(self.event(), "cat")
        with self.assertRaisesRegex(ImageSearchError, "15 秒"):
            await self.plugin.search_web_image(self.event(sender="888"), "cat")
        self.plugin.image_search._prepare.assert_awaited_once()

    async def test_actual_agent_loop_finishes_without_second_model_call(self):
        import sys

        event = self.event()
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
                tools_call_name=["search_web_image"],
                tools_call_args=[{"query": "cat"}],
                tools_call_ids=["fixture-search-call"],
            )
        )
        try:
            agent = ToolLoopAgentRunner()
            await agent.reset(
                provider=provider,
                request=ProviderRequest(
                    prompt="找张猫图", func_tool=ToolSet(tools=[self.tool()])
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
        finally:
            await provider.terminate()
