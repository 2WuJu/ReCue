"""Read-only sample inventory for M0, without importing media into the queue."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from app.config import ROOT
from app.storage.uploads import MAX_UPLOAD_BYTES


class InspectionError(ValueError):
    pass


def positive_duration(value):
    try:
        duration = Decimal(str(value))
    except InvalidOperation:
        return None
    return duration if duration.is_finite() and duration > 0 else None


def inspect_sample(path: Path, *, ffprobe="ffprobe", timeout=30):
    """Hash in bounded chunks and retain probe timestamps without remapping them.

    Probing headers does not establish full decodability or ASR readiness.
    This function never writes the input, application storage, or database.
    """
    if not path.is_absolute():
        raise InspectionError("Sample path must be absolute")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise InspectionError("Sample must be a regular file")
    before = path.stat()
    if not before.st_size:
        raise InspectionError("Sample is empty")
    executable = shutil.which(ffprobe)
    if not executable:
        raise InspectionError("FFprobe missing: install FFmpeg or pass --ffprobe")
    try:
        version = subprocess.run(
            [executable, "-version"], capture_output=True, check=True, timeout=10,
        ).stdout.decode("utf-8", errors="replace").splitlines()[0]
        probe = subprocess.run([
            executable, "-v", "error", "-protocol_whitelist", "file",
            "-show_entries",
            "format=format_name,start_time,duration:"
            "stream=index,codec_type,codec_name,time_base,start_time,duration,"
            "sample_rate,channels,width,height:stream_disposition=attached_pic",
            "-of", "json", "-i", str(path),
        ], capture_output=True, check=True, timeout=timeout)
        metadata = json.loads(probe.stdout)
    except subprocess.TimeoutExpired as exc:
        raise InspectionError("FFprobe timed out; no sample report was produced") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[-2000:]
        raise InspectionError(f"FFprobe failed: {detail}") from exc
    except (ValueError, IndexError) as exc:
        raise InspectionError("FFprobe returned invalid metadata") from exc

    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    after = path.stat()
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if identity(before) != identity(after):
        raise InspectionError("Sample changed during inspection; retry with a stable file")
    streams = metadata.get("streams", [])
    duration = positive_duration(metadata.get("format", {}).get("duration"))
    issues = []
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        issues.append("no_audio_stream")
    if duration is None:
        issues.append("unknown_duration")
    elif duration > 120 * 60:
        issues.append("duration_exceeds_120_minutes")
    if before.st_size > MAX_UPLOAD_BYTES:
        issues.append("size_exceeds_4_gib")
    return {
        "schema_version": 1,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "sample": {"path": str(path), "size_bytes": before.st_size,
                   "mtime_ns": before.st_mtime_ns, "sha256": checksum.hexdigest()},
        "ffprobe": {"path": executable, "version": version},
        "probe": metadata,
        "screening_issues": issues,
        "validation": {"full_decode": "not_run", "recognition": "not_run",
                       "quality": "not_measured", "performance": "not_measured",
                       "timeline_mapping": "not_verified"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path, help="Absolute path to a real course file")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable name or path")
    parser.add_argument("--output", type=Path, help="New JSON report inside project .local/")
    args = parser.parse_args()
    try:
        output = args.output.resolve() if args.output else None
        if output and not output.is_relative_to((ROOT / ".local").resolve()):
            raise InspectionError("Reports must stay inside project .local/")
        report = inspect_sample(args.sample, ffprobe=args.ffprobe)
        contents = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as handle:
                handle.write(contents)
            print(f"Sample inventory saved: {output}")
        else:
            print(contents, end="")
        return 2 if report["screening_issues"] else 0
    except (InspectionError, OSError) as exc:
        parser.exit(1, f"Sample inspection failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
