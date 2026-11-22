"""Regression coverage for catalog state and resource-aware queue claims."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings
from app.repositories.database import connect, migrate
from app.repositories.queue import ModelDisabled, change_queued_model, claim_next, received, set_model_disabled
from app.worker.configuration import file_signature, model_catalog, model_wait_reasons


class ModelAvailabilityTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.settings = Settings(self.root, self.root, None, "ffmpeg", "127.0.0.1", 4318)
        self.db = self.settings.database
        migrate(self.db)
        self.large = dict(name="large", status="verified", is_default=True,
                          required_memory_mib=4000, memory_source="reference")
        self.small = dict(name="small", status="verified", is_default=False,
                          required_memory_mib=1000, memory_source="reference")
        for entry in (self.large, self.small):
            path = self.root / entry["name"]
            path.write_bytes(b"weight")
            entry.update(path=str(path), signature=file_signature(path))
        self.entries = {entry["name"]: entry for entry in (self.large, self.small)}

    def heartbeat(self, *extra):
        catalog = json.dumps([*self.entries.values(), *extra])
        with connect(self.db) as db, db:
            db.execute("""INSERT INTO worker_status
                (singleton, worker_id, pid, heartbeat, state, models_json)
                VALUES (1, 'worker', 1, ?, 'processing', ?)""", (time.time(), catalog))

    def enqueue(self, media_id, model):
        return received(self.db, request_id=media_id, media_id=media_id,
                        display_name=media_id, source_path=media_id,
                        size_bytes=1, requested_model=model)["job_id"]

    def claim(self, available):
        return claim_next(self.db, "worker", default_model="large",
                          model_revisions={"large": 0, "small": 0},
                          blocked_models=model_wait_reasons(self.entries, available))

    def job(self, job_id):
        with connect(self.db) as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_catalog_keeps_failures_and_applies_fresh_memory_and_admin_state(self):
        self.heartbeat(dict(name="other", status="unsupported"))
        with patch("app.worker.configuration.available_memory", return_value=3900):
            by_name = {row["name"]: row for row in model_catalog(self.settings)}
        self.assertEqual(by_name["other"]["status"], "unsupported")
        self.assertEqual(by_name["large"]["status"], "memory_insufficient")
        self.assertTrue(by_name["small"]["selectable"])
        self.assertNotIn("path", by_name["large"])

        with patch("app.worker.configuration.available_memory", return_value=4100):
            self.assertEqual(model_catalog(self.settings)[0]["status"], "below_recommended")
        with connect(self.db) as db, db:
            db.execute("INSERT INTO model_controls(name, disabled) VALUES ('large', 1)")
        self.assertEqual(model_catalog(self.settings)[0]["status"], "disabled")

    def test_low_memory_does_not_starve_other_jobs_and_recovers(self):
        large_id = self.enqueue("first", "large")
        small_id = self.enqueue("second", "small")
        self.assertEqual(self.claim(2000)["id"], small_id)
        self.assertIn("内存", self.job(large_id)["wait_reason"])
        with connect(self.db) as db, db:
            db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (small_id,))
        self.assertEqual(self.claim(6000)["id"], large_id)
        self.assertIsNone(self.job(large_id)["wait_reason"])

    def test_disable_blocks_stale_uploads_and_allows_queued_model_replacement(self):
        queued = self.enqueue("first", "large")
        self.heartbeat()
        with connect(self.db) as db, db:
            set_model_disabled(db, "large", True)
        self.assertIn("停用", self.job(queued)["wait_reason"])
        with self.assertRaises(ModelDisabled):
            self.enqueue("second", "large")
        self.assertIsNone(self.claim(6000))
        self.assertIn("停用", self.job(queued)["wait_reason"])
        change_queued_model(self.db, "first", "small")
        self.assertEqual(self.claim(6000)["requested_model"], "small")
