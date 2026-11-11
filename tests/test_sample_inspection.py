"""Real FFprobe checks on generated media, never course/ASR acceptance."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.media.inspect import InspectionError, inspect_sample


@unittest.skipUnless(shutil.which("ffprobe"), "FFprobe required")
class SampleInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Deliberately misleading extension; inspect content rather than suffix.
        self.sample = Path(self.temp.name) / "中文 sample.mp4"
        with wave.open(str(self.sample), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)

    def test_content_probe_hash_and_explicit_validation_limits(self):
        original = self.sample.read_bytes()
        report = inspect_sample(self.sample)
        self.assertEqual(report["probe"]["format"]["format_name"], "wav")
        self.assertEqual(report["probe"]["format"]["duration"], "1.000000")
        self.assertEqual(report["probe"]["streams"][0]["sample_rate"], "16000")
        self.assertEqual(report["sample"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(report["screening_issues"], [])
        self.assertEqual(report["validation"]["recognition"], "not_run")
        self.assertEqual(report["validation"]["full_decode"], "not_run")
        self.assertEqual(self.sample.read_bytes(), original)

    def test_bad_paths_missing_tool_and_truncated_media(self):
        with self.assertRaisesRegex(InspectionError, "absolute"):
            inspect_sample(Path("relative.wav"))
        with self.assertRaisesRegex(InspectionError, "missing"):
            inspect_sample(self.sample, ffprobe="recue-missing-ffprobe")
        self.sample.write_bytes(self.sample.read_bytes()[:12])
        with self.assertRaisesRegex(InspectionError, "FFprobe failed"):
            inspect_sample(self.sample)
        self.sample.write_bytes(b"")
        with self.assertRaisesRegex(InspectionError, "empty"):
            inspect_sample(self.sample)

    def test_probe_timeout_does_not_return_success_report(self):
        with patch("app.media.inspect.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 1)):
            with self.assertRaisesRegex(InspectionError, "timed out"):
                inspect_sample(self.sample)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_video_without_audio_is_reported_and_cli_exits_two(self):
        video = self.sample.parent / "silent.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=1",
            "-t", "1", "-c:v", "ffv1", str(video),
        ], check=True, capture_output=True, timeout=15)
        result = subprocess.run([
            sys.executable, "-m", "app.media.inspect", str(video),
        ], cwd=ROOT / "backend", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["screening_issues"], ["no_audio_stream"])

    def test_cli_reports_stay_local_and_never_overwrite(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".local") as directory:
            output = Path(directory) / "sample.json"
            command = [sys.executable, "-m", "app.media.inspect", str(self.sample), "--output"]
            outside = self.sample.parent / "report.json"
            rejected = subprocess.run(command + [str(outside)], cwd=ROOT / "backend",
                                      capture_output=True, timeout=15)
            self.assertEqual(rejected.returncode, 1)
            self.assertFalse(outside.exists())
            first = subprocess.run(command + [str(output)], cwd=ROOT / "backend",
                                   capture_output=True, timeout=15)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = subprocess.run(command + [str(output)], cwd=ROOT / "backend",
                                    capture_output=True, timeout=15)
            self.assertEqual(second.returncode, 1)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
