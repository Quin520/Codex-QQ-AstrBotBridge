"""Expire disposable audio/video files without traversing persistent data."""

import asyncio
import json
import stat
import time
from pathlib import Path

from astrbot.api import logger

MEDIA_SUFFIXES = {
    ".aac",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".silk",
    ".wav",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}


def cleanup_expired_media(root: Path, retention_hours: int, *, now=None) -> dict:
    """Delete old regular media files immediately inside one trusted temp root.

    Args:
        root: AstrBot's dedicated temporary directory, never a chat argument.
        retention_hours: Minimum file age; clamped to at least one hour.
        now: Optional wall-clock value for deterministic validation.

    Returns:
        Counts of removed bytes/files and failures; no private file names.
    """
    result = {"removed_files": 0, "removed_bytes": 0, "failed_files": 0}
    if not root.exists():
        return result
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or getattr(root_info, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("The media cache root must be a real directory.")
    resolved = root.resolve(strict=True)
    cutoff = (time.time() if now is None else now) - max(1, retention_hours) * 3600
    for path in root.iterdir():
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_nlink != 1
                or info.st_mtime >= cutoff
                or path.resolve(strict=True).parent != resolved
            ):
                continue
            path.unlink()
            result["removed_files"] += 1
            result["removed_bytes"] += info.st_size
        except FileNotFoundError:
            continue  # Its owning sender may already have released it.
        except OSError:
            result["failed_files"] += 1  # Locked files remain for the next sweep.
    return result


class MediaCacheCleaner:
    """Own one cancellable periodic sweep of AstrBot's disposable media."""

    def __init__(self, config: dict, root: Path, status_file: Path):
        self.config = config
        self.root = root
        self.status_file = status_file
        self.task = None
        self.stopped = asyncio.Event()

    def start(self) -> None:
        """Start only after plugin initialization, never during construction."""
        if self.task is None:
            self.stopped.clear()
            self.task = asyncio.create_task(self._run(), name="codex-media-cache")

    async def stop(self) -> None:
        """Wait for any current sweep and stop without leaving a worker behind."""
        self.stopped.set()
        if self.task is not None:
            await self.task
            self.task = None

    async def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                hours = max(
                    1, min(720, int(self.config.get("media_cache_retention_hours", 24)))
                )
                minutes = max(
                    10, min(1440, int(self.config.get("media_cache_check_minutes", 60)))
                )
                if self.config.get("media_cache_cleanup_enabled", False):
                    result = await asyncio.to_thread(
                        cleanup_expired_media, self.root, hours
                    )
                    self.status_file.parent.mkdir(parents=True, exist_ok=True)
                    self.status_file.write_text(
                        json.dumps(
                            {
                                "checked_at": time.time(),
                                "retention_hours": hours,
                                "check_minutes": minutes,
                                **result,
                            }
                        ),
                        encoding="utf-8",
                    )
                    if result["removed_files"] or result["failed_files"]:
                        logger.info("Temporary media cleanup: %s", result)
            except (OSError, TypeError, ValueError):
                minutes = 60
                logger.exception(
                    "Temporary media cleanup failed; will retry next interval"
                )
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=minutes * 60)
            except TimeoutError:
                continue
