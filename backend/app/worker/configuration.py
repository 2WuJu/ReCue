"""Explicit deployment configuration; no automatic model selection/fallback."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil


@dataclass(frozen=True)
class ProcessingConfig:
    executable: Path
    model: Path
    ffmpeg: str
    ffprobe: str
    snapshot: dict


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
