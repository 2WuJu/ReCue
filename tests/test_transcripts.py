"""Transaction and HTTP checks; metadata examples do not establish ASR quality."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.repositories.database import connect, migrate
from app.repositories.queue import QueueConflict, claim_next, received
from app.repositories.transcripts import get_transcript, publish
from app.transcription.whisper import TranscriptionError, parse_result
from http_support import HTTPAPICase


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "recue.sqlite3"
        migrate(self.database)
        self.media_id = str(uuid4())
        received(self.database, request_id=str(uuid4()), media_id=self.media_id,
                 display_name="test.wav", source_path=f"media/{self.media_id}/source", size_bytes=100)
        self.job = claim_next(self.database, "owner")

    def publish(self, segments=None, owner="owner"):
        publish(self.database, self.job["id"], owner, duration_ms=1000, media_type="audio",
                segments=segments if segments is not None else [
                    {"start_ms": 0, "end_ms": 200, "text": "数据库测试"},
                    {"start_ms": 500, "end_ms": 900, "text": "保留静音间隙"}],
                engine="test-metadata", model="not-asr", processing_version="test")

    def test_publication_is_complete_stable_and_repeat_is_rejected(self):
        self.assertEqual(get_transcript(self.database, self.media_id), {"ready": False})
        self.publish()
        result = get_transcript(self.database, self.media_id)
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result, get_transcript(self.database, self.media_id))
        self.assertEqual(result["segments"][1]["start_ms"], 500)
        with self.assertRaises(QueueConflict):
            self.publish()
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "succeeded")

    def test_mid_publication_failure_rolls_back_all_changes(self):
        with connect(self.database) as db, db:
            db.execute("""CREATE TRIGGER fail_second_segment BEFORE INSERT ON transcript_segments
                          WHEN NEW.sequence=1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "running")
            self.assertIsNone(db.execute("SELECT duration_ms FROM media").fetchone()[0])

    def test_old_owner_deleting_and_cancelled_jobs_cannot_publish(self):
        with self.assertRaises(QueueConflict):
            self.publish(owner="stale-owner")
        with connect(self.database) as db, db:
            db.execute("UPDATE media SET lifecycle='deleting'")
        with self.assertRaises(QueueConflict):
            self.publish()
        with connect(self.database) as db, db:
            db.execute("UPDATE media SET lifecycle='active'")
            db.execute("UPDATE jobs SET cancel_requested=1")
        with self.assertRaises(QueueConflict):
            self.publish()
        self.assertEqual(get_transcript(self.database, self.media_id), {"ready": False})

    def test_silence_can_publish_an_empty_transcript(self):
        self.publish(segments=[])
        self.assertEqual(get_transcript(self.database, self.media_id)["segments"], [])

    def test_raw_timestamp_validation_rejects_overflow_and_missing_data(self):
        def payload(start, end, text="语音"):
            return {"transcription": [{"offsets": {"from": start, "to": end}, "text": text}]}
        self.assertEqual(parse_result(payload(100, 500), 1000)[0]["start_ms"], 100)
        for data in [payload(-1, 100), payload(100, 100), payload(0, 1001),
                     payload(False, 100), payload(0, 100, " "), {},
                     {"transcription": [{"text": "没有时间戳"}]}]:
            with self.subTest(data=data), self.assertRaises(TranscriptionError):
                parse_result(data, 1000)
        with self.assertRaises(TranscriptionError):
            parse_result({"transcription": [
                {"offsets": {"from": 500, "to": 700}, "text": "后"},
                {"offsets": {"from": 0, "to": 200}, "text": "前"}]}, 1000)


class TranscriptHTTPTests(HTTPAPICase):
    def test_unknown_pending_published_and_restart(self):
        media_id = str(uuid4())
        path = f"/api/media/{media_id}/transcript"
        self.assertEqual(self.http("GET", path)[0], 404)
        received(self.data / "recue.sqlite3", request_id=str(uuid4()), media_id=media_id,
                 display_name="test.wav", source_path=f"media/{media_id}/source", size_bytes=100)
        self.assertEqual(self.http("GET", path)[0], 409)
        job = claim_next(self.data / "recue.sqlite3", "owner")
        publish(self.data / "recue.sqlite3", job["id"], "owner", duration_ms=1000,
                media_type="audio", segments=[{"start_ms": 0, "end_ms": 900, "text": "HTTP测试"}],
                engine="test-metadata", model="not-asr", processing_version="test")
        status, result = self.http("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["segments"][0]["text"], "HTTP测试")
        self.stop()
        self.start()
        self.assertEqual(self.http("GET", path), (200, result))
