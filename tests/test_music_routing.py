"""Route authorized music links without swallowing other plugin handlers."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.message.components import At, Json, Node, Nodes, Plain, Reply
from astrbot.core.pipeline.process_stage.stage import ProcessStage, StarRequestSubStage
from astrbot.core.star.star import star_map
from astrbot_plugin_local_codex.main import LocalCodex
from astrbot_plugin_local_codex.music_routing import MusicRouting

from .test_bridge import event_for

LINK = "https://b23.tv/AbCd123"
LONG_LINK = "https://www.bilibili.com/video/BV1Abcdefghi/"
PARSER = "data.plugins.astrbot_plugin_parser.main"


def handler(module=PARSER, name="on_message", callback=None):
    """Create only the handler metadata used by the actual process stage."""
    return SimpleNamespace(
        handler_module_path=module,
        handler_name=name,
        handler_full_name=module + "_" + name,
        handler=callback or AsyncMock(),
    )


def music_event(text, sender="123", group="456", mention="321"):
    """Use fictional identities and real AstrBot event/component classes."""
    event = event_for(text, sender, group, mention)
    event.set_extra("activated_handlers", [handler()])
    return event


class MusicRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routing = MusicRouting()

    def test_explicit_music_links_remove_only_exact_parser_handler_in_place(self):
        for link in [LINK, LONG_LINK, "https://bilibili.com/video/av12345"]:
            for request in [
                "这个翻唱一下",
                "用你的声音翻唱《测试歌》",
                "播放这首歌",
                "用你的声音唱 生日快乐歌",
                "我要听你翻唱 测试歌",
                "这个 翻唱一下",
            ]:
                with self.subTest(link=link, request=request):
                    event = music_event(link + " " + request)
                    keep = [
                        handler("data.plugins.another.main"),
                        handler(PARSER, "other_handler"),
                        handler("data.plugins.astrbot_plugin_parser.main_fake"),
                    ]
                    original = [keep[0], handler(), keep[1], keep[2]]
                    event.set_extra("activated_handlers", original)
                    self.assertTrue(self.routing.route(event))
                    self.assertIs(event.get_extra("activated_handlers"), original)
                    self.assertEqual(original, keep)
                    self.assertFalse(event._has_send_oper)

    def test_both_exact_parser_module_forms_are_supported(self):
        event = music_event("翻唱一下 " + LINK)
        event.set_extra(
            "activated_handlers", [handler(), handler("astrbot_plugin_parser.main")]
        )
        self.assertTrue(self.routing.route(event))
        self.assertEqual(event.get_extra("activated_handlers"), [])

    def test_music_text_records_intent_and_link_or_share_title_can_follow(self):
        for text in [LINK, "【测试生日歌-哔哩哔哩】 " + LINK]:
            with self.subTest(text=text):
                request = music_event("我想听你唱一首生日歌")
                self.assertFalse(self.routing.route(request))
                self.assertEqual(len(request.get_extra("activated_handlers")), 1)
                self.assertTrue(self.routing.route(music_event(text)))

    def test_followup_requires_same_sender_group_and_platform_instance(self):
        self.routing.route(music_event("帮我翻唱一首生日歌"))
        others = [music_event(LINK, sender="999"), music_event(LINK, group="888")]
        another_platform = music_event(LINK)
        another_platform.platform_meta.name = "another_adapter"
        others.append(another_platform)
        another_instance = music_event(LINK)
        another_instance.platform_meta.id = "another_instance"
        others.append(another_instance)
        private = music_event(LINK, group="", mention=None)
        others.append(private)
        for event in others:
            self.assertFalse(self.routing.route(event))
            self.assertEqual(len(event.get_extra("activated_handlers")), 1)
        self.assertTrue(self.routing.route(music_event(LINK)))

    def test_followup_expires_after_five_minutes_without_refresh_from_link(self):
        with patch("astrbot_plugin_local_codex.music_routing.time.monotonic") as clock:
            clock.return_value = 100
            self.routing.route(music_event("帮我翻唱一首歌"))
            clock.return_value = 399
            self.assertTrue(self.routing.route(music_event(LINK)))
            clock.return_value = 401
            self.assertFalse(self.routing.route(music_event(LINK)))

    def test_pending_intents_are_bounded_and_evict_oldest_sender(self):
        for sender in range(129):
            self.routing.route(music_event("帮我翻唱一首歌", sender=str(1000 + sender)))
        self.assertEqual(len(self.routing._pending), 128)
        self.assertFalse(self.routing.route(music_event(LINK, sender="1000")))
        self.assertTrue(self.routing.route(music_event(LINK, sender="1128")))

    def test_normal_questions_and_negation_keep_parser_even_after_music_intent(self):
        for text in [
            "这个播放不了，为什么？ ",
            "你觉得这个视频怎么样？ ",
            "我想听你评价这个视频 ",
            "这是不是翻唱？ ",
            "不要翻唱一下这个 ",
            "【这是什么？-哔哩哔哩】 ",
        ]:
            with self.subTest(text=text):
                self.routing.route(music_event("帮我翻唱一首歌"))
                event = music_event(text + LINK)
                self.assertFalse(self.routing.route(event))
                self.assertEqual(len(event.get_extra("activated_handlers")), 1)

    def test_invalid_and_foreign_urls_never_remove_parser(self):
        for link in [
            "https://b23.tv.evil.invalid/AbCd123",
            "https://b23.tv@127.0.0.1/AbCd123",
            "https://127.0.0.1/AbCd123",
            "https://b23.tv:8000/AbCd123",
            "https://www.bilibili.com/not-a-video",
            "https://[broken",
            "file:///private/song.wav",
        ]:
            with self.subTest(link=link):
                event = music_event("翻唱一下 " + link)
                self.assertFalse(self.routing.route(event))
                self.assertEqual(len(event.get_extra("activated_handlers")), 1)

    def test_real_bot_mention_is_required_in_groups(self):
        for mention in [None, "999", "all"]:
            event = music_event("翻唱一下 " + LINK, mention=mention)
            self.assertFalse(self.routing.route(event))
        event = music_event("[CQ:at,qq=321] 翻唱一下 " + LINK, mention=None)
        self.assertFalse(self.routing.route(event))
        self.assertFalse(self.routing._pending)

    def test_quoted_forwarded_json_links_do_not_become_current_request_links(self):
        for component in [
            Reply(id="42", chain=[Plain(LINK)]),
            Nodes([Node(uin="999", name="Fictional", content=[Plain(LINK)])]),
            Json(data={"url": LINK}),
        ]:
            event = music_event("翻唱一下")
            event.message_obj.message.append(component)
            self.assertFalse(self.routing.route(event))

    def test_bot_mention_inside_quote_does_not_authorize_message(self):
        event = music_event("翻唱一下 " + LINK, mention=None)
        event.message_obj.message.append(Reply(id="42", chain=[At(qq="321")]))
        self.assertFalse(self.routing.route(event))

    def test_owner_private_scope_is_allowed_after_caller_authorizes_it(self):
        self.assertTrue(self.routing.route(music_event("翻唱一下 " + LINK, group="")))

    def test_existing_send_flag_is_never_reset(self):
        event = music_event("翻唱一下 " + LINK)
        event._has_send_oper = True
        self.assertTrue(self.routing.route(event))
        self.assertTrue(event._has_send_oper)

    async def test_real_process_stages_preserve_neighbours_and_call_model_once(self):
        for use_routing in [False, True]:
            with self.subTest(use_routing=use_routing):
                observed = []
                event = music_event("这个翻唱一下 " + LINK)
                event.is_at_or_wake_command = True

                async def before(current, observed=observed):
                    observed.append("before")

                async def neighbour(current, observed=observed):
                    observed.append("neighbour")

                async def parser(current, observed=observed):
                    observed.append("parser")
                    current._has_send_oper = True  # Simulate a completed preview send.

                async def after(current, observed=observed):
                    observed.append("after")

                class AgentStage:
                    async def process(self, current, observed=observed):
                        observed.append("model")
                        yield

                plugin = LocalCodex.__new__(LocalCodex)
                plugin.config = {
                    "owner_qq": "123",
                    "group_qq": "456",
                    "integrate_plugins": True,
                }
                plugin.group_context = SimpleNamespace(
                    observe=lambda current, observed=observed: observed.append("bridge")
                )
                plugin.music_routing = (
                    self.routing
                    if use_routing
                    else SimpleNamespace(route=lambda _, **kwargs: False)
                )
                plugin.cover_sources = SimpleNamespace(discard=lambda _: None)
                handlers = [
                    handler("fixture.before", "before", before),
                    handler("fixture.bridge", "chat", plugin.chat),
                    handler("fixture.neighbour", "neighbour", neighbour),
                    handler(callback=parser),
                    handler("fixture.after", "after", after),
                ]
                event.set_extra("activated_handlers", handlers)
                context = SimpleNamespace(astrbot_config=deepcopy(DEFAULT_CONFIG))
                stage = ProcessStage()
                stage.ctx = context
                stage.star_request_sub_stage = StarRequestSubStage()
                await stage.star_request_sub_stage.initialize(context)
                stage.agent_sub_stage = AgentStage()
                with patch.dict(
                    star_map,
                    {
                        item.handler_module_path: SimpleNamespace(
                            name=item.handler_name
                        )
                        for item in handlers
                    },
                ):
                    async for _ in stage.process(event):
                        pass
                self.assertEqual(observed.count("model"), int(use_routing))
                self.assertEqual(observed.count("parser"), int(not use_routing))
                self.assertEqual(
                    [item for item in observed if item not in {"parser", "model"}],
                    ["before", "bridge", "neighbour", "after"],
                )


if __name__ == "__main__":
    unittest.main()
