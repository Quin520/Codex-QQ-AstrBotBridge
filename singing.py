"""Render bounded song covers in a separate, administrator-configured runtime."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Record

from .access import is_allowed
from .group_context import is_proactive_group
from .music_source import prepared_song
from .voice import VoiceService


class SingingService:
    """Accept only a current music-search selection, never model-selected paths."""

    def __init__(self, context, config: dict, data_dir: Path):
        self.context = context
        self.config = config
        self.data_dir = data_dir / "singing"
        self._lock = asyncio.Lock()
        self._last_sent = {}
        self._process = None
        self._active_task = None
        self._closed = False

    def _settings_error(self) -> str:
        """Check operator-owned paths before downloading any song."""
        model = str(self.config.get("singing_model", "") or "").strip()
        if not model:
            return "翻唱入口已接入，但还没有配置角色音色模型；现在不能生成翻唱。请主人在插件中选择 RVC 音色模型。"
        for key, suffixes in (
            ("singing_python", {".exe", ""}),
            ("singing_model", {".pth"}),
            ("singing_index", {".index"}),
        ):
            value = str(self.config.get(key, "") or "").strip()
            if key == "singing_index" and not value:
                continue
            path = Path(value)
            if (
                not path.is_absolute()
                or not path.is_file()
                or path.suffix.lower() not in suffixes
            ):
                return (
                    "翻唱运行环境或音色模型尚未配置完整，请主人检查插件中的翻唱设置。"
                )
        root = Path(str(self.config.get("singing_rvc_root", "") or ""))
        if not root.is_absolute() or not (root / "infer/cli.py").is_file():
            return "翻唱需要已安装依赖的 RVC 运行目录，请主人完成翻唱环境配置。"
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            return "翻唱运行环境没有找到 FFmpeg/FFprobe，请主人检查安装。"
        return ""

    async def _kill_worker(self) -> None:
        """Stop the worker and its owned inference children on timeout/unload."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            import signal

            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def _render(self, source: Path, directory: Path) -> Path:
        """Run the bundled fixed pipeline with private configuration and logs."""
        output = directory / "cover.mp3"
        job = {
            "rvc_root": str(self.config["singing_rvc_root"]),
            "model": str(self.config["singing_model"]),
            "index": str(self.config.get("singing_index", "") or ""),
            "pitch": max(-12, min(12, int(self.config.get("singing_pitch", 0)))),
            "duration": max(
                30, min(120, int(self.config.get("singing_duration_seconds", 60)))
            ),
            "source": str(source),
            "output": str(output),
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "device": "cuda" if self.config.get("singing_device") == "cuda" else "cpu",
        }
        job_path = directory / "job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        with (self.data_dir / "last-worker.log").open("wb") as log:
            self._process = await asyncio.create_subprocess_exec(
                str(self.config["singing_python"]),
                str(Path(__file__).with_name("singing_worker.py")),
                "--job",
                str(job_path),
                cwd=str(self.config["singing_rvc_root"]),
                env=env,
                stdout=log,
                stderr=log,
                **options,
            )
            try:
                async with asyncio.timeout(180):
                    code = await self._process.wait()
                if code != 0:
                    raise RuntimeError(
                        "Cover worker failed; see the private worker log."
                    )
            finally:
                await self._kill_worker()
                self._process = None
                if (worker_log := directory / "worker.log").is_file():
                    log.write(worker_log.read_bytes()[-2 * 1024 * 1024 :])
        if (
            output.is_symlink()
            or output.resolve().parent != directory.resolve()
            or not output.is_file()
            or not 0 < output.stat().st_size <= 10 * 1024 * 1024
        ):
            raise RuntimeError("Cover worker did not produce a bounded local MP3.")
        return output

    async def send(self, event: AstrMessageEvent, search_id: str, position: int) -> str:
        """Convert one selected song and deliver it to the current trusted chat."""
        if is_proactive_group(event) or not is_allowed(
            self.config,
            event.get_platform_name(),
            event.get_sender_id(),
            event.get_group_id(),
        ):
            raise PermissionError("This QQ event is not allowed to request a cover.")
        if self._closed or not self.config.get("singing_enabled", False):
            return "翻唱功能未启用，请不要声称已经演唱或发送。"
        if message := self._settings_error():
            return message
        if event.get_extra("local_codex_cover_sent"):
            return "本次回复已经尝试发送翻唱，请不要重复发送。"
        if self._lock.locked():
            return "正在制作另一首翻唱，请稍后再试。"
        now = time.monotonic()
        cooldown = max(
            60, min(3600, int(self.config.get("singing_cooldown_seconds", 300)))
        )
        self._last_sent = {
            key: when for key, when in self._last_sent.items() if now - when < 3600
        }
        scope = VoiceService._scope(event)
        if now - self._last_sent.get(scope, float("-inf")) < cooldown:
            return "当前会话刚请求过翻唱，请稍后再试。"
        async with self._lock:
            self._active_task = asyncio.current_task()
            # Reserve GPU work, including failed attempts, against request floods.
            self._last_sent[scope] = time.monotonic()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(
                    prefix="cover-", dir=self.data_dir
                ) as temporary:
                    directory = Path(temporary)
                    async with asyncio.timeout(260):
                        async with asyncio.timeout(45):
                            async with prepared_song(
                                self.context, event, search_id, position
                            ) as (source, title):
                                copied = directory / "source.audio"
                                shutil.copyfile(source, copied)
                        output = await self._render(copied, directory)
                        description = f"角色音色翻唱片段：{str(title)[:120]}"
                        event.set_extra("local_codex_cover_sent", True)
                        async with asyncio.timeout(30):
                            sent = await event.send(
                                event.chain_result(
                                    [Record.fromFileSystem(output, text=description)]
                                )
                            )
                        if sent is False or getattr(
                            event, "proactive_send_failed", False
                        ):
                            raise RuntimeError("Cover delivery was not confirmed.")
                        event.set_extra("local_codex_cover_text", description)
                        return "角色音色翻唱片段已发送到当前会话。"
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Log details privately at the tool boundary.
                logger.exception("Local character cover generation or delivery failed")
                if event.get_extra("local_codex_cover_sent"):
                    return "翻唱已生成，但未确认发送成功；请不要声称已送达，也不要立即重发。"
                return "翻唱制作失败或超时，尚未发送；请主人检查音色及运行环境，详细错误保存在本机日志。"
            finally:
                self._active_task = None

    async def stop(self) -> None:
        """Cancel owned work when the plugin is disabled or unloaded."""
        self._closed = True
        if self._active_task and self._active_task is not asyncio.current_task():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        await self._kill_worker()
