"""Check scoped music borrowing without downloading audio or contacting QQ."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_local_codex.music_source import MusicSourceError, prepared_song

MODULE_NAME = "data.plugins.astrbot_plugin_listen_music.main"


class MusicSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / "recording.m4a"
        self.audio.write_bytes(b"isolated fake audio")
        self.event = SimpleNamespace(unified_msg_origin="fixture:Group:456:999")
        self.candidate = SimpleNamespace(display_title="Fixture Song")
        self.snapshot = SimpleNamespace(candidate_at=Mock(return_value=self.candidate))
        self.search = SimpleNamespace(snapshot=Mock(return_value=self.snapshot))
        self.media = SimpleNamespace(path=self.audio)
        self.delivery = SimpleNamespace(
            deliver=AsyncMock(return_value=SimpleNamespace(media=self.media))
        )
        self.store = SimpleNamespace(_media_dir=self.root, release=AsyncMock())
        self.lease = object()
        self.active = True
        plugin_type = type("ListenMusicPlugin", (), {"__module__": MODULE_NAME})
        self.plugin = plugin_type()
        self.plugin._initialized = True
        self.plugin._active_llm_search = Mock(side_effect=self.active_lease)
        self.plugin._consume_llm_search = Mock(side_effect=self.consume)
        self.plugin._require_search = lambda: self.search
        self.plugin._require_delivery = lambda: self.delivery
        self.plugin._require_media = lambda: self.store
        self.module = ModuleType(MODULE_NAME)
        self.module.ListenMusicPlugin = plugin_type
        self.module.VOICE_MEDIA_LIMITS = object()
        self.module_patch = patch.dict(sys.modules, {MODULE_NAME: self.module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.star = SimpleNamespace(
            name="astrbot_plugin_listen_music",
            root_dir_name="astrbot_plugin_listen_music",
            module_path=MODULE_NAME,
            activated=True,
            reserved=False,
            star_cls=self.plugin,
        )
        self.context = SimpleNamespace(get_all_stars=lambda: [self.star])

    def active_lease(self, session_id, search_id):
        if (
            self.active
            and session_id == "fixture:Group:456:999"
            and search_id == "search-fixture"
        ):
            return self.lease
        return None

    def consume(self, session_id, lease):
        if self.active and lease is self.lease:
            self.active = False
            return True
        return False

    def prepare(self, search_id="search-fixture", position=1, event=None):
        return prepared_song(self.context, event or self.event, search_id, position)

    async def test_valid_candidate_is_borrowed_until_context_exits(self):
        async with self.prepare() as song:
            self.assertEqual(song, (self.audio.resolve(), "Fixture Song"))
            self.store.release.assert_not_awaited()
        self.store.release.assert_awaited_once_with(self.media)
        self.search.snapshot.assert_called_once_with(
            search_id="search-fixture", session_id=self.event.unified_msg_origin
        )
        self.delivery.deliver.assert_awaited_once_with(
            self.candidate, limits=self.module.VOICE_MEDIA_LIMITS
        )

    async def test_consumer_failure_still_releases_original_media(self):
        with self.assertRaisesRegex(RuntimeError, "conversion failed"):
            async with self.prepare():
                raise RuntimeError("conversion failed")
        self.store.release.assert_awaited_once_with(self.media)

    async def test_cancelled_consumer_releases_original_media(self):
        entered = asyncio.Event()

        async def use_song():
            async with self.prepare():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(use_song())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.store.release.assert_awaited_once_with(self.media)

    async def test_other_session_and_unknown_search_cannot_download(self):
        for event, search_id in (
            (
                SimpleNamespace(unified_msg_origin="fixture:Group:456:888"),
                "search-fixture",
            ),
            (self.event, "old-search"),
        ):
            with self.assertRaisesRegex(MusicSourceError, "当前会话"):
                async with self.prepare(search_id, event=event):
                    self.fail("Unowned selection was yielded")
        self.delivery.deliver.assert_not_awaited()
        self.plugin._consume_llm_search.assert_not_called()

    async def test_no_snapshot_or_missing_position_cannot_consume(self):
        self.search.snapshot.return_value = None
        with self.assertRaises(MusicSourceError):
            async with self.prepare():
                self.fail("Missing snapshot was accepted")
        self.search.snapshot.return_value = self.snapshot
        self.snapshot.candidate_at.return_value = None
        with self.assertRaises(MusicSourceError):
            async with self.prepare():
                self.fail("Missing candidate was accepted")
        self.plugin._consume_llm_search.assert_not_called()
        self.delivery.deliver.assert_not_awaited()

    async def test_selection_is_consumed_before_download_and_cannot_replay(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await finish.wait()
            return SimpleNamespace(media=self.media)

        async def use_song():
            async with self.prepare():
                pass

        self.delivery.deliver.side_effect = download
        first = asyncio.create_task(use_song())
        await entered.wait()
        with self.assertRaises(MusicSourceError):
            async with self.prepare():
                self.fail("Selection was replayed")
        finish.set()
        await first
        self.delivery.deliver.assert_awaited_once()

    async def test_failed_consume_prevents_download(self):
        self.plugin._consume_llm_search.side_effect = None
        self.plugin._consume_llm_search.return_value = False
        with self.assertRaisesRegex(MusicSourceError, "已被使用"):
            async with self.prepare():
                self.fail("Failed consumption was ignored")
        self.delivery.deliver.assert_not_awaited()

    async def test_disabled_reserved_or_wrong_module_plugin_is_rejected(self):
        for field, value in (
            ("activated", False),
            ("reserved", True),
            ("module_path", "data.plugins.unrelated.main"),
            ("root_dir_name", "unrelated"),
            ("name", "unrelated"),
        ):
            with (
                self.subTest(field=field),
                patch.object(self.star, field, value),
                self.assertRaisesRegex(MusicSourceError, "启用"),
            ):
                async with self.prepare():
                    self.fail("Wrong plugin was accepted")
        self.delivery.deliver.assert_not_awaited()

    async def test_uninitialized_or_impostor_instance_is_rejected(self):
        for target, field, value in (
            (self.plugin, "_initialized", False),
            (self.star, "star_cls", SimpleNamespace()),
            (self.plugin, "_consume_llm_search", None),
        ):
            with (
                self.subTest(field=field),
                patch.object(target, field, value),
                self.assertRaisesRegex(MusicSourceError, "未就绪|不兼容"),
            ):
                async with self.prepare():
                    self.fail("Incompatible plugin was accepted")
        self.delivery.deliver.assert_not_awaited()

    async def test_user_url_path_and_invalid_position_are_not_media_inputs(self):
        for search_id, position in (
            ("", 1),
            (None, 1),
            ("a" * 129, 1),
            ("https://example.com/song.wav", 1),
            (str(self.audio), 1),
            ("search-fixture", True),
            ("search-fixture", "1"),
            ("search-fixture", 0),
            ("search-fixture", 101),
        ):
            with self.subTest(position=position), self.assertRaises(MusicSourceError):
                async with self.prepare(search_id, position):
                    self.fail("Invalid input was accepted")
        self.delivery.deliver.assert_not_awaited()

    async def test_download_errors_do_not_expose_internal_details(self):
        for error in (TimeoutError(), RuntimeError("private credential detail")):
            self.active = True
            self.delivery.deliver.side_effect = error
            with self.assertRaises(MusicSourceError) as caught:
                async with self.prepare():
                    self.fail("Failed download yielded media")
            self.assertNotIn("private credential", str(caught.exception))
        self.store.release.assert_not_awaited()

    async def test_disabling_plugin_during_download_releases_media(self):
        async def download(*args, **kwargs):
            self.star.activated = False
            return SimpleNamespace(media=self.media)

        self.delivery.deliver.side_effect = download
        with self.assertRaisesRegex(MusicSourceError, "已停用"):
            async with self.prepare():
                self.fail("Disabled plugin yielded media")
        self.store.release.assert_awaited_once_with(self.media)

    async def test_non_audio_missing_relative_and_outside_paths_release(self):
        forbidden = self.root / "credentials.txt"
        forbidden.write_text("isolated fixture", encoding="utf-8")
        for path in (forbidden, self.root / "missing.wav", Path("relative.wav")):
            self.active = True
            self.media.path = path
            with self.subTest(path=path), self.assertRaises(MusicSourceError):
                async with self.prepare():
                    self.fail("Invalid local media was accepted")
        self.assertEqual(self.store.release.await_count, 3)
        self.active = True
        self.store._media_dir = self.root / "owned"
        self.store._media_dir.mkdir()
        self.media.path = self.audio
        with self.assertRaises(MusicSourceError):
            async with self.prepare():
                self.fail("Outside file was accepted")
        self.assertEqual(self.store.release.await_count, 4)
