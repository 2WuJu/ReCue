"""Run local Qwen3-ASR and translate forced alignment into ReCue segments."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unicodedata

from app.processes import run_tool
from app.transcription.segments import TranscriptionError, UnsupportedModel, validate_segments

ENGINE = "qwen3-asr"
ALIGNER_NAME = "Qwen3-ForcedAligner-0.6B"
RUNTIME_PYTHON = Path(".local/qwen3-runtime") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
VERSION = "0.0.6"
RUNNER = Path(__file__).with_name("_qwen3_runner.py").resolve()
CHECKPOINT_SUFFIXES = {".json", ".txt", ".safetensors", ".jinja", ".model"}


def checkpoint_files(directory):
    return sorted(path for path in directory.iterdir()
                  if path.suffix in CHECKPOINT_SUFFIXES and path.is_file())


def checkpoint_digest(directory):
    digest = hashlib.sha256()
    for path in checkpoint_files(directory):
        digest.update(path.name.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def check_checkpoint(path, *, aligner=False):
    config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    if (not isinstance(config, dict) or config.get("model_type") != "qwen3_asr"
            or ("timestamp_token_id" in config) != aligner):
        expected = "Qwen3-ForcedAligner" if aligner else "Qwen3-ASR"
        raise UnsupportedModel(f"Expected a {expected} checkpoint")
    if not any(path.glob("*.safetensors")):
        raise TranscriptionError("Qwen3 checkpoint is missing safetensors weights")


def discover_models(*directories):
    for directory in directories:
        for config in sorted(directory.glob("*/config.json")):
            try:
                check_checkpoint(config.parent)
            except (OSError, ValueError):
                continue
            yield config.parent


def local_runtime(model, root):
    python = root / RUNTIME_PYTHON
    aligner = model.parent / ALIGNER_NAME
    if python.is_file() and aligner.is_dir():
        return dict(python=str(python), aligner=str(aligner))
    return None


def alignment_char(character):
    return character == "'" or unicodedata.category(character)[0] in "LN"


def parse_result(payload, duration_ms):
    if not isinstance(payload, dict):
        raise TranscriptionError("Invalid Qwen3-ASR output")
    text, words = payload.get("text"), payload.get("words")
    if not isinstance(text, str) or not isinstance(words, list):
        raise TranscriptionError("Qwen3-ASR output is missing transcript or alignment")
    validate_segments([], duration_ms)
    if not words and not text.strip():
        return []

    # The aligner strips punctuation. Match its tokens back to the original
    # transcript so punctuation, spaces and technical terms survive grouping.
    positions = [index for index, character in enumerate(text) if alignment_char(character)]
    aligned_text = "".join(text[index] for index in positions)
    segments = []
    offset = begin = matched = last_begin = 0
    start_ms = end_ms = previous_start = -1
    for word in words:
        if not isinstance(word, dict):
            raise TranscriptionError("Invalid Qwen3 alignment token")
        token, start, end = (word.get(key) for key in ("text", "start_time", "end_time"))
        if not isinstance(token, str) or not token or not aligned_text.startswith(token, matched):
            raise TranscriptionError("Qwen3 alignment text does not match the transcript")
        if any(type(value) not in (float, int) for value in (start, end)):
            raise TranscriptionError("Invalid Qwen3 alignment timestamp")
        if not 0 <= start <= end <= (duration_ms + 0.5) / 1000:
            raise TranscriptionError("Invalid Qwen3 alignment timestamp range")
        start, end = round(start * 1000), round(end * 1000)
        if not previous_start <= start <= end <= duration_ms:
            raise TranscriptionError("Qwen3 alignment timestamps are out of order or outside the media")
        previous_start = start

        # Close a segment only where there is a real boundary with positive duration.
        if end_ms > start_ms >= 0 and (
            text[positions[matched - 1] + 1:offset].rstrip().endswith(("。", "！", "？", "!", "?"))
            or start - end_ms >= 800 or end_ms - start_ms >= 12_000
        ):
            segments.append(dict(start_ms=start_ms, end_ms=end_ms, text=text[begin:offset].strip()))
            last_begin, begin = begin, offset
            start_ms = -1
        if start_ms < 0:
            start_ms, end_ms = start, end
        else:
            end_ms = max(end_ms, end)
        matched += len(token)
        offset = positions[matched] if matched < len(positions) else len(text)

    if matched != len(positions) or start_ms < 0:
        raise TranscriptionError("Qwen3 alignment does not cover the transcript")
    if end_ms == start_ms and segments:
        segments[-1]["text"] = text[last_begin:].strip()
        segments[-1]["end_ms"] = max(segments[-1]["end_ms"], end_ms)
    else:
        segments.append(dict(start_ms=start_ms, end_ms=end_ms, text=text[begin:].strip()))
    validate_segments(segments, duration_ms)
    return segments


def run_recognition(audio, model, runtime, work, *, timeout, cancel, report, probe=False):
    output = work / ("probe.json" if probe else "result.json")
    command = [runtime["python"], "-I", str(RUNNER), str(model), runtime["aligner"],
               str(audio), str(output)]
    if probe:
        log = work / "probe.log"
        output.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        with log.open("wb") as stream:
            run_tool(command + ["--probe"], stdout=stream, stderr=stream, timeout=timeout,
                     cancel=cancel, report=report, measure_memory=True)
    else:
        work.mkdir(parents=True, exist_ok=False)
        with (work / "stdout.log").open("wb") as stdout, (work / "stderr.log").open("wb") as stderr:
            run_tool(command, stdout=stdout, stderr=stderr, timeout=timeout, cancel=cancel, report=report)
    return json.loads(output.read_text(encoding="utf-8"))


def probe_model(audio, model, runtime, work, cancel, report):
    output = run_recognition(audio, model, runtime, work, timeout=180, cancel=cancel, report=report, probe=True)
    if output != {"ready": True}:
        raise TranscriptionError("Qwen3-ASR could not complete its model probe")


def transcribe(audio, model, runtime, work, duration_ms, *, cancel=None, report=None):
    try:
        output = run_recognition(audio, model, runtime, work, timeout=4 * 60 * 60, cancel=cancel, report=report)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TranscriptionError("Qwen3-ASR recognition failed; inspect private worker logs") from exc
    return parse_result(output, duration_ms)
