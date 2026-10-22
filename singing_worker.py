"""Run bounded cover or speech jobs with the pinned official RVC offline CLIs.

This worker has only standard-library dependencies. Run it with the isolated
RVC environment's Python, never AstrBot's Python. Job paths and model settings
are supplied by the administrator-facing bridge, not by model tool arguments.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import BinaryIO

RVC_REVISION = "81eed5e8f68b6bed1789f682fe78cdd324495afc"
SEPARATOR_MODEL = "model_bs_roformer_ep_368_sdr_12.9628.ckpt"
SEPARATOR_RELATIVE = Path("vocal/vocal_extraction") / SEPARATOR_MODEL
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _existing_file(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {name}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Missing or empty {name}: {path}")
    return path.resolve()


def _executable(value: object, fallback: str) -> str:
    configured = str(value or "").strip()
    resolved = configured or shutil.which(fallback)
    if not resolved:
        raise FileNotFoundError(f"{fallback} is not configured or on PATH")
    return str(_existing_file(resolved, fallback))


def _load_job(job_path: Path) -> dict:
    if job_path.stat().st_size > 16 * 1024:
        raise ValueError("Job configuration is too large")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise TypeError("Job configuration must be an object")
    job_dir = job_path.parent.resolve()
    source = _existing_file(job.get("source"), "source")
    if source.parent != job_dir:
        raise ValueError("Source must be copied directly into the job directory")
    output = Path(str(job.get("output", "")))
    if not output.is_absolute() or output != job_dir / "cover.mp3":
        raise ValueError("Output must be the job directory's cover.mp3")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists")
    root_value = job.get("rvc_root")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise ValueError("rvc_root must be an absolute path")
    root = Path(root_value).resolve()
    model = _existing_file(job.get("model"), "RVC voice model")
    if model.suffix.lower() != ".pth":
        raise ValueError("RVC voice model must be a .pth file")
    index = None
    if job.get("index"):
        index = _existing_file(job["index"], "RVC added index")
        if index.suffix.lower() != ".index" or "trained" in index.name:
            raise ValueError("Use an added .index file, not a trained index")
    device = job.get("device", "cpu")
    if device not in {"cpu", "cuda"}:
        raise ValueError("Device must be cpu or cuda")
    mode = job.get("mode", "cover")
    if not isinstance(mode, str) or mode not in {"cover", "speech"}:
        raise ValueError("Mode must be cover or speech")
    return {
        "root": root,
        "directory": job_dir,
        "source": source,
        "output": output,
        "model": model,
        "index": index,
        "pitch": _integer(job.get("pitch", 0), "pitch", -12, 12),
        "duration": _integer(job.get("duration", 60), "duration", 30, 120),
        "device": device,
        "mode": mode,
        "ffmpeg": _executable(job.get("ffmpeg"), "ffmpeg"),
        "ffprobe": _executable(job.get("ffprobe"), "ffprobe"),
    }


def _preflight(job: dict) -> None:
    root = job["root"]
    required = [
        root / "infer/cli.py",
        root / "assets/hubert_base/config.json",
        root / "assets/hubert_base/preprocessor_config.json",
        root / "assets/hubert_base/pytorch_model.bin",
        root / "assets/rmvpe/rmvpe.pt",
    ]
    if job["mode"] == "cover":
        required.extend(
            [
                root / "tools/pymss/cli.py",
                root / "assets/pymss_weights" / SEPARATOR_RELATIVE,
                root / "assets/pymss_weights" / SEPARATOR_RELATIVE.with_suffix(".yaml"),
            ]
        )
    for path in required:
        _existing_file(str(path), "RVC runtime asset")
    # Every artifact is new for this job; stale stems must never count as success.
    for name in ("input.wav", "stems", "converted.wav", "cover.pending.mp3"):
        path = job["directory"] / name
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Job artifact already exists: {name}")


def _environment(job: dict) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(job["root"]),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "CUDA_VISIBLE_DEVICES": "0" if job["device"] == "cuda" else "-1",
            "PYMSS_MODEL_DIR": str(job["root"] / "assets/pymss_weights"),
            "weight_root": str(job["model"].parent),
            "rmvpe_root": str(job["root"] / "assets/rmvpe"),
        }
    )
    executable_dirs = {str(Path(job[key]).parent) for key in ("ffmpeg", "ffprobe")}
    env["PATH"] = (
        os.pathsep.join(sorted(executable_dirs)) + os.pathsep + env.get("PATH", "")
    )
    return env


def _run(
    command: list[str], job: dict, log: BinaryIO, *, capture: bool = False
) -> bytes:
    log.write(("Running: " + repr(command) + "\n").encode("utf-8"))
    log.flush()
    # The parent enforces one deadline and terminates this process tree on expiry.
    # Keeping each heavy stage in its own process also releases its GPU allocation.
    result = subprocess.run(
        command,
        cwd=job["root"],
        env=_environment(job),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else log,
        stderr=log,
        check=True,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout if capture else b""


def _audio_duration(path: Path, job: dict, log: BinaryIO) -> float:
    _existing_file(str(path), "audio artifact")
    raw = _run(
        [
            job["ffprobe"],
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(path),
        ],
        job,
        log,
        capture=True,
    )
    if len(raw) > 16384:
        raise ValueError("Unexpected ffprobe response size")
    metadata = json.loads(raw)
    if not metadata.get("streams"):
        raise ValueError("Audio artifact contains no audio stream")
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio artifact has no finite positive duration")
    return duration


def _validate_length(path: Path, expected: float, job: dict, log: BinaryIO) -> None:
    actual = _audio_duration(path, job, log)
    tolerance = 0.25 if job["mode"] == "speech" else 1
    if abs(actual - expected) > tolerance:
        raise ValueError(f"Unexpected audio duration: {actual}, expected {expected}")


def run_job(job_path: Path, log: BinaryIO) -> Path:
    """Convert a fresh local job; only covers separate and mix accompaniment."""
    job = _load_job(job_path.resolve())
    _preflight(job)
    directory = job["directory"]
    clip = directory / "input.wav"
    stems = directory / "stems"
    vocals = stems / "input_Vocals.wav"
    instrumental = stems / "input_Instrumental.wav"
    converted = directory / "converted.wav"
    pending = directory / "cover.pending.mp3"
    source_duration = _audio_duration(job["source"], job, log)
    speech = job["mode"] == "speech"
    if speech and source_duration > job["duration"]:
        raise ValueError(
            "Speech source exceeds the duration limit; refusing to truncate"
        )
    duration = min(source_duration, job["duration"])
    _run(
        [
            job["ffmpeg"],
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(job["source"]),
            *([] if speech else ["-t", str(job["duration"])]),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(clip),
        ],
        job,
        log,
    )
    _validate_length(clip, duration, job, log)
    voice_input = clip
    if not speech:
        _run(
            [
                sys.executable,
                "-m",
                "tools.pymss.cli",
                "infer",
                SEPARATOR_MODEL,
                "--model-dir",
                str(job["root"] / "assets/pymss_weights"),
                "--input",
                str(clip),
                "--output",
                str(stems),
                "--device",
                job["device"],
                "--device-id",
                "0",
                "--format",
                "wav",
                "--param",
                "batch_size=1",
            ],
            job,
            log,
        )
        _validate_length(vocals, duration, job, log)
        _validate_length(instrumental, duration, job, log)
        voice_input = vocals
    command = [
        sys.executable,
        str(job["root"] / "infer/cli.py"),
        "--model",
        str(job["model"]),
        "--input",
        str(voice_input),
        "--output",
        str(converted),
        "--speaker-id",
        "0",
        "--pitch",
        str(job["pitch"]),
        "--f0-method",
        "rmvpe",
        "--index-rate",
        "0.75" if job["index"] else "0",
        "--rms-mix-rate",
        "1",
        "--protect",
        "0.33",
    ]
    if job["index"]:
        command.extend(["--index", str(job["index"])])
    _run(command, job, log)
    _validate_length(converted, duration, job, log)
    encode_input = ["-map", "0:a:0", "-af", "alimiter=limit=0.95"]
    if not speech:
        encode_input = [
            "-i",
            str(instrumental),
            "-filter_complex",
            (
                "[0:a][1:a]amix=inputs=2:duration=shortest:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95[a]"
            ),
            "-map",
            "[a]",
            "-t",
            str(job["duration"]),
        ]
    _run(
        [
            job["ffmpeg"],
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(converted),
            *encode_input,
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(pending),
        ],
        job,
        log,
    )
    _validate_length(pending, duration, job, log)
    if pending.stat().st_size > MAX_OUTPUT_BYTES:
        raise ValueError("Generated cover exceeds the output size limit")
    pending.rename(job["output"])
    return job["output"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    job_path = args.job.resolve()
    with (job_path.parent / "worker.log").open("ab") as log:
        try:
            run_job(job_path, log)
        except Exception:  # noqa: BLE001 - CLI boundary logs privately and exits nonzero.
            log.write(traceback.format_exc().encode("utf-8"))
            print(
                json.dumps(
                    {"ok": False, "error": "Cover job failed; see private worker.log"}
                )
            )
            return 1
    print(json.dumps({"ok": True, "output": "cover.mp3"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
