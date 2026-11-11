"""CPU-only integration baseline; never synthesize or evenly divide timestamps."""
import json
from pathlib import Path
import subprocess
from app.processes import ProcessCancelled, run_tool


class TranscriptionError(ValueError):
    pass


def validate_segments(segments, duration_ms):
    if type(duration_ms) is not int or duration_ms <= 0:
        raise TranscriptionError("Media duration must be a positive integer in milliseconds")
    if not isinstance(segments, list):
        raise TranscriptionError("Transcript segments must be an array")
    previous_start = -1
    for segment in segments:
        if not isinstance(segment, dict):
            raise TranscriptionError("Invalid transcript segment")
        start, end, text = (segment.get(key) for key in ("start_ms", "end_ms", "text"))
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= duration_ms or start < previous_start):
            raise TranscriptionError("Invalid or out-of-order timestamps; transcript not published")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("Transcript segment text must not be empty")
        previous_start = start


def parse_result(payload, duration_ms):
    if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
        raise TranscriptionError("Whisper JSON is missing its transcription array")
    segments = []
    for item in payload["transcription"]:
        if not isinstance(item, dict) or not isinstance(item.get("offsets"), dict):
            raise TranscriptionError("Whisper segment is missing timestamp offsets")
        segments.append({"start_ms": item["offsets"].get("from"),
                         "end_ms": item["offsets"].get("to"), "text": item.get("text")})
    validate_segments(segments, duration_ms)
    return segments


def transcribe(audio: Path, model: Path, executable: Path, output_dir: Path,
               duration_ms: int, *, timeout=4 * 60 * 60, cancel=None):
    """Run one real file, retaining raw evidence in a new private work directory.

    cancel accepts a threading.Event;
    cancellation, timeout and owner exit terminate the contained process tree.
    """
    validate_segments([], duration_ms)
    for path in (audio, model, executable):
        if not path.is_absolute() or not path.is_file():
            raise TranscriptionError("Audio, model and executable must be existing absolute files")
    output_dir.mkdir(parents=True, exist_ok=False)
    prefix = output_dir / "result"
    command = [str(executable), "-m", str(model), "-f", str(audio), "-l", "zh",
               "-t", "8", "-p", "1", "-ng", "-oj", "-of", str(prefix)]
    # No -d truncation: the input file is the exact processing boundary.
    try:
        with (output_dir / "stdout.log").open("xb") as stdout, (output_dir / "stderr.log").open("xb") as stderr:
            run_tool(command, stdout=stdout, stderr=stderr, timeout=timeout, cancel=cancel)
        payload = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError("Local recognition timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise TranscriptionError("Local recognition process failed; inspect private worker logs") from exc
    except ProcessCancelled:
        raise
    except (OSError, ValueError) as exc:
        raise TranscriptionError("Cannot read local recognition output") from exc
    return parse_result(payload, duration_ms)
