"""Real FFmpeg signal/timestamp checks, not speech quality or browser acceptance."""
from array import array
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.media.prepare import PreparationError, prepare
from app.processes import ProcessCancelled


@unittest.skipUnless(sys.platform == "win32" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Windows FFmpeg/FFprobe required")
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ReCue 媒体 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def ffmpeg(self, args):
        return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *args],
                              check=True, capture_output=True, timeout=20).stdout

    def fixture(self, origin=0, audio_delay=0, video_delay=0, gap=False):
        source = self.directory / "source.mkv"
        audio = (f"aselect='not(between(t,0.7,1.2))'," if gap else "")
        self.ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=2,"
                     "drawbox=color=white:t=fill:enable='gte(t,1)'", "-f", "lavfi", "-i",
                     "sine=frequency=1000:sample_rate=16000:duration=2",
                     "-filter_complex", f"[0:v]setpts=PTS+({origin + video_delay})/TB[v];"
                     f"[1:a]{audio}asetpts=PTS+({origin + audio_delay})/TB[a]",
                     "-map", "[v]", "-map", "[a]", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source)])
        return source

    def samples(self, path):
        with wave.open(str(path), "rb") as reader:
            self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()), (1, 2, 16000))
            return array("h", reader.readframes(reader.getnframes()))

    def energy(self, samples, start, end):
        window = samples[int(start * 16000):int(end * 16000)]
        self.assertTrue(window)
        return sum(abs(item) for item in window) / len(window)

    def playback_samples(self, playback):
        decoded = self.directory / "decoded.wav"
        self.ffmpeg(["-i", str(playback), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(decoded)])
        return self.samples(decoded)

    def test_nonzero_origin_keeps_audio_delay_and_video_marker(self):
        source = self.fixture(origin=5, audio_delay=0.5)
        before = source.read_bytes()
        result = prepare(source, self.directory / "prepared")
        self.assertEqual(result.source_origin_seconds, "5.000000")
        self.assertTrue(2490 <= result.duration_ms <= 2510)
        self.assertEqual(result.media_type, "video")
        for samples in (self.samples(result.audio), self.playback_samples(result.playback)):
            self.assertLess(self.energy(samples, 0.1, 0.4), 10)
            self.assertGreater(self.energy(samples, 0.55, 0.65), 1000)
        # The original video switches to white at original PTS 6, normalized t=1.
        for position, expected_white in ((0.8, False), (1.2, True)):
            frame = self.ffmpeg(["-i", str(result.playback), "-ss", str(position), "-frames:v", "1",
                                 "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
            self.assertTrue(frame)
            self.assertEqual(sum(frame) / len(frame) > 200, expected_white)
        self.assertEqual(source.read_bytes(), before)
        manifest = json.loads((result.audio.parent / "prepared.json").read_text())
        self.assertEqual(manifest["asr_offset_ms"], 0)

    def test_timestamp_gap_is_silence_and_later_audio_stays_in_place(self):
        result = prepare(self.fixture(gap=True), self.directory / "prepared")
        for samples in (self.samples(result.audio), self.playback_samples(result.playback)):
            self.assertGreater(self.energy(samples, 0.2, 0.4), 1000)
            self.assertLess(self.energy(samples, 0.85, 1.1), 10)
            self.assertGreater(self.energy(samples, 1.4, 1.6), 1000)

    def test_late_video_is_not_independently_shifted_to_zero(self):
        result = prepare(self.fixture(origin=3, video_delay=0.4), self.directory / "prepared")
        metadata = json.loads((result.audio.parent / "prepared.json").read_text())["playback_probe"]
        video = next(item for item in metadata["streams"] if item["codec_type"] == "video")
        self.assertAlmostEqual(float(video["start_time"]), 0.4, delta=0.02)
        self.assertGreater(self.energy(self.samples(result.audio), 0.1, 0.3), 1000)

    def test_wav_mp3_and_h264_aac_inputs_have_real_playback_copies(self):
        for suffix in ("wav", "mp3", "mp4"):
            with self.subTest(suffix=suffix):
                source = self.directory / f"input.{suffix}"
                inputs = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1"]
                if suffix == "mp4":
                    inputs += ["-f", "lavfi", "-i", "color=s=64x64:r=10:d=1", "-c:v", "libx264", "-c:a", "aac"]
                self.ffmpeg(inputs + [str(source)])
                result = prepare(source, self.directory / suffix)
                self.assertTrue(990 <= result.duration_ms <= 1200)
                self.assertTrue(result.playback.is_file())
                self.assertGreater(self.energy(self.samples(result.audio), 0.2, 0.4), 1000)

    def test_no_audio_corrupt_and_playlists_never_produce_ready_manifest(self):
        video = self.directory / "video.mkv"
        self.ffmpeg(["-f", "lavfi", "-i", "color=s=16x16:r=1:d=1", str(video)])
        corrupt = self.directory / "corrupt"
        corrupt.write_bytes(b"not media")
        playlist = self.directory / "playlist.m3u8"
        playlist.write_text("#EXTM3U\n#EXTINF:1\nsecret.ts\n#EXT-X-ENDLIST\n")
        for source in (video, corrupt, playlist):
            output = self.directory / f"result-{source.name}"
            with self.subTest(source=source), self.assertRaises(PreparationError):
                prepare(source, output)
            self.assertFalse((output / "prepared.json").exists())

    def test_valid_header_with_truncated_audio_fails_full_decode(self):
        source = self.directory / "truncated.wav"
        with wave.open(str(source), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * 32000)
        with source.open("r+b") as file:
            file.truncate(1001)
        output = self.directory / "truncated-result"
        with self.assertRaises(PreparationError):
            prepare(source, output)
        self.assertFalse((output / "prepared.json").exists())

    def test_limit_timeout_cancel_and_existing_output_are_not_success(self):
        source = self.fixture()
        output = self.directory / "limited"
        with patch("app.media.prepare.MAX_DURATION_MS", 500), self.assertRaisesRegex(PreparationError, "duration"):
            prepare(source, output)
        self.assertFalse((output / "prepared.json").exists())
        with self.assertRaises(FileExistsError):
            prepare(source, output)
        with self.assertRaisesRegex(PreparationError, "timed out"):
            prepare(source, self.directory / "timeout", timeout=0.0001)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProcessCancelled):
            prepare(source, self.directory / "cancelled", cancel=cancel)
        self.assertFalse((self.directory / "cancelled/prepared.json").exists())
