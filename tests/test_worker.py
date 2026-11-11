"""Pipeline transaction/race checks. ASR fixtures are not quality evidence."""
import json
import hashlib
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings
from app.media.prepare import PreparedMedia, PreparationError
from app.processes import ProcessCleanupError
from app.repositories.database import connect, migrate
from app.repositories.queue import claim_next, received
from app.worker.configuration import ProcessingConfig, load_processing
from app.worker.processor import process_attempt


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = Settings(self.data, ROOT / "frontend/dist", None, "ffmpeg", "127.0.0.1", 4318)
        self.settings.prepare()
        migrate(self.settings.database)
        self.media_id = str(uuid4())
        source = self.data / "media" / self.media_id / "source"
        source.parent.mkdir()
        source.write_bytes(b"pipeline fixture")
        received(self.settings.database, request_id=str(uuid4()), media_id=self.media_id,
                 display_name="fixture", source_path=source.relative_to(self.data).as_posix(), size_bytes=16)
        self.snapshot = dict(engine="whisper.cpp", model_name="fixture", backend="cpu",
                             processing_version="test", timeline_mapping="source_pts_seconds - source_origin_seconds")
        self.config = ProcessingConfig(source, source, "ffmpeg", "ffprobe", self.snapshot)
        self.job = claim_next(self.settings.database, "owner", configuration=self.snapshot)
        self.stop = threading.Event()

    def row(self, table):
        with connect(self.settings.database) as db:
            return dict(db.execute(f"SELECT * FROM {table}").fetchone())

    def prepared(self, source, directory, **kwargs):
        directory.mkdir(parents=True)
        playback = directory / "playback.m4a"
        playback.write_bytes(b"fixture")
        return PreparedMedia(source, playback, 1000, "audio", "5")

    def run_attempt(self, transcription=None):
        with patch("app.worker.processor.prepare", side_effect=self.prepared), \
             patch("app.worker.processor.transcribe", side_effect=transcription or (lambda *a, **kw: [
                 dict(start_ms=0, end_ms=500, text="transaction fixture")])):
            process_attempt(self.settings, self.config, self.job, self.stop)

    def test_configuration_and_playback_publish_together(self):
        self.run_attempt()
        self.assertEqual(self.row("jobs")["status"], "succeeded")
        self.assertEqual(json.loads(self.row("jobs")["parameters_json"])["source_origin_seconds"], "5")
        self.assertTrue((self.data / self.row("media")["playback_path"]).is_file())
        self.assertEqual(self.row("transcripts")["job_id"], self.job["id"])

    def test_cancellation_and_deletion_before_publish_do_not_expose_artifacts(self):
        def cancel(*args, **kwargs):
            with connect(self.settings.database) as db, db:
                db.execute("UPDATE media SET lifecycle='deleting'")
                db.execute("UPDATE jobs SET cancel_requested=1")
            return [dict(start_ms=0, end_ms=500, text="must not publish")]
        self.run_attempt(cancel)
        self.assertEqual(self.row("jobs")["status"], "cancelled")
        self.assertIsNone(self.row("media")["playback_path"])
        with connect(self.settings.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)

    def test_cleanup_failure_is_fatal_and_keeps_running_ownership(self):
        with patch("app.worker.processor.prepare", side_effect=ProcessCleanupError("injected")):
            with self.assertRaises(ProcessCleanupError):
                process_attempt(self.settings, self.config, self.job, self.stop)
        self.assertEqual(self.row("jobs")["status"], "running")

    def test_decode_failure_fails_attempt_without_publication(self):
        with patch("app.worker.processor.prepare", side_effect=PreparationError("injected")):
            process_attempt(self.settings, self.config, self.job, self.stop)
        self.assertEqual(self.row("jobs")["status"], "failed")
        self.assertIsNone(self.row("media")["playback_path"])

    def test_stop_before_work_is_interrupted(self):
        self.stop.set()
        self.run_attempt()
        self.assertEqual(self.row("jobs")["status"], "interrupted")

    def test_missing_configuration_never_silently_selects_model(self):
        with self.assertRaisesRegex(ValueError, "RECUE_PROCESSING_CONFIG"):
            load_processing(self.settings)

    def test_configuration_checks_hashes_and_records_actual_fixed_parameters(self):
        resource = self.data / "resource"
        resource.write_bytes(b"deployment fixture")
        digest = hashlib.sha256(resource.read_bytes()).hexdigest()
        payload = dict(version=1, backend="cpu", executable=str(resource), model=str(resource),
                       executable_sha256=digest, model_sha256=digest, model_name="fixture",
                       engine_version="fixture", engine_commit="fixed", model_revision="fixed",
                       threads=999, vad=True)
        path = self.data / "processing.json"
        path.write_text(json.dumps(payload))
        settings = replace(self.settings, processing_config=path)
        loaded = load_processing(settings)
        self.assertEqual(loaded.snapshot["threads"], 8)
        self.assertFalse(loaded.snapshot["vad"])
        resource.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            load_processing(settings)
        for value in ([], {**payload, "backend": "vulkan"}, {**payload, "version": True}):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_processing(settings)

    def test_final_status_write_failure_rolls_back_playback_and_transcript(self):
        import sqlite3
        with connect(self.settings.database) as db, db:
            db.execute("""CREATE TRIGGER fail_success BEFORE UPDATE OF status ON jobs
                WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_attempt()
        self.assertIsNone(self.row("media")["playback_path"])
        self.assertEqual(self.row("jobs")["status"], "running")
        with connect(self.settings.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)
