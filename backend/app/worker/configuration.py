"""Explicit deployment configuration; no automatic model selection/fallback."""
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
from math import ceil
from pathlib import Path
import shutil
import stat
import sys
import subprocess
from tempfile import TemporaryDirectory
import time
import wave

from app.config import ROOT
from app.repositories.database import connect
from app.transcription import qwen3
from app.transcription.segments import UnsupportedModel
from app.transcription.whisper import probe_model
from app.worker.resources import (MEASURED_MODELS, MIB, PROBE_RESERVE_MIB, MemoryPressure, available_memory,
                                  probe_budget, resource_status)

WORKER_HEARTBEAT_TTL = 10


class ModelUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ProcessingConfig:
    executable: Path | None
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


def model_signature(path, runtime=None):
    if runtime is None:
        return file_signature(path)
    try:
        checkpoints = []
        for directory in (Path(path), Path(runtime["aligner"])):
            files = [[file.name, file_signature(file)] for file in qwen3.checkpoint_files(directory)]
            if not files or any(signature is None for _, signature in files):
                return None
            checkpoints.append(files)
        python = file_signature(runtime["python"])
        runner = file_signature(qwen3.RUNNER)
        if python is None or runner is None:
            return None
    except OSError:
        return None
    size = sum(signature[2] for files in checkpoints for _, signature in files)
    return [python, runner, size, checkpoints]


def entry_signature(entry):
    return model_signature(entry["path"], entry.get("qwen3"))


MODEL_MESSAGES = {
    "pending": "等待服务端验证",
    "unsupported": "模型与当前配置不兼容，请检查转写引擎和权重格式",
    "check_failed": "模型检查失败，请检查模型、运行环境和服务端日志",
    "check_timeout": "模型检查超时，请在服务端重新检查",
    "missing": "模型文件已移走或需要重新验证",
    "disabled": "由服务端停用",
    "offline": "转写进程未就绪",
    "memory_insufficient": "处理电脑内存暂不足",
    "below_recommended": "当前可用内存低于推荐余量",
    "resource_unknown": "资源需求尚无可靠估算",
    "available": "已通过转写检查",
}
SELECTABLE = {"available", "below_recommended", "resource_unknown"}


def model_catalog(settings):
    with connect(settings.database) as db:
        worker = db.execute("SELECT state, heartbeat, models_json FROM worker_status WHERE singleton=1").fetchone()
        controls = {row["name"]: dict(row) for row in db.execute("SELECT * FROM model_controls")}
    ready = (worker and worker["state"] == "processing"
             and time.time() - worker["heartbeat"] < WORKER_HEARTBEAT_TTL)
    entries = {entry["name"]: entry for entry in json.loads(worker["models_json"])} if worker else {}
    if not any(entry.get("is_default") for entry in entries.values()) and settings.processing_config:
        try:
            name = Path(json.loads(settings.processing_config.read_text(encoding="utf-8"))["model"]).name
            entries.setdefault(name, {"name": name, "status": "pending"})["is_default"] = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
    for name, control in controls.items():
        if control["path"] or control["disabled"]:
            entries.setdefault(name, {"name": name, "status": "pending"})
    available = available_memory()
    result = []
    for name, entry in sorted(entries.items()):
        control = controls.get(name, {})
        status = entry.get("status", "verified")
        if control.get("disabled"):
            status = "disabled"
        elif not ready:
            status = "offline"
        elif entry.get("revision", 0) != control.get("revision", 0):
            status = "pending"
        elif status == "verified":
            status = (resource_status(entry, available)
                      if entry_signature(entry) == entry["signature"] else "missing")
        required = entry.get("required_memory_mib")
        message = ("暂时无法读取处理电脑的可用内存"
                   if status == "resource_unknown" and available is None else MODEL_MESSAGES[status])
        result.append(dict(name=name, is_default=entry.get("is_default", False), status=status,
                           selectable=status in SELECTABLE, message=message,
                           required_memory_mib=required, available_memory_mib=available,
                           recommended_memory_mib=required + max(512, required // 4) if required else None,
                           memory_source=entry.get("memory_source")))
    return result


def configured_models(settings):
    return [item["name"] for item in model_catalog(settings) if item["selectable"]]


def probe_entry(config, work, audio, entry, stop):
    path = Path(entry["path"])
    runtime = entry.get("qwen3")
    signature = entry["signature"]
    if entry["engine"] == qwen3.ENGINE and runtime is None:
        return "unsupported"
    if entry["engine"] == "whisper.cpp" and config.executable is None:
        return "unsupported"
    if signature is None:
        return "missing"
    if entry.get("sha256"):
        digest = entry["sha256"]
    elif runtime:
        aligner = Path(runtime["aligner"])
        qwen3.check_checkpoint(path)
        qwen3.check_checkpoint(aligner, aligner=True)
        aligner_digest = qwen3.checkpoint_digest(aligner)
        if aligner_digest != runtime.get("aligner_sha256", aligner_digest):
            logging.warning("Qwen3 forced aligner SHA-256 mismatch: %s", aligner.name)
            return "check_failed"
        entry["qwen3"] = {**runtime, "aligner_sha256": aligner_digest}
        digest = qwen3.checkpoint_digest(path)
    else:
        with path.open("rb") as stream:
            header = stream.read(32)
            if len(header) < 32:
                return "check_failed"
            if header[:4] != b"lmgg":
                return "unsupported"
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    entry["sha256"] = digest
    if entry["is_default"] and digest != config.snapshot["model_sha256"]:
        logging.warning("Default model SHA-256 mismatch: %s", path.name)
        return "check_failed"

    reference = MEASURED_MODELS.get(digest)
    if reference:
        entry.update(required_memory_mib=reference, memory_source="reference")
    available = available_memory()
    budget = (ceil(2 * signature[2] / MIB) + PROBE_RESERVE_MIB if runtime else probe_budget(entry))
    if available is not None and available < max(budget, entry.get("probe_resume_mib", 0)):
        return "memory_insufficient"
    peak = 0

    def sample(_cpu, memory):
        nonlocal peak
        peak = max(peak, memory or 0)
        remaining = available_memory()
        if remaining is not None and remaining < 256:
            entry["probe_resume_mib"] = (available or 0) + 512
            raise MemoryPressure("Available memory fell below the probe reserve")

    if runtime:
        qwen3.probe_model(audio, path, runtime, work, stop, report=sample)
    else:
        probe_model(audio, path, config.executable, work, stop, report=sample)
    if entry_signature(entry) != signature:
        return "pending"
    if peak:
        entry["probe_peak_memory_mib"] = ceil(peak)
    if runtime and budget >= ceil(peak * 1.15):
        entry.update(required_memory_mib=budget, memory_source="reference")
    elif peak:
        estimate = ceil(peak * 1.15)
        if not reference or estimate > reference:
            entry.update(required_memory_mib=estimate,
                         memory_source="sampled_rss" if sys.platform == "darwin" else "peak_commit")
    return "verified"


def refresh_models(settings, config, previous, stop, publish):
    with connect(settings.database) as db:
        controls = {row["name"]: dict(row) for row in db.execute("SELECT * FROM model_controls")}
    default = config.model
    candidates = {name: Path(entry["path"]) for name, entry in previous.items()
                  if not entry_signature(entry)}
    registered = {name: Path(row["path"]) for name, row in controls.items() if row["path"]}
    explicit_paths = {default.resolve(), *(path.resolve() for path in registered.values())}
    model_dir = ROOT / "models"
    discovery_dirs = dict.fromkeys((default.parent, model_dir, model_dir / "qwen3"))
    discovered = {}
    for path in qwen3.discover_models(*discovery_dirs):
        resolved = path.resolve()
        if resolved not in explicit_paths:
            discovered.setdefault(path.name, path)
            explicit_paths.add(resolved)
    candidates.update(discovered)
    candidates.update(registered)
    if config.executable:
        candidates.update((path.name, path) for path in sorted(default.parent.glob("ggml-*.bin"))
                          if path.resolve().parent == default.resolve().parent)
    candidates[default.name] = default
    entries = {}
    for name, path in candidates.items():
        control = controls.get(name, {})
        engine = config.snapshot["engine"] if name == default.name else (
            qwen3.ENGINE if path.is_dir() else "whisper.cpp")
        runtime = None
        if engine == qwen3.ENGINE:
            runtime = config.snapshot.get("qwen3") or qwen3.local_runtime(path, ROOT)
        signature = model_signature(path, runtime)
        revision = control.get("revision", 0)
        old = previous.get(name, {})
        same = (old.get("path") == str(path) and old.get("signature") == signature
                and old.get("engine") == engine
                and old.get("revision") == revision)
        if same and old["status"] not in {"pending", "memory_insufficient"}:
            entries[name] = old
        else:
            entry = dict(name=name, path=str(path), signature=signature, revision=revision,
                         is_default=name == default.name, engine=engine, status="pending")
            if runtime:
                entry["qwen3"] = runtime
            if same and old["status"] == "memory_insufficient":
                entry = old
            entries[name] = entry
    publish(entries)
    pending = [entry for entry in entries.values() if entry["status"] in {"pending", "memory_insufficient"}
               and not controls.get(entry["name"], {}).get("disabled")
               and time.monotonic() >= entry.get("retry_after", 0)]
    if not pending:
        return entries

    with TemporaryDirectory(prefix="model-probe-", dir=settings.data_dir / "tmp") as temporary:
        work = Path(temporary)
        audio = work / "silence.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(32000))
        for entry in sorted(pending, key=lambda candidate: not candidate["is_default"]):
            if stop.is_set():
                break
            (work / "probe.log").unlink(missing_ok=True)
            try:
                entry["status"] = probe_entry(config, work, audio, entry, stop)
            except UnsupportedModel:
                entry["status"] = "unsupported"
            except MemoryPressure:
                entry["status"] = "memory_insufficient"
            except subprocess.TimeoutExpired:
                entry["status"] = "check_timeout"
            except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError):
                entry["status"] = "check_failed"
                detail = work / "probe.log"
                logging.exception("Cannot check model %s", entry["name"])
                if detail.is_file():
                    with detail.open("rb") as stream:
                        stream.seek(max(0, detail.stat().st_size - 2048))
                        logging.warning("%s", stream.read().decode(errors="replace"))
            if entry["status"] == "memory_insufficient":
                entry["retry_after"] = time.monotonic() + 10
            publish(entries)
    return entries


def model_wait_reasons(entries, available):
    blocked = {}
    for name, entry in entries.items():
        status = entry["status"]
        if status == "verified":
            status = resource_status(entry, available)
        if status not in SELECTABLE:
            blocked[name] = MODEL_MESSAGES[status]
    return blocked


def select_model(config, name):
    name = name or config.model.name
    entry = config.models.get(name)
    if entry is None or entry_signature(entry) != entry["signature"]:
        raise ModelUnavailable("Selected model is unavailable")
    if name == config.model.name:
        return config
    path = Path(entry["path"])
    snapshot = {**config.snapshot, "model": str(path), "model_sha256": entry["sha256"],
                "model_name": name, "model_revision": "unrecorded"}
    if entry["engine"] != config.snapshot["engine"]:
        snapshot.update(engine_version="unrecorded", engine_commit="unrecorded")
    if entry["engine"] == qwen3.ENGINE:
        snapshot["qwen3"] = entry["qwen3"]
    apply_engine(snapshot, entry["engine"])
    return replace(config, model=path, snapshot=snapshot)


def apply_engine(snapshot, engine):
    snapshot.update(engine=engine, language="zh", threads=8, processors=1, vad=False,
                    chunking=engine == qwen3.ENGINE)
    if engine == qwen3.ENGINE:
        snapshot["engine_version"] = qwen3.VERSION


def load_processing(settings, *, verify_default=True):
    if settings.processing_config is None:
        raise ValueError("Set RECUE_PROCESSING_CONFIG to a local processing JSON file")
    payload = json.loads(settings.processing_config.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int
            or payload.get("version") != 1 or payload.get("backend") != "cpu"):
        raise ValueError("Processing config requires version=1 and backend=cpu")
    for field in ("engine_version", "engine_commit", "model_revision", "model_name"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"Processing config requires {field}")
    engine = payload.get("engine", "whisper.cpp")
    if engine not in ("whisper.cpp", qwen3.ENGINE):
        raise ValueError("Processing config engine must be whisper.cpp or qwen3-asr")
    runtime = payload.get("qwen3")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise ValueError("Processing config qwen3 must be an object")
        for field in ("python", "aligner"):
            if not isinstance(runtime.get(field), str) or not Path(runtime[field]).is_absolute():
                raise ValueError(f"Processing config requires absolute qwen3.{field}")
        if not isinstance(runtime.get("aligner_sha256"), str):
            raise ValueError("Processing config requires qwen3.aligner_sha256")
    elif engine == qwen3.ENGINE:
        raise ValueError("The qwen3-asr default requires a qwen3 runtime and forced aligner")

    model = Path(payload["model"])
    if not model.is_absolute() or not isinstance(payload.get("model_sha256"), str):
        raise ValueError("Processing config requires absolute model and SHA-256")
    executable = None
    if "executable" in payload or engine == "whisper.cpp":
        executable = Path(payload["executable"])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("Processing config requires an existing absolute executable")
        with executable.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != payload.get("executable_sha256"):
                raise ValueError("Processing config executable SHA-256 mismatch")
    if verify_default:
        if engine == qwen3.ENGINE:
            qwen3.check_checkpoint(model)
            digest = qwen3.checkpoint_digest(model)
        else:
            with model.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != payload["model_sha256"]:
            raise ValueError("Processing config model SHA-256 mismatch")
    if not isinstance(payload.get("ffprobe", "ffprobe"), str):
        raise ValueError("ffprobe must be an executable path or name")
    tools = [shutil.which(settings.ffmpeg), shutil.which(payload.get("ffprobe", "ffprobe"))]
    if not all(tools):
        raise ValueError("FFmpeg and FFprobe are required before claiming work")
    snapshot = {**payload, "processing_version": "recue-pipeline-v1",
                "timeline_mapping": "source_pts_seconds - source_origin_seconds", "asr_offset_ms": 0,
                "ffmpeg": str(Path(tools[0]).resolve()), "ffprobe": str(Path(tools[1]).resolve())}
    apply_engine(snapshot, engine)
    return ProcessingConfig(executable, model, snapshot["ffmpeg"], snapshot["ffprobe"], snapshot)
