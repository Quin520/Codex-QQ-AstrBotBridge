"""Exercise source selection, ownership and media limits without sending QQ."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import make_dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from astrbot_plugin_local_codex.cover_sources import CoverSources
from astrbot_plugin_local_codex.music_source import MusicSourceError, PreparedSong
from astrbot_plugin_local_codex.video_context import VideoContextError


class CoverSourcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {
            "owner_qq": "123",
            "group_qq": "456",
            "singing_enabled": True,
            "singing_music_fallback": True,
        }
        old_path = "data.plugins.astrbot_plugin_listen_music.main"
        new_root = "data.plugins.astrbot_plugin_music"
        new_path = new_root + ".main"
        player_path = new_root + ".core.platform.ncm"
        model_path = new_root + ".core.model"
        old_type = type("ListenMusicPlugin", (), {"__module__": old_path})
        new_type = type("MusicPlugin", (), {"__module__": new_path})
        player_type = type("NetEaseMusic", (), {"__module__": player_path})
        song_type = make_dataclass(
            "Song",
            ["id", "name", "artists", "duration", "audio_url"],
            namespace={"__module__": model_path},
        )
        modules = {}
        for path, name, value in (
            (old_path, "ListenMusicPlugin", old_type),
            (new_path, "MusicPlugin", new_type),
            (player_path, "NetEaseMusic", player_type),
            (model_path, "Song", song_type),
        ):
            module = ModuleType(path)
            setattr(module, name, value)
            modules[path] = module
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.original = old_type()
        self.original._initialized = True
        self.original.find_music_for_llm = AsyncMock(
            return_value=json.dumps({"status": "error", "message": "Bilibili HTTP 412"})
        )
        self.song = song_type("fixture", "Fixture song", "Fixture artist", 120000, None)
        self.player = player_type()
        self.player.fetch_songs = AsyncMock(return_value=[self.song])

        async def extra(song):
            song.audio_url = "https://audio.example.com/song.mp3"
            return song

        self.player.fetch_extra = AsyncMock(side_effect=extra)
        self.plugin = new_type()
        self.plugin.get_player = Mock(return_value=self.player)
        self.old_star = SimpleNamespace(
            name="astrbot_plugin_listen_music",
            root_dir_name="astrbot_plugin_listen_music",
            module_path=old_path,
            activated=True,
            reserved=False,
            star_cls=self.original,
        )
        self.star = SimpleNamespace(
            name="astrbot_plugin_music",
            root_dir_name="astrbot_plugin_music",
            module_path=new_path,
            activated=True,
            reserved=False,
            star_cls=self.plugin,
        )
        self.context = SimpleNamespace(get_all_stars=lambda: [self.old_star, self.star])
        self.service = CoverSources(self.context, self.config, self.root)
        self.event = self.event_for()

        async def download(_url, path, _maximum):
            path.write_bytes(b"ID3 fixture audio")

        self.download = AsyncMock(side_effect=download)
        self.probe = AsyncMock(
            return_value=json.dumps(
                {"format": {"duration": "120"}, "streams": [{"codec_type": "audio"}]}
            ).encode()
        )
        for target, value in (
            ("download_video", self.download),
            ("_run_media", self.probe),
            ("shutil.which", Mock(return_value="ffprobe")),
        ):
            started = patch("astrbot_plugin_local_codex.cover_sources." + target, value)
            started.start()
            self.addCleanup(started.stop)

    @staticmethod
    def event_for(sender="999", group="456", platform="aiocqhttp"):
        return SimpleNamespace(
            unified_msg_origin="fixture:shared-group-session",
            get_sender_id=lambda: sender,
            get_group_id=lambda: group,
            get_self_id=lambda: "321",
            get_platform_name=lambda: platform,
            get_platform_id=lambda: "fixture",
        )

    async def find(self, event=None, title="Fixture song"):
        return json.loads(await self.service.find(event or self.event, title))

    async def test_bili_player_025_search_explicitly_uses_audio_and_preserves_lease(self):
        response = {"status": "candidates", "search_id": "new-lease", "candidates": [{"position": 1}]}
        self.original.find_in_bilibili_for_llm = AsyncMock(return_value=json.dumps(response))
        result = await self.find()
        self.assertEqual(result["search_id"], "new-lease")
        self.original.find_in_bilibili_for_llm.assert_awaited_once_with(self.event, "Fixture song", delivery="audio")
        self.original.find_music_for_llm.assert_not_awaited()
        self.player.fetch_songs.assert_not_awaited()

    async def test_music_33_reviewed_netease_module_can_supply_fallback(self):
        root = "data.plugins.astrbot_plugin_music.core.platform.netease"
        player_type = type("NetEaseMusic", (), {"__module__": root})
        module = ModuleType(root)
        module.NetEaseMusic = player_type
        player = player_type()
        player.fetch_songs = self.player.fetch_songs
        player.fetch_extra = self.player.fetch_extra
        self.plugin.get_player.return_value = player
        with patch.dict(sys.modules, {root: module}):
            result = await self.find()
        self.assertEqual(result["status"], "candidates")
        player.fetch_songs.assert_awaited_once()

    async def test_primary_candidates_are_preserved_without_contacting_fallback(self):
        response = {
            "status": "candidates",
            "search_id": "legacy-token",
            "candidates": [{"position": 1, "title": "Chosen original"}],
        }
        self.original.find_music_for_llm.return_value = json.dumps(response)
        result = await self.find()
        self.assertEqual(result, {**response, "source": "bilibili"})
        self.player.fetch_songs.assert_not_awaited()

    async def test_exact_video_reference_never_substitutes_a_different_source(self):
        for title in (
            "https://b23.tv/Example",
            "https://www.bilibili.com/video/BV1Q541167Qg",
            "BV1Q541167Qg",
            "av170001",
        ):
            with self.subTest(title=title):
                result = await self.find(title=title)
                self.assertEqual(result["status"], "error")
                self.assertIn("HTTP 412", result["message"])
                self.assertIn("请提供歌名", result["message"])
        self.player.fetch_songs.assert_not_awaited()

    async def test_fallback_is_opt_in_and_disabled_singing_denies_all_search(self):
        self.config.pop("singing_music_fallback")
        self.assertEqual((await self.find())["status"], "error")
        self.player.fetch_songs.assert_not_awaited()
        self.original.find_music_for_llm.reset_mock()
        self.config["singing_enabled"] = False
        self.assertEqual((await self.find())["status"], "error")
        self.original.find_music_for_llm.assert_not_awaited()

    async def test_untrusted_private_chat_other_group_and_proactive_cannot_search(self):
        for event in (
            self.event_for(group=""),
            self.event_for(group="other"),
            self.event_for(platform="webchat"),
        ):
            self.assertEqual((await self.find(event))["status"], "error")
        event_type = type(
            "ProactiveMessageEvent",
            (),
            {
                "__module__": "data.plugins.astrbot_plugin_proactive_chat.core.proactive_event"
            },
        )
        proactive = event_type()
        proactive.__dict__.update(vars(self.event))
        self.assertEqual((await self.find(proactive))["status"], "error")
        self.original.find_music_for_llm.assert_not_awaited()
        self.assertEqual(
            (await self.find(self.event_for(sender="123", group="")))["status"],
            "candidates",
        )

    async def test_fallback_candidates_hide_urls_and_paths(self):
        result = await self.find()
        self.assertEqual(result["source"], "netease")
        self.assertEqual(
            result["candidates"],
            [{"position": 1, "title": "Fixture song", "artist": "Fixture artist"}],
        )
        self.assertNotIn("http", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.download.assert_not_awaited()

    async def test_one_network_failure_keeps_other_verified_candidates(self):
        other = copy.copy(self.song)
        other.id = "other"
        self.player.fetch_songs.return_value = [self.song, other]

        async def extra(song):
            if song.id == "other":
                raise aiohttp.ClientConnectionError("fixture failure")
            song.audio_url = "https://audio.example.com/song.mp3"
            return song

        self.player.fetch_extra.side_effect = extra
        result = await self.find()
        self.assertEqual(result["status"], "candidates")
        self.assertEqual(len(result["candidates"]), 1)

    async def test_missing_sender_is_not_an_authorized_group_member(self):
        self.assertEqual(
            (await self.find(self.event_for(sender=None)))["status"], "error"
        )
        self.original.find_music_for_llm.assert_not_awaited()

    async def test_owned_source_is_bounded_verified_and_cleaned_after_lease(self):
        result = await self.find()
        async with self.service.prepared(self.event, result["search_id"], 1) as song:
            self.assertTrue(song.path.is_file())
            self.assertTrue(song.path.is_relative_to(self.service.data_dir))
            self.assertEqual(song.title, "Fixture song")
            source_path = song.path
        self.assertFalse(source_path.exists())
        self.assertEqual(self.download.call_args.args[2], 25 * 1024 * 1024)
        argv = self.probe.call_args.args[0]
        self.assertIn("file,pipe", argv)
        self.assertIn("mp3,flac,wav,ogg,mov,aac", argv)
        self.assertNotIn("http", " ".join(argv))

    async def test_same_group_session_different_member_cannot_consume(self):
        result = await self.find()
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(
                self.event_for(sender="888"), result["search_id"], 1
            ):
                self.fail("a different member consumed the source")
        self.download.assert_not_awaited()
        async with self.service.prepared(self.event, result["search_id"], 1):
            pass

    async def test_expiry_new_message_and_new_search_invalidate_old_candidates(self):
        result = await self.find()
        self.service._selections[result["search_id"]].expires = 0
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("expired source was accepted")
        result = await self.find()
        self.service.discard(self.event)
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("old message remained active")
        result = await self.find()
        await self.find()
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("new search did not replace old source")
        self.download.assert_not_awaited()

    async def test_late_search_result_cannot_repopulate_after_new_message(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def fetch(**_kwargs):
            entered.set()
            await finish.wait()
            return [self.song]

        self.player.fetch_songs.side_effect = fetch
        pending = asyncio.create_task(self.find())
        await entered.wait()
        self.service.discard(self.event)
        finish.set()
        self.assertEqual((await pending)["status"], "error")
        self.assertEqual(len(self.service._selections), 0)

    async def test_candidate_consumption_is_atomic_and_cannot_replay(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        result = await self.find()

        async def download(_url, path, _maximum):
            entered.set()
            await finish.wait()
            path.write_bytes(b"ID3 fixture")

        self.download.side_effect = download

        async def prepare():
            async with self.service.prepared(self.event, result["search_id"], 1):
                pass

        first = asyncio.create_task(prepare())
        await entered.wait()
        with self.assertRaises(MusicSourceError):
            await prepare()
        finish.set()
        await first
        self.download.assert_awaited_once()

    async def test_plugin_or_fallback_disabled_after_selection_blocks_download(self):
        result = await self.find()
        self.star.activated = False
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("disabled plugin was used")
        self.star.activated = True
        result = await self.find()
        self.config["singing_music_fallback"] = False
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("disabled fallback was used")
        self.download.assert_not_awaited()

    async def test_unregistered_or_spoofed_plugin_and_player_are_rejected(self):
        for field, replacement in (
            ("reserved", True),
            ("root_dir_name", "other"),
            ("module_path", "evil.main"),
            ("star_cls", SimpleNamespace(get_player=lambda **_kwargs: self.player)),
        ):
            previous = getattr(self.star, field)
            setattr(self.star, field, replacement)
            self.assertEqual((await self.find())["status"], "error")
            setattr(self.star, field, previous)
        self.plugin.get_player.return_value = SimpleNamespace(
            fetch_songs=self.player.fetch_songs, fetch_extra=self.player.fetch_extra
        )
        self.assertEqual((await self.find())["status"], "error")
        self.player.fetch_songs.assert_not_awaited()

    async def test_private_url_is_rejected_and_dns_guard_errors_are_mapped(self):
        self.song.audio_url = "http://127.0.0.1/private"
        self.assertEqual((await self.find())["status"], "error")
        self.download.assert_not_awaited()
        self.song.audio_url = None
        result = await self.find()
        self.download.side_effect = VideoContextError("non-public DNS address")
        with self.assertRaisesRegex(MusicSourceError, "公网音频"):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("unsafe source was yielded")
        self.assertEqual(list(self.service.data_dir.iterdir()), [])

    async def test_bad_audio_duration_and_non_audio_content_are_rejected(self):
        for duration, streams in (
            (901, [{"codec_type": "audio"}]),
            (float("nan"), [{"codec_type": "audio"}]),
            (120, [{"codec_type": "video"}]),
        ):
            result = await self.find()
            self.probe.return_value = json.dumps(
                {"format": {"duration": duration}, "streams": streams}
            ).encode()
            with self.assertRaises(MusicSourceError):
                async with self.service.prepared(self.event, result["search_id"], 1):
                    self.fail("invalid media was yielded")
            self.assertEqual(list(self.service.data_dir.iterdir()), [])

    async def test_large_local_file_and_download_failure_leave_no_temporary_media(self):
        async def too_large(_url, path, _maximum):
            with path.open("wb") as stream:
                stream.truncate(25 * 1024 * 1024 + 1)

        self.download.side_effect = too_large
        result = await self.find()
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("oversized media was yielded")
        self.assertEqual(list(self.service.data_dir.iterdir()), [])

    async def test_cancellation_during_download_cleans_partial_file(self):
        entered = asyncio.Event()

        async def download(_url, path, _maximum):
            path.write_bytes(b"partial")
            entered.set()
            await asyncio.Event().wait()

        self.download.side_effect = download
        result = await self.find()

        async def prepare():
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("cancelled source was yielded")

        task = asyncio.create_task(prepare())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(self.service.data_dir.iterdir()), [])

    async def test_new_message_during_download_prevents_yield(self):
        async def download(_url, path, _maximum):
            path.write_bytes(b"ID3 fixture")
            self.service.discard(self.event)

        self.download.side_effect = download
        result = await self.find()
        with self.assertRaises(MusicSourceError):
            async with self.service.prepared(self.event, result["search_id"], 1):
                self.fail("stale source was yielded")
        self.assertEqual(list(self.service.data_dir.iterdir()), [])

    async def test_legacy_handle_delegates_to_existing_scoped_source(self):
        @asynccontextmanager
        async def legacy(context, event, token, position):
            self.assertIs(context, self.context)
            self.assertIs(event, self.event)
            self.assertEqual((token, position), ("old-token", 1))
            yield PreparedSong(self.root / "existing.m4a", "Existing song")

        with patch(
            "astrbot_plugin_local_codex.cover_sources.prepared_song", side_effect=legacy
        ):
            async with self.service.prepared(self.event, "old-token", 1) as song:
                self.assertEqual(song.title, "Existing song")
        self.download.assert_not_awaited()

    async def test_candidate_storage_is_bounded(self):
        first = None
        for number in range(130):
            result = await self.find(self.event_for(sender=str(1000 + number)))
            first = first or result["search_id"]
        self.assertLessEqual(len(self.service._selections), 128)
        self.assertLessEqual(len(self.service._generations), 128)
        self.assertNotIn(first, self.service._selections)
