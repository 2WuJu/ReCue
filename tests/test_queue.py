"""T03 storage/queue checks; fixtures are metadata, not recognition samples."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.repositories.database import MIGRATIONS, connect, migrate
from app.repositories.queue import QueueConflict, claim_next, fail_owned, received, retry


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "recue.sqlite3"
        migrate(self.path)

    def receive(self, request_id=None, size_bytes=100):
        media_id = str(uuid4())
        return received(self.path, request_id=request_id or str(uuid4()), media_id=media_id,
                        display_name="中文课程.mp4", source_path=f"media/{media_id}/source",
                        size_bytes=size_bytes)

    def rows(self, table):
        with connect(self.path) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def test_upgrade_v1_retains_worker_and_is_repeatable(self):
        path = self.directory / "v1.sqlite3"
        with connect(path) as db, db:
            for statement in MIGRATIONS[0][1]:
                db.execute(statement)
            db.execute("INSERT INTO worker_status VALUES (1, 'old', 1, 0, 'stopped')")
            db.execute("PRAGMA user_version = 1")
        migrate(path)
        migrate(path)
        with connect(path) as db:
            self.assertEqual(db.execute("SELECT worker_id FROM worker_status").fetchone()[0], "old")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_receipt_is_atomic_and_idempotent(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.receive("same-request"), range(8)))
        self.assertTrue(all(item == receipts[0] for item in receipts))
        self.assertEqual(len(self.rows("media")), 1)
        self.assertEqual(len(self.rows("jobs")), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.receive(size_bytes=0)
        self.assertEqual(len(self.rows("media")), 1)

    def test_fifo_global_serial_claim_and_attempt_history(self):
        first, second = self.receive(), self.receive()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda n: claim_next(self.path, f"worker-{n}"), range(8)))
        claimed = [item for item in claims if item]
        self.assertEqual(len(claimed), 1)
        job = claimed[0]
        self.assertEqual(job["id"], first["job_id"])
        self.assertEqual(job["stage"], "preprocessing")
        with self.assertRaises(QueueConflict):
            fail_owned(self.path, job["id"], "wrong-owner", "decode_failed", "invalid")
        fail_owned(self.path, job["id"], job["worker_id"], "decode_failed", "invalid")

        def retry_once(_):
            try:
                return retry(self.path, first["media_id"])
            except QueueConflict:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            attempts = list(pool.map(retry_once, range(8)))
        self.assertEqual(len([item for item in attempts if item]), 1)
        self.assertEqual(claim_next(self.path, "next")["id"], second["job_id"])
        history = [item for item in self.rows("jobs") if item["media_id"] == first["media_id"]]
        self.assertEqual([item["attempt"] for item in history], [1, 2])
        self.assertEqual(history[0]["error_code"], "decode_failed")
        with self.assertRaises(QueueConflict):
            fail_owned(self.path, job["id"], job["worker_id"], "late", "stale write")

    def test_database_rejects_duplicate_active_attempt_and_parallel_running(self):
        first, second = self.receive(), self.receive()
        with connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError), db:
                db.execute("""INSERT INTO jobs(id, media_id, status, attempt, created_at)
                              VALUES ('duplicate', ?, 'queued', 2, 0)""", (first["media_id"],))
        claim_next(self.path, "worker")
        with connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError), db:
                db.execute("""UPDATE jobs SET status='running', worker_id='rogue', started_at=0
                              WHERE id=?""", (second["job_id"],))

    def test_deleting_media_blocks_claim_retry_and_late_write(self):
        receipt = self.receive()
        job = claim_next(self.path, "owner")
        with connect(self.path) as db, db:
            db.execute("UPDATE media SET lifecycle='deleting'")
        with self.assertRaises(QueueConflict):
            fail_owned(self.path, job["id"], "owner", "failed", "late")
        with self.assertRaises(QueueConflict):
            retry(self.path, receipt["media_id"])
        # Separate queued media marked for deletion is not eligible for claim.
        with connect(self.path) as db, db:
            db.execute("UPDATE jobs SET status='cancelled'")
        self.receive()
        with connect(self.path) as db, db:
            db.execute("UPDATE media SET lifecycle='deleting'")
        self.assertIsNone(claim_next(self.path, "owner"))

    def test_worker_restart_recovers_running_only_under_instance_lock(self):
        running, queued = self.receive(), self.receive()
        claim_next(self.path, "exited-worker")
        env = {**os.environ, "RECUE_DATA_DIR": str(self.directory)}

        def launch():
            return subprocess.Popen([sys.executable, "-m", "app.worker"],
                                    cwd=ROOT / "backend", env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        worker = launch()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.rows("worker_status"):
                    break
                time.sleep(.05)
            else:
                self.fail("Worker did not start")
            jobs = {item["id"]: item for item in self.rows("jobs")}
            self.assertEqual(jobs[running["job_id"]]["status"], "interrupted")
            self.assertEqual(jobs[queued["job_id"]]["status"], "queued")
            self.assertEqual(jobs[running["job_id"]]["error_code"], "worker_interrupted")
            # Stand in for a future processor while this worker holds the OS lock.
            current = claim_next(self.path, self.rows("worker_status")[0]["worker_id"])
            duplicate = launch()
            try:
                self.assertNotEqual(duplicate.wait(timeout=10), 0)
            finally:
                if duplicate.poll() is None:
                    duplicate.terminate()
                    duplicate.wait(timeout=10)
            # API startup migration cannot recover a live job, even with stale heartbeat.
            with connect(self.path) as db, db:
                db.execute("UPDATE jobs SET heartbeat=0 WHERE id=?", (current["id"],))
            migrate(self.path)
            self.assertEqual(self.rows("jobs")[1]["status"], "running")
            retry_id = retry(self.path, running["media_id"])
            self.assertIsNotNone(retry_id)
        finally:
            worker.terminate()
            worker.wait(timeout=10)
        replacement = launch()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.rows("jobs")[1]["status"] == "interrupted":
                    break
                time.sleep(.05)
            else:
                self.fail("Replacement worker did not recover running job")
            self.assertEqual(self.rows("jobs")[2]["status"], "queued")
        finally:
            replacement.terminate()
            replacement.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
