"""Decode local media into a common zero-based playback/recognition timeline."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from pathlib import Path
import shutil
import subprocess
import wave

from app.processes import ProcessCancelled, run_tool
from app.storage.uploads import MAX_UPLOAD_BYTES

MAX_DURATION_MS = 120 * 60 * 1000
# Do not allow uploaded playlists to make FFmpeg read arbitrary local references.
FORMATS = "mov,matroska,webm,mp3,wav,ogg,flac,aac,avi"


class PreparationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedMedia:
    audio: Path
    playback: Path
    duration_ms: int
    media_type: str
    source_origin_seconds: str


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise PreparationError("Media timestamp is missing or invalid") from None
    if not result.is_finite():
        raise PreparationError("Media timestamp must be finite")
    return result


def prepare(source: Path, output_dir: Path, *, ffmpeg="ffmpeg", ffprobe="ffprobe",
            timeout=4 * 60 * 60, cancel=None):
    """Return only validated artifacts; retain private partial files on failure.

    Both streams subtract the SAME source origin. Audio resampling pads initial
    and internal timestamp gaps rather than shifting speech to time zero. No VAD,
    segmentation, duration truncation or per-stream STARTPTS reset is used.
    A manifest is written last; callers must never publish unreturned artifacts.
    """
    if not source.is_absolute() or not source.is_file():
        raise PreparationError("Source must be an existing absolute file")
    if not output_dir.is_absolute():
        raise PreparationError("Work directory must be absolute")
    size = source.stat().st_size
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise PreparationError("Media is empty or exceeds 4 GiB")
    executables = [shutil.which(name) for name in (ffmpeg, ffprobe)]
    if not all(executables):
        raise PreparationError("FFmpeg and FFprobe are required")
    ffmpeg_path, ffprobe_path = [str(Path(item).resolve()) for item in executables]
    output_dir.mkdir(parents=True, exist_ok=False)

    def execute(label, command):
        try:
            with (output_dir / f"{label}.stdout").open("xb") as stdout, \
                    (output_dir / f"{label}.stderr").open("xb") as stderr:
                run_tool(command, stdout=stdout, stderr=stderr, timeout=timeout, cancel=cancel)
        except subprocess.TimeoutExpired as exc:
            raise PreparationError(f"Media {label} timed out") from exc
        except subprocess.CalledProcessError as exc:
            raise PreparationError(f"Media {label} failed; inspect private processing logs") from exc

    def probe(label, path):
        execute(label, [ffprobe_path, "-v", "error", "-protocol_whitelist", "file",
                        "-format_whitelist", FORMATS, "-show_entries",
                        "format=start_time,duration,format_name:"
                        "stream=index,codec_type,codec_name,start_time,duration:"
                        "stream_disposition=attached_pic", "-of", "json", "-i", str(path)])
        try:
            result = json.loads((output_dir / f"{label}.stdout").read_text(encoding="utf-8"))
            if not isinstance(result.get("streams"), list) or not isinstance(result.get("format"), dict):
                raise ValueError()
            return result
        except (ValueError, AttributeError) as exc:
            raise PreparationError("Invalid media probe output") from exc

    metadata = probe("source-probe", source)
    streams = metadata["streams"]
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    video = next((item for item in streams if item.get("codec_type") == "video"
                  and not item.get("disposition", {}).get("attached_pic")), None)
    if audio is None:
        raise PreparationError("Media has no audio stream")
    # WAV has no start_time; zero is appropriate when all streams omit it.
    starts = [number(item["start_time"]) for item in streams if "start_time" in item]
    origin = number(metadata["format"].get("start_time", min(starts, default=Decimal(0))))
    reported_duration = metadata["format"].get("duration")
    if reported_duration is not None:
        # Some demuxers report an absolute end, others a duration. This is only
        # a conservative early rejection; decoded output remains authoritative.
        minimum_duration = number(reported_duration) - max(origin, Decimal(0))
        if minimum_duration > Decimal(MAX_DURATION_MS) / 1000:
            raise PreparationError("Reported media duration exceeds 120 minutes")
    media_type = "video" if video else "audio"
    playback = output_dir / ("playback.mp4" if video else "playback.m4a")
    wav = output_dir / "recognition.wav"
    audio_filter = f"asetpts=PTS-({origin})/TB,aresample=48000:async=1:first_pts=0"
    filters = f"[0:{audio['index']}]{audio_filter}[a]"
    if video:
        filters += f";[0:{video['index']}]setpts=PTS-({origin})/TB[v]"
    base = [ffmpeg_path, "-nostdin", "-v", "error", "-n", "-xerror", "-err_detect", "explode",
            "-protocol_whitelist", "file", "-format_whitelist", FORMATS, "-copyts", "-i", str(source)]
    command = base + ["-filter_complex", filters, "-map", "[a]"]
    if video:
        command += ["-map", "[v]", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-fps_mode:v", "vfr"]
    command += ["-c:a", "aac", "-b:a", "160k", "-map_metadata", "-1", "-map_chapters", "-1",
                "-avoid_negative_ts", "disabled", "-movflags", "+faststart", str(playback)]
    execute("playback", command)  # Full selected-stream decode; no -t / -shortest.
    output = probe("playback-probe", playback)
    duration = number(output["format"].get("duration"))
    if not 0 < duration <= Decimal(MAX_DURATION_MS) / 1000:
        raise PreparationError("Decoded media duration is invalid or exceeds 120 minutes")
    if abs(number(output["format"].get("start_time", 0))) > Decimal("0.1"):
        raise PreparationError("Playback did not retain the normalized timeline origin")
    # Decode the original audio, not lossy AAC, and pad its tail to the video end.
    samples = int((duration * 16000).to_integral_value(rounding=ROUND_CEILING))
    execute("recognition", base + ["-map", f"0:{audio['index']}", "-vn", "-af",
            f"asetpts=PTS-({origin})/TB,aresample=16000:async=1:first_pts=0,apad=whole_len={samples}",
            "-ac", "1", "-c:a", "pcm_s16le", "-map_metadata", "-1", str(wav)])
    with wave.open(str(wav), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
            raise PreparationError("Invalid recognition PCM format")
        wav_duration = Decimal(reader.getnframes()) / 16000
    if abs(wav_duration - duration) > Decimal("0.1"):
        raise PreparationError("Recognition and playback durations disagree")
    duration_ms = int((max(duration, wav_duration) * 1000).to_integral_value(rounding=ROUND_CEILING))
    if duration_ms > MAX_DURATION_MS:
        raise PreparationError("Decoded media exceeds 120 minutes")
    result = PreparedMedia(wav, playback, duration_ms, media_type, str(origin))
    if cancel is not None and cancel.is_set():
        raise ProcessCancelled("Local processing cancelled")
    manifest = {"version": 1, "media_type": media_type, "duration_ms": duration_ms,
                "source_origin_seconds": str(origin), "mapping": "source_pts_seconds - source_origin_seconds",
                "asr_offset_ms": 0, "audio": wav.name, "playback": playback.name,
                "source_probe": metadata, "playback_probe": output}
    (output_dir / "prepared.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return result
