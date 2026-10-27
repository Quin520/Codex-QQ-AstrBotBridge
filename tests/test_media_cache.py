"""Verify expiry boundaries and protect non-cache files using real temp files."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_local_codex.media_cache import (
    MediaCacheCleaner,
    cleanup_expired_media,
)


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()

    def file(self, name, *, age_hours=25):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        age = self.now - age_hours * 3600
        os.utime(path, (age, age))
        return path

    def test_only_old_flat_audio_video_are_deleted(self):
        old = [self.file("old.mp4"), self.file("old.mp3")]
        keep = [
            self.file("recent.wav", age_hours=1),
            self.file("boundary.ogg", age_hours=24),
            self.file("model.pth"),
            self.file("config.json"),
            self.file("meme.gif"),
            self.file("recordings/meeting.wav"),
            self.file("image.png"),
        ]
        result = cleanup_expired_media(self.root, 24, now=self.now)
        self.assertEqual(
            result, {"removed_files": 2, "removed_bytes": 14, "failed_files": 0}
        )
        self.assertTrue(all(not path.exists() for path in old))
        self.assertTrue(all(path.exists() for path in keep))

    def test_hardlinks_are_never_removed(self):
        source = self.file("retained.wav")
        target = self.root / "linked.wav"
        os.link(source, target)
        self.assertEqual(
            cleanup_expired_media(self.root, 24, now=self.now)["removed_files"], 0
        )
        self.assertTrue(source.exists() and target.exists())

    def test_locked_file_is_retained_and_reported(self):
        source = self.file("locked.mp4")
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            report = cleanup_expired_media(self.root, 24, now=self.now)
        self.assertEqual(report["failed_files"], 1)
        self.assertTrue(source.exists())

    async def test_worker_sweeps_and_stops_cleanly(self):
        source = self.file("expired.mp3")
        status = self.root / "status.json"
        cleaner = MediaCacheCleaner(
            {"media_cache_cleanup_enabled": True}, self.root, status
        )
        cleaner.start()
        try:
            async with asyncio.timeout(3):
                while not status.exists():
                    await asyncio.sleep(0.01)
            report = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(report["removed_files"], 1)
            self.assertFalse(source.exists())
        finally:
            await cleaner.stop()
        self.assertIsNone(cleaner.task)

    async def test_disabled_worker_keeps_files(self):
        source = self.file("expired.mp3")
        status = self.root / "status.json"
        cleaner = MediaCacheCleaner(
            {"media_cache_cleanup_enabled": False}, self.root, status
        )
        cleaner.start()
        await asyncio.sleep(0)
        await cleaner.stop()
        self.assertTrue(source.exists())
        self.assertFalse(status.exists())
