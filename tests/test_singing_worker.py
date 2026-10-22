"""Check bounded local singing jobs without models, network, or QQ messages."""

import importlib.util
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

WORKER_PATH = Path(__file__).resolve().parents[1] / "singing_worker.py"
SPEC = importlib.util.spec_from_file_location("isolated_singing_worker", WORKER_PATH)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class SingingWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "rvc"
        self.job_dir = self.directory / "job"
        self.root.mkdir()
        self.job_dir.mkdir()
        self.source = self.job_dir / "source.wav"
        self.source.write_bytes(b"fixture source")
        self.model = self.root / "voice.pth"
        self.model.write_bytes(b"fixture; never loaded by Torch")
        self.binary = self.root / "fake-ffmpeg.exe"
        self.binary.write_bytes(b"fixture executable")
        self.job_path = self.job_dir / "job.json"
        self.job = {
            "rvc_root": str(self.root),
            "model": str(self.model),
            "index": "",
            "pitch": 0,
            "duration": 60,
            "source": str(self.source),
            "output": str(self.job_dir / "cover.mp3"),
            "ffmpeg": str(self.binary),
            "ffprobe": str(self.binary),
            "device": "cpu",
        }
        for relative in (
            "infer/cli.py",
            "tools/pymss/cli.py",
            "assets/hubert_base/config.json",
            "assets/hubert_base/preprocessor_config.json",
            "assets/hubert_base/pytorch_model.bin",
            "assets/rmvpe/rmvpe.pt",
            "assets/pymss_weights/" + str(worker.SEPARATOR_RELATIVE),
            "assets/pymss_weights/"
            + str(worker.SEPARATOR_RELATIVE.with_suffix(".yaml")),
        ):
            asset = self.root / relative
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b"fixture asset")
        self.write_job()

    def write_job(self):
        self.job_path.write_text(json.dumps(self.job), encoding="utf-8")

    def test_job_defaults_and_bounds(self):
        self.job.pop("pitch")
        self.job.pop("duration")
        self.write_job()
        job = worker._load_job(self.job_path)
        self.assertEqual(job["pitch"], 0)
        self.assertEqual(job["duration"], 60)
        self.assertEqual(job["mode"], "cover")
        for key, value in (
            ("duration", 121),
            ("duration", 29),
            ("duration", True),
            ("pitch", 13),
            ("mode", "raw"),
            ("mode", None),
            ("mode", {"speech": True}),
        ):
            with self.subTest(key=key, value=value):
                changed = {**self.job, key: value}
                self.job_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    worker._load_job(self.job_path)

    def test_source_and_output_cannot_escape_job_directory(self):
        for mode in ("cover", "speech"):
            for key, value in (
                ("source", str(self.model)),
                ("output", str(self.root / "cover.mp3")),
            ):
                with self.subTest(mode=mode, key=key):
                    self.job_path.write_text(
                        json.dumps({**self.job, "mode": mode, key: value}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        worker._load_job(self.job_path)

    def _speech_mode(self, duration=120):
        self.job.update(mode="speech", duration=duration)
        self.write_job()
        for relative in (
            "tools/pymss/cli.py",
            "assets/pymss_weights/" + str(worker.SEPARATOR_RELATIVE),
            "assets/pymss_weights/"
            + str(worker.SEPARATOR_RELATIVE.with_suffix(".yaml")),
        ):
            (self.root / relative).unlink()

    def test_speech_preflight_needs_only_conversion_assets(self):
        self._speech_mode()
        job = worker._load_job(self.job_path)
        worker._preflight(job)
        (self.root / "assets/rmvpe/rmvpe.pt").unlink()
        with self.assertRaises(FileNotFoundError):
            worker._preflight(job)

    def test_missing_base_model_fails_before_any_process(self):
        (self.root / "assets/rmvpe/rmvpe.pt").unlink()
        with (
            patch.object(worker.subprocess, "run") as run,
            self.assertRaises(FileNotFoundError),
        ):
            worker.run_job(self.job_path, io.BytesIO())
        run.assert_not_called()
        self.assertFalse(Path(self.job["output"]).exists())

    def test_stale_artifact_cannot_count_as_success(self):
        (self.job_dir / "converted.wav").write_bytes(b"old output")
        with self.assertRaises(FileExistsError):
            worker._preflight(worker._load_job(self.job_path))

    def test_spawn_uses_fixed_argv_and_offline_environment(self):
        job = worker._load_job(self.job_path)
        with patch.object(worker.subprocess, "run") as run:
            worker._run([sys.executable, "--version"], job, io.BytesIO())
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertTrue(run.call_args.kwargs["check"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "-1")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["PYTHONPATH"], str(self.root))

    def _real_media_tools(self):
        ffmpeg = os.environ.get("LOCAL_CODEX_TEST_FFMPEG") or shutil.which("ffmpeg")
        ffprobe = os.environ.get("LOCAL_CODEX_TEST_FFPROBE") or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest(
                "FFmpeg and ffprobe are required for the real audio pipeline check"
            )
        self.job.update(ffmpeg=ffmpeg, ffprobe=ffprobe, duration=30, pitch=-2)
        self.write_job()
        pattern = b"".join(
            struct.pack(
                "<hh",
                int(5000 * math.sin(i * math.tau / 100)),
                int(5000 * math.sin(i * math.tau / 100)),
            )
            for i in range(100)
        )
        with wave.open(str(self.source), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            output.writeframes(pattern * (31 * 441))
        (self.root / "tools/pymss/cli.py").write_text(
            "import pathlib, shutil, sys\n"
            "args = sys.argv\n"
            "source = pathlib.Path(args[args.index('--input') + 1])\n"
            "dest = pathlib.Path(args[args.index('--output') + 1]); dest.mkdir()\n"
            "for stem in ('Vocals', 'Instrumental'):\n"
            "    shutil.copyfile(source, dest / ('input_' + stem + '.wav'))\n",
            encoding="utf-8",
        )
        (self.root / "infer/cli.py").write_text(
            "import shutil, sys\n"
            "args = sys.argv\n"
            "assert args[args.index('--speaker-id') + 1] == '0'\n"
            "assert args[args.index('--f0-method') + 1] == 'rmvpe'\n"
            "assert args[args.index('--pitch') + 1] == '-2'\n"
            "assert args[args.index('--index-rate') + 1] == '0'\n"
            "shutil.copyfile(args[args.index('--input') + 1], args[args.index('--output') + 1])\n",
            encoding="utf-8",
        )

    def test_real_ffmpeg_trims_and_mixes_fixed_backend_artifacts(self):
        self._real_media_tools()
        with (self.job_dir / "test.log").open("wb") as log:
            output = worker.run_job(self.job_path, log)
        # Re-probe independently because the job loader correctly refuses reused output.
        result = subprocess.run(
            [
                self.job["ffprobe"],
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(output),
            ],
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
        self.assertAlmostEqual(duration, 30, delta=0.1)
        self.assertGreater(output.stat().st_size, 1000)
        self.assertEqual(output.name, "cover.mp3")
        self.assertFalse((self.job_dir / "cover.pending.mp3").exists())

    def test_real_ffmpeg_speech_skips_separator_and_preserves_full_utterance(self):
        self._real_media_tools()
        self._speech_mode()
        index = self.root / "added_voice.index"
        index.write_bytes(b"fixture index")
        self.job["index"] = str(index)
        self.write_job()
        (self.root / "infer/cli.py").write_text(
            "import pathlib, shutil, sys\n"
            "args = sys.argv\n"
            "assert args[args.index('--speaker-id') + 1] == '0'\n"
            "assert args[args.index('--f0-method') + 1] == 'rmvpe'\n"
            "assert args[args.index('--pitch') + 1] == '-2'\n"
            "assert args[args.index('--index-rate') + 1] == '0.75'\n"
            "assert pathlib.Path(args[args.index('--index') + 1]).name == 'added_voice.index'\n"
            "assert pathlib.Path(args[args.index('--input') + 1]).name == 'input.wav'\n"
            "shutil.copyfile(args[args.index('--input') + 1], args[args.index('--output') + 1])\n",
            encoding="utf-8",
        )
        job = worker._load_job(self.job_path)
        with (self.job_dir / "test.log").open("wb") as log:
            output = worker.run_job(self.job_path, log)
            self.assertAlmostEqual(
                worker._audio_duration(output, job, log), 31, delta=0.1
            )
        self.assertEqual(output.name, "cover.mp3")
        self.assertGreater(output.stat().st_size, 1000)
        self.assertFalse((self.job_dir / "stems").exists())
        self.assertNotIn(
            "tools.pymss", (self.job_dir / "test.log").read_text(encoding="utf-8")
        )

    def test_speech_over_duration_limit_fails_instead_of_truncating(self):
        self._real_media_tools()
        self._speech_mode(duration=30)
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaisesRegex(ValueError, "refusing to truncate"),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse((self.job_dir / "input.wav").exists())
        self.assertFalse(Path(self.job["output"]).exists())

    def test_speech_conversion_failure_never_returns_tts_original(self):
        self._real_media_tools()
        self._speech_mode()
        (self.root / "infer/cli.py").write_text(
            "raise SystemExit(7)\n", encoding="utf-8"
        )
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())
        self.assertFalse((self.job_dir / "cover.pending.mp3").exists())

    def test_speech_success_without_converted_audio_is_failure(self):
        self._real_media_tools()
        self._speech_mode()
        (self.root / "infer/cli.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaises(FileNotFoundError),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())

    def test_speech_truncated_conversion_is_rejected(self):
        self._real_media_tools()
        self._speech_mode()
        (self.root / "infer/cli.py").write_text(
            "import sys, wave\n"
            "args = sys.argv\n"
            "with wave.open(args[args.index('--input') + 1], 'rb') as source:\n"
            "    with wave.open(args[args.index('--output') + 1], 'wb') as output:\n"
            "        output.setparams(source.getparams())\n"
            "        output.writeframes(source.readframes(source.getframerate()))\n",
            encoding="utf-8",
        )
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaisesRegex(ValueError, "Unexpected audio duration"),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())

    def test_speech_output_size_limit_prevents_publication(self):
        self._real_media_tools()
        self._speech_mode()
        with (
            patch.object(worker, "MAX_OUTPUT_BYTES", 100),
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaisesRegex(ValueError, "output size limit"),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())

    def test_separator_success_without_stems_is_failure(self):
        self._real_media_tools()
        (self.root / "tools/pymss/cli.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaises(FileNotFoundError),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())
        self.assertFalse((self.job_dir / "converted.wav").exists())

    def test_failed_conversion_never_returns_original_audio(self):
        self._real_media_tools()
        (self.root / "infer/cli.py").write_text(
            "raise SystemExit(7)\n", encoding="utf-8"
        )
        with (
            (self.job_dir / "test.log").open("wb") as log,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            worker.run_job(self.job_path, log)
        self.assertFalse(Path(self.job["output"]).exists())
        self.assertFalse((self.job_dir / "cover.pending.mp3").exists())

    def test_main_keeps_error_details_in_private_job_log(self):
        (self.root / "assets/rmvpe/rmvpe.pt").unlink()
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(worker.main(["--job", str(self.job_path)]), 1)
        self.assertNotIn(str(self.root), stdout.getvalue())
        self.assertIn(
            "rmvpe.pt", (self.job_dir / "worker.log").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
