"""Bridge QQ identity, context, and tools to a configured API or Codex backend."""

import asyncio
import hashlib
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, ProviderType
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import At, Image, Plain, Video
from astrbot.core.star.command_management import list_commands, rename_command
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .access import PermissionCheckedTool, QQToolPolicy, is_allowed
from .api_services import search_public_web
from .backend import provider_id, uses_api, uses_pipeline
from .capabilities import capability_instructions
from .cover_sources import CoverSources
from .douyin_video import DouyinVideo
from .group_context import GroupContext, is_proactive_group
from .image_generator import ImageGenerator
from .image_intent import capture_image_request, requests_image_generation
from .image_search import ImageSearch
from .integration import configure_provider
from .media_cache import MediaCacheCleaner
from .mentions import mentioned_member_instructions
from .music_routing import MusicRouting
from .profile_learning import ProfileLearner
from .profile_reviews import ProfileReviewStore
from .profiles import profile_instructions
from .provider import PROVIDER_ID
from .roleplay import roleplay_instructions
from .runner import CodexRunner
from .singing import SingingService
from .video_context import VideoContext, VideoContextError, apply_summary
from .voice import VoiceService


class LocalCodex(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.tool_policy = QQToolPolicy(context, config)
        self.music_routing = MusicRouting()
        self.profiles = {}
        for profile in config.get("user_profiles", []):
            qq = str(profile.get("qq", "")).strip()
            if not qq:
                continue
            if qq in self.profiles:
                raise ValueError("Each QQ account may have only one user profile.")
            for key in ("tags", "boundaries"):
                if not isinstance(profile.get(key, []), list):
                    raise TypeError(f"User profile {key} must be a list.")
            self.profiles[qq] = profile
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_local_codex")
        self.group_context = GroupContext(
            self.config, self.data_dir / "group_context.json"
        )
        self.video_context = VideoContext(self.config, self.data_dir)
        self.image_generator = ImageGenerator(self.config, self.data_dir, context=context)
        self.image_search = ImageSearch(self.config, self.data_dir)
        self.media_cache = MediaCacheCleaner(
            config,
            Path(get_astrbot_temp_path()),
            self.data_dir / "media_cache_status.json",
        )
        self.douyin_video = DouyinVideo(
            context, config, self.data_dir, self.video_context
        )
        self.cover_sources = CoverSources(context, config, self.data_dir)
        self.singing = SingingService(
            context, config, self.data_dir, source_service=self.cover_sources
        )
        self.voice = VoiceService(context, config, converter=self.singing)
        self.session_file = self.data_dir / "sessions.json"
        self.sessions = (
            json.loads(self.session_file.read_text(encoding="utf-8"))
            if self.session_file.exists()
            else {}
        )
        self.lock = asyncio.Lock()
        self.review_loop = None
        self.profile_reviews = ProfileReviewStore(
            config, self.data_dir, self._refresh_profiles
        )
        self.profile_learner = ProfileLearner(self, self.profile_reviews)
        self.runner = None if uses_api(config) else CodexRunner(
            Path(str(config.get("codex_exe", ""))).expanduser(),
            self.data_dir,
            max(30, min(600, int(config.get("timeout_seconds", 180)))),
            str(config.get("model", "")).strip(),
            fast_mode=bool(config.get("fast_mode", True)),
            reasoning_effort=str(config.get("reasoning_effort", "low")),
            web_search=bool(config.get("web_search", False)),
        )

    async def initialize(self) -> None:
        """Register the optional provider without modifying other plugins."""
        self.review_loop = asyncio.get_running_loop()
        await configure_provider(self.context, self.config, self.data_dir)
        self.media_cache.start()

    def _refresh_profiles(self) -> None:
        """Apply approved preferences immediately to subsequent conversations."""
        self.profiles = {
            str(profile["qq"]).strip(): profile
            for profile in self.config.get("user_profiles", [])
            if str(profile.get("qq", "")).strip()
        }

    def observe_profile_message(self, event: AstrMessageEvent, adapter) -> None:
        """Accept collected events from the active self-learning plugin."""
        if is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            self.profile_learner.observe(event, adapter)

    async def profile_review_api(self, operation: str, **kwargs):
        """Marshal authenticated WebUI requests onto the AstrBot event loop."""

        async def execute():
            if self.profile_learner.closed:
                raise RuntimeError("The Codex bridge has been unloaded.")
            if operation == "list":
                return self.profile_reviews.list(**kwargs)
            if operation == "review":
                return self.profile_reviews.review(**kwargs)
            raise ValueError("Unknown profile review operation.")

        if self.review_loop is None or self.review_loop.is_closed():
            raise RuntimeError("The Codex bridge has not finished initialization.")
        if asyncio.get_running_loop() is self.review_loop:
            return await execute()
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(execute(), self.review_loop)
        )

    @filter.on_astrbot_loaded()
    async def select_provider(self) -> None:
        """Select the configured provider after AstrBot finishes startup."""
        if uses_pipeline(self.config):
            await self.context.provider_manager.set_provider(
                provider_id(self.config), ProviderType.CHAT_COMPLETION
            )

    def _profile_instructions(self, event: AstrMessageEvent) -> str:
        """Build instructions using only the current sender's configured profile.

        Args:
            event: The message identifying the current sender.

        Returns:
            Personalization instructions, or an empty string when unconfigured.
        """
        profile = self.profiles.get(str(event.get_sender_id()))
        if not profile:
            return ""
        return profile_instructions(
            profile, self.config.get("relationship_presets", {})
        )

    @filter.on_llm_request(priority=-100000)
    async def personalize_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Apply sender preferences after other plugin request hooks.

        Args:
            event: Incoming or plugin-generated QQ event.
            req: AstrBot request containing persona, context, and plugin tools.
        """
        if (
            not uses_pipeline(self.config)
            or event.get_platform_name() != "aiocqhttp"
        ):
            return
        # The proactive plugin can generate a group event before any member has
        # spoken. Its trusted event type has no sender; never borrow a member's
        # personal profile for a message addressed to the entire group.
        proactive_group = (
            is_proactive_group(event)
            and bool(str(self.config.get("owner_qq", "")).strip())
            and bool(event.get_group_id())
            and event.get_group_id() == str(self.config.get("group_qq", "")).strip()
        )
        if not proactive_group and not is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            event.stop_event()
            return
        instructions = "" if proactive_group else self._profile_instructions(event)
        anchor = str(self.config.get("persona_anchor", "")).strip()
        if anchor:
            req.system_prompt = (
                (req.system_prompt or "") + "\n角色表达约定：\n" + anchor
            )
        if instructions:
            req.system_prompt = (req.system_prompt or "") + "\n" + instructions
        if not proactive_group:
            req.system_prompt = (
                req.system_prompt or ""
            ) + mentioned_member_instructions(
                event,
                self.config,
                self.profiles,
                self.config.get("relationship_presets", {}),
            )
        req.system_prompt = (req.system_prompt or "") + self.group_context.instructions(
            event, proactive=proactive_group
        )
        req.system_prompt += roleplay_instructions(
            self.config, proactive=proactive_group
        )
        # Public tools need an explicit reviewed allowlist. Never infer computer
        # access from a message, nickname, group role, or a plugin's tool name.
        if req.func_tool:
            req.func_tool = ToolSet(
                tools=[
                    PermissionCheckedTool(tool, self.tool_policy)
                    for tool in req.func_tool
                    if self.tool_policy.allows(event, tool)
                ]
            )
        req.system_prompt += capability_instructions(
            self.config, {tool.name for tool in req.func_tool} if req.func_tool else set()
        )

    @filter.on_plugin_loaded()
    async def configure_new_command(self, plugin: StarMetadata) -> None:
        """Reserve /new using AstrBot's command configuration API.

        Args:
            plugin: Metadata for the plugin that just finished loading.
        """
        builtin_module = "astrbot.builtin_stars.builtin_commands.main"
        if plugin.module_path not in {__name__, builtin_module}:
            return
        for command in await list_commands():
            if command["handler_full_name"] != f"{builtin_module}_new_conv":
                continue
            if (
                command["effective_command"] != "new"
                and "new" not in command["aliases"]
            ):
                continue
            backup = self.data_dir / "builtin-new-command.json"
            if not backup.exists():
                backup.write_text(
                    json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            await rename_command(
                command["handler_full_name"],
                "astrbot_new",
                aliases=[alias for alias in command["aliases"] if alias != "new"],
            )
            logger.info("Local Codex reserved /new; AstrBot's command is /astrbot_new")

    def _accepts_event(self, event: AstrMessageEvent) -> bool:
        """Require an allowed sender and an explicit bot mention in groups.

        Args:
            event: Incoming message event.

        Returns:
            Whether this message is addressed to the Codex bridge.
        """
        return (
            event.get_sender_id() != event.get_self_id()
            and is_allowed(
                self.config,
                event.get_platform_name(),
                event.get_sender_id(),
                event.get_group_id(),
            )
            and (
                not event.get_group_id()
                or any(
                    isinstance(component, At)
                    and str(component.qq) == str(event.get_self_id())
                    for component in event.get_messages()
                )
            )
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE,
        priority=100000,
    )
    async def chat(self, event: AstrMessageEvent):
        """Answer ordinary text while leaving registered commands to their handlers.

        Args:
            event: Incoming private message or group mention.
        """
        capture_image_request(event)
        self.group_context.observe(event)
        accepted = self._accepts_event(event)
        if event.get_sender_id() != event.get_self_id() and is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            self.cover_sources.discard(event)
        if accepted and self.config.get("video_summary_enabled", False):
            await self._prepare_video(event)
        if uses_pipeline(self.config):
            # AstrBot 4.29 stores a suppression flag despite this method's name:
            # ProcessStage enters the default model pipeline when call_llm=False.
            event.should_call_llm(not accepted)
            if accepted:
                self.music_routing.route(
                    event,
                    douyin_video=bool(self.config.get("douyin_video_enabled", False)),
                )
                event.set_extra("selected_provider", provider_id(self.config))
                event.set_extra("enable_streaming", False)
            return
        if not accepted:
            return
        prompt = "".join(
            component.text
            for component in event.get_messages()
            if isinstance(component, Plain)
        ).strip()
        if not prompt or prompt.startswith("/"):
            return
        if any(
            isinstance(handler_filter, CommandFilter)
            for handler in event.get_extra("activated_handlers", [])
            for handler_filter in handler.event_filters
        ):
            return
        async for result in self._ask(event, prompt):
            yield result

    async def _prepare_video(self, event: AstrMessageEvent) -> None:
        """Replace authorized video attachments before the core downloads media."""
        provider = self.context.get_provider_by_id(provider_id(self.config))
        try:
            summary = await self.video_context.summarize(event, provider)
        except VideoContextError as error:
            apply_summary(event, None, error=str(error))
        except Exception:  # noqa: BLE001 - Strip failed media before core fallback.
            logger.exception("Local Codex video summary failed")
            apply_summary(event, None, error="视频摘要生成失败，请稍后重试。")
        else:
            if summary:
                apply_summary(event, summary)
                self.group_context.observe_video_summary(
                    event, summary.text, summary.source_qq, summary.source_name
                )

    @filter.command("codex", priority=100)
    async def codex(self, event: AstrMessageEvent):
        """Ask the local Codex CLI: /codex followed by a question.

        Args:
            event: Incoming command event.
        """
        if not self._accepts_event(event):
            return
        parts = event.get_message_str().strip().split(maxsplit=1)
        if len(parts) != 2:
            event.should_call_llm(True)
            yield event.plain_result("直接发文字即可聊天；群里请 @我。新对话：/new")
            event.stop_event()
            return
        if uses_pipeline(self.config):
            event.should_call_llm(True)
            event.set_extra("selected_provider", provider_id(self.config))
            yield event.request_llm(prompt=parts[1])
            return
        async for result in self._ask(event, parts[1]):
            yield result

    async def _ask(self, event: AstrMessageEvent, prompt: str):
        """Run a turn in the sender's conversation and persist its thread ID.

        Args:
            event: An already authorized incoming message.
            prompt: Text to send to Codex.
        """
        event.should_call_llm(True)
        try:
            if self.lock.locked():
                yield event.plain_result(
                    "Codex 正在处理上一条请求，请等它完成后再发送。"
                )
                return
            async with self.lock:
                key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
                try:
                    instructions = self._profile_instructions(event)
                    instructions += mentioned_member_instructions(
                        event,
                        self.config,
                        self.profiles,
                        self.config.get("relationship_presets", {}),
                    )
                    anchor = str(self.config.get("persona_anchor", "")).strip()
                    if anchor:
                        instructions = anchor + "\n" + instructions
                    instructions += self.group_context.instructions(event)
                    instructions += roleplay_instructions(self.config)
                    if instructions:
                        prompt = instructions + "\n用户消息：\n" + prompt
                    thread_id, answer = await self.runner.run(
                        prompt, self.sessions.get(key)
                    )
                    self.sessions[key] = thread_id
                    temporary = self.session_file.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps(self.sessions, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    temporary.replace(self.session_file)
                except TimeoutError:
                    yield event.plain_result(
                        "Codex 请求超时，已停止本次进程。可以稍后重试。"
                    )
                    return
                except (OSError, RuntimeError, ValueError) as error:
                    logger.exception("Local Codex request failed")
                    yield event.plain_result(
                        f"Codex 调用失败：{error}\n请查看 AstrBot 日志及插件数据目录；未返回成功结果。"
                    )
                    return
                for start in range(0, len(answer), 1500):
                    yield event.plain_result(answer[start : start + 1500])
        finally:
            event.stop_event()

    @filter.command("new", alias={"codex_reset"}, priority=100)
    async def reset(self, event: AstrMessageEvent):
        """Start a new Codex conversation on the next question.

        Args:
            event: Incoming reset command event.
        """
        if not self._accepts_event(event):
            return
        event.should_call_llm(True)
        try:
            if self.lock.locked():
                yield event.plain_result("请等当前 Codex 请求完成后再重置对话。")
                return
            if uses_pipeline(self.config):
                await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin
                )
            key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
            self.sessions.pop(key, None)
            temporary = self.session_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.sessions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.session_file)
            yield event.plain_result("已为你开启新对话。直接发文字即可，群里请 @我。")
        finally:
            event.stop_event()

    @filter.after_message_sent()
    async def remember_group_reply(self, event: AstrMessageEvent) -> None:
        """Keep successful public replies available to other group members."""
        self.group_context.observe_reply(event)

    def observe_parsed_link(self, event: AstrMessageEvent, payload: dict) -> None:
        """Receive public parser metadata only after its sender confirms delivery."""
        self.group_context.observe_parsed_link(event, payload)

    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str):
        """Speak a short greeting or requested voice reply in the current chat.

        This is text-to-speech, not singing. Successful delivery finishes the turn.

        Args:
            text(string): Short text to speak, at most 300 characters. No SSML.
        """
        result = await self.voice.send(event, text)
        if spoken := event.get_extra("local_codex_voice_text"):
            self.group_context.observe_voice_reply(event, spoken)
            return None
        return result

    @filter.llm_tool(name="generate_image")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """Generate and deliver one new image requested by the current user.

        Describe visible reference details in the prompt when asked to redraw.
        This creates a new image from text; it does not edit pixels or read files.
        Delivery ends the turn.

        Args:
            prompt(string): Visual description of one new image, at most 1500 characters.
        """
        if is_proactive_group(event):
            return "请等待群成员明确要求生图，主动发言不自动生图。"
        if not requests_image_generation(event):
            return "本条消息未明确要求生图；如需新画，请说‘画一张…’。找现成图片可使用网络找图。"
        path = await self.image_generator.generate(event, prompt)
        delivered = await event.send(event.chain_result([Image.fromFileSystem(path)]))
        if delivered is False or getattr(event, "proactive_send_failed", False):
            raise RuntimeError("图片已生成，但未确认发送成功。")
        return None

    @filter.llm_tool(name="search_public_web")
    async def search_public_web(self, event: AstrMessageEvent, query: str) -> str:
        """Search public web information and return sources without sending a QQ message.

        Args:
            query(string): Public search topic, at most 300 characters; exclude private chat or member information.
        """
        if not uses_api(self.config) or not self.config.get("web_search", False):
            raise PermissionError("API web search is disabled.")
        if not is_allowed(self.config, event.get_platform_name(), event.get_sender_id(), event.get_group_id()):
            raise PermissionError("This sender cannot search through the bridge.")
        return await search_public_web(self.context, self.config, query)

    @filter.llm_tool(name="search_web_image")
    async def search_web_image(self, event: AstrMessageEvent, query: str):
        """Find and send one existing online image with its source to this chat.

        Default for find/search/show/send a picture. Never generates a picture.
        Successful delivery finishes the turn; do not send another confirmation.

        Args:
            query(string): Image subject to search, at most 120 characters; no private chat context.
        """
        async with self.image_search.prepare(event, query) as (path, source):
            delivered = await event.send(
                event.chain_result(
                    [
                        Image.fromFileSystem(path),
                        Plain(f"{source.title}\n来源：{source.page_url}"),
                    ]
                )
            )
            if delivered is False or getattr(event, "proactive_send_failed", False):
                raise RuntimeError("已找到图片，但未确认发送成功。")

    @filter.llm_tool(name="send_douyin_video")
    async def send_douyin_video(self, event: AstrMessageEvent, url: str):
        """Download and send one existing Douyin video to the current chat.

        For keywords, first use live web search to find a real douyin.com/video
        page. For an exact user share link, pass that original link unchanged.
        Never generates video. Successful delivery ends the turn.

        Args:
            url(string): One official Douyin video page or v.douyin.com share link; no search pages, local paths or invented IDs.
        """
        async with self.douyin_video.prepare(event, url) as (path, title, source):
            delivered = await event.send(
                event.chain_result(
                    [
                        Video.fromFileSystem(str(path)),
                        Plain(f"{title}\n来源：{source}"),
                    ]
                )
            )
            if delivered is False or getattr(event, "proactive_send_failed", False):
                raise RuntimeError("已下载抖音视频，但未确认发送成功。")
            self.group_context.observe_parsed_link(
                event,
                {
                    "resource_id": "douyin-"
                    + hashlib.sha256(source.encode()).hexdigest(),
                    "url": source,
                    "platform": "抖音",
                    "text": "已发送抖音视频，发布者标题："
                    + title
                    + "（仅标题，未分析视频内容。）",
                },
            )

    @filter.llm_tool(name="sing_cover")
    async def sing_cover(self, event: AstrMessageEvent, search_id: str, position: int):
        """Sing a requested song excerpt using the configured character voice.

        First call find_cover_music and select one of its current candidates. This is
        voice conversion, not ordinary playback or text-to-speech. Never use it
        during proactive messages. Successful delivery finishes the turn.

        Args:
            search_id(string): Exact current search_id returned by find_cover_music.
            position(int): Exact candidate position returned by find_cover_music.
        """
        result = await self.singing.send(event, search_id, position)
        if description := event.get_extra("local_codex_cover_text"):
            self.group_context.observe_voice_reply(event, description)
            return None
        return result

    @filter.llm_tool(name="find_cover_music")
    async def find_cover_music(self, event: AstrMessageEvent, title: str):
        """Find session-bound audio candidates for a requested character cover.

        Searches the original music provider, then an enabled alternate provider
        on failure. Does not play or send the original recording. An exact video
        reference cannot silently become a different song from another source.

        Args:
            title(string): Song name with optional artist, or the exact Bilibili link/ID.
        """
        return await self.cover_sources.find(event, title)

    async def terminate(self):
        """Stop the owned Codex process when this plugin is unloaded."""
        self.image_search.close()
        self.douyin_video.close()
        await self.media_cache.stop()
        await self.profile_learner.close()
        await self.image_generator.stop()
        await self.singing.stop()
        if self.runner is not None:
            await self.runner.stop()
        if not uses_api(self.config) and self.config.get("integrate_plugins", False):
            await self.context.provider_manager.terminate_provider(PROVIDER_ID)
