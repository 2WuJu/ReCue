"""Explicit deployment configuration; no automatic model selection/fallback."""
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
import time
import wave

from app.repositories.database import connect
from app.transcription.whisper import probe_model

WORKER_HEARTBEAT_TTL = 10


class ModelUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ProcessingConfig:
    executable: Path
    model: Path
    ffmpeg: str
    ffprobe: str
    snapshot: dict
    models: dict = field(default_factory=dict)


def file_signature(path):
    try:
        info = Path(path).stat()
    except OSError:
        return None
    if stat.S_ISREG(info.st_mode):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def configured_models(settings):
    with connect(settings.database) as db:
        worker = db.execute("SELECT state, heartbeat, models_json FROM worker_status WHERE singleton=1").fetchone()
    if not worker or worker["state"] != "processing" or time.time() - worker["heartbeat"] >= WORKER_HEARTBEAT_TTL:
        return []
    return sorted(entry["name"] for entry in json.loads(worker["models_json"])
                  if file_signature(entry["path"]) == entry["signature"])


def verify_models(settings, config, stop):
    default = config.model
    directory = default.resolve().parent
    candidates = [default, *(path for path in sorted(default.parent.glob("ggml-*.bin"))
                             if path != default and path.resolve().parent == directory)]
    models = {}
    with TemporaryDirectory(prefix="whisper-probe-", dir=settings.data_dir / "tmp") as work:
        work = Path(work)
        audio = work / "silence.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(32000))
        for path in candidates:
            try:
                signature = file_signature(path)
                if signature is None:
                    raise ValueError("Model file is missing")
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if path == default and digest != config.snapshot["model_sha256"]:
                    raise ValueError("Default model SHA-256 mismatch")
                probe_model(audio, path, config.executable, work, stop)
                if file_signature(path) != signature:
                    raise ValueError("File changed during model verification")
            except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                log = work / "probe.log"
                detail = log.read_bytes()[-2048:].decode(errors="replace") if log.is_file() else ""
                logging.warning("Cannot use Whisper model %s: %s\n%s", path.name, exc, detail)
                if path == default:
                    raise ValueError(f"Default model failed the Whisper check: {path.name}") from exc
            else:
                models[path.name] = dict(name=path.name, path=str(path), signature=signature, sha256=digest)
    return replace(config, models=models)


def select_model(config, name):
    name = name or config.model.name
    entry = config.models.get(name)
    if entry is None or file_signature(entry["path"]) != entry["signature"]:
        raise ModelUnavailable("Selected model is unavailable")
    if name == config.model.name:
        return config
    path = Path(entry["path"])
    snapshot = {**config.snapshot, "model": str(path), "model_sha256": entry["sha256"],
                "model_name": name, "model_revision": "unrecorded"}
    return replace(config, model=path, snapshot=snapshot)


def load_processing(settings):
    if settings.processing_config is None:
        raise ValueError("Set RECUE_PROCESSING_CONFIG to a local processing JSON file")
    payload = json.loads(settings.processing_config.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int
            or payload.get("version") != 1 or payload.get("backend") != "cpu"):
        raise ValueError("Processing config requires version=1 and backend=cpu")
    for field in ("engine_version", "engine_commit", "model_revision", "model_name"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"Processing config requires {field}")
    paths = []
    for field in ("executable", "model"):
        path = Path(payload[field])
        digest = payload.get(field + "_sha256")
        if not path.is_absolute() or not path.is_file() or not isinstance(digest, str):
            raise ValueError(f"Processing config requires absolute {field} and SHA-256")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Processing config {field} SHA-256 mismatch")
        paths.append(path)
    if not isinstance(payload.get("ffprobe", "ffprobe"), str):
        raise ValueError("ffprobe must be an executable path or name")
    tools = [shutil.which(settings.ffmpeg), shutil.which(payload.get("ffprobe", "ffprobe"))]
    if not all(tools):
        raise ValueError("FFmpeg and FFprobe are required before claiming work")
    snapshot = {**payload, "engine": "whisper.cpp", "language": "zh", "threads": 8,
                "processors": 1, "vad": False, "chunking": False,
                "processing_version": "recue-pipeline-v1",
                "timeline_mapping": "source_pts_seconds - source_origin_seconds", "asr_offset_ms": 0,
                "ffmpeg": str(Path(tools[0]).resolve()), "ffprobe": str(Path(tools[1]).resolve())}
    return ProcessingConfig(*paths, snapshot["ffmpeg"], snapshot["ffprobe"], snapshot)
