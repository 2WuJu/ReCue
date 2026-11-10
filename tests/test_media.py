"""HTTP metadata/queue checks; no recognition results or course claims."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from http_support import HTTPAPICase
from app.config import Settings
from app.repositories.database import connect, migrate
from app.repositories.queue import claim_next, fail_owned
from app.services.maintenance import maintain_uploads
from app.storage.uploads import recover_uploads, upload_lock


class MediaHTTPTests(HTTPAPICase):
    def upload(self):
        code, receipt = self.http("POST", "/api/media?display_name=course.wav", b"arbitrary bytes", {
            "X-Upload-ID": str(uuid4()), "Content-Type": "application/octet-stream",
        })
        self.assertEqual(code, 201)
        return receipt

    def test_empty_detail_and_stable_pagination_with_timestamp_ties(self):
        self.assertEqual(self.http("GET", "/api/media"), (200, {"items": [], "next_cursor": None}))
        receipts = [self.upload() for _ in range(5)]
        with connect(self.data / "recue.sqlite3") as db, db:
            db.execute("UPDATE media SET created_at=100")
        expected = sorted((item["media_id"] for item in receipts), reverse=True)
        first = self.http("GET", "/api/media?limit=2")[1]
        self.assertEqual([item["id"] for item in first["items"]], expected[:2])
        newer = self.upload()
        cursor, seen = first["next_cursor"], [item["id"] for item in first["items"]]
        while cursor:
            code, page = self.http("GET", "/api/media?limit=2&cursor=" + quote(cursor))
            self.assertEqual(code, 200)
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, expected)
        self.assertNotIn(newer["media_id"], seen)
        code, detail = self.http("GET", "/api/media/" + receipts[0]["media_id"])
        self.assertEqual(code, 200)
        self.assertEqual(detail["latest_job"]["id"], receipts[0]["job_id"])
        self.assertEqual(detail["latest_job"]["status"], "queued")
        self.assertIsNone(detail["media_type"])
        self.assertIsNone(detail["duration_ms"])
        self.assertNotIn("source_path", detail)
        self.assertNotIn("playback_path", detail)
        self.assertNotIn("worker_id", detail["latest_job"])
        self.stop()
        self.start()
        self.assertEqual(self.http("GET", "/api/media/" + detail["id"]), (200, detail))

    def test_concurrent_retry_creates_one_attempt_and_exposes_latest_status(self):
        receipt = self.upload()
        database = self.data / "recue.sqlite3"
        job = claim_next(database, "test-owner")
        fail_owned(database, job["id"], "test-owner", "decode_failed", "cannot decode")
        url = "/api/media/" + receipt["media_id"]
        detail = self.http("GET", url)[1]
        self.assertEqual(detail["latest_job"]["status"], "failed")
        self.assertEqual(detail["latest_job"]["error_code"], "decode_failed")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.http("POST", url + "/retry"), range(8)))
        self.assertEqual([code for code, _ in results].count(201), 1)
        self.assertEqual([code for code, _ in results].count(409), 7)
        latest = self.http("GET", url)[1]["latest_job"]
        self.assertEqual(latest["status"], "queued")
        self.assertEqual(latest["attempt"], 2)
        self.assertIsNone(latest["error_code"])
        self.assertEqual(self.http("GET", "/api/media")[1]["items"][0]["latest_job"], latest)
        with connect(database) as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY attempt").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["error_message"], "cannot decode")
        self.assertEqual(rows[1]["id"], latest["id"])

    def test_interrupted_retry_and_deleting_conflict(self):
        receipt = self.upload()
        database = self.data / "recue.sqlite3"
        with connect(database) as db, db:
            db.execute("UPDATE jobs SET status='interrupted', finished_at=1")
        url = "/api/media/" + receipt["media_id"]
        self.assertEqual(self.http("POST", url + "/retry")[0], 201)
        with connect(database) as db, db:
            db.execute("UPDATE jobs SET status='failed'")
            db.execute("UPDATE media SET lifecycle='deleting'")
        self.assertEqual(self.http("POST", url + "/retry")[0], 409)
        self.assertEqual(self.http("GET", url)[1]["lifecycle"], "deleting")
        self.assertEqual(self.http("POST", url + "/retry", headers={"Origin": "https://other.example"})[0], 403)

    def test_validation_unknown_media_and_error_schema(self):
        for method, url, expected in [
            ("GET", f"/api/media/{uuid4()}", 404),
            ("POST", f"/api/media/{uuid4()}/retry", 404),
            ("GET", "/api/media/invalid", 422),
            ("POST", "/api/media/invalid/retry", 422),
            ("GET", "/api/media?limit=0", 422),
            ("GET", "/api/media?limit=101", 422),
            ("GET", "/api/media?cursor=invalid", 422),
            ("GET", "/api/media?cursor=" + "x" * 257, 422),
        ]:
            with self.subTest(url=url):
                status, error = self.http(method, url)
                self.assertEqual(status, expected)
                self.assertEqual(set(error), {"code", "message", "retryable"})
        for invalid in ([True, str(uuid4())], [-1, str(uuid4())], [2**64, str(uuid4())], {}, [1, "bad"]):
            cursor = base64.urlsafe_b64encode(json.dumps(invalid).encode()).decode()
            self.assertEqual(self.http("GET", "/api/media?cursor=" + quote(cursor))[0], 422)
        schema = self.http("GET", "/openapi.json")[1]
        self.assertIn("get", schema["paths"]["/api/media"])
        self.assertIn("/api/media/{media_id}/retry", schema["paths"])


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = Settings(self.data, ROOT / "frontend/dist", None, "ffmpeg", "127.0.0.1", 4318)
        self.settings.prepare()
        migrate(self.settings.database)

    async def test_periodic_recovery_skips_active_upload_and_recovers_after_scan_failure(self):
        temporary = self.data / "tmp" / f"{uuid4()}.part"
        temporary.write_bytes(b"partial")
        stop = asyncio.Event()
        with upload_lock(self.data):
            task = asyncio.create_task(maintain_uploads(self.settings, stop, interval=.01))
            try:
                await asyncio.sleep(.05)
                self.assertTrue(temporary.exists())
            finally:
                stop.set()
                await task
        stop = asyncio.Event()
        calls = 0
        def failing_scan(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("test storage unavailable")
            return recover_uploads(*args)
        with patch("app.services.maintenance.recover_uploads", side_effect=failing_scan):
            task = asyncio.create_task(maintain_uploads(self.settings, stop, interval=.01))
            try:
                deadline = time.monotonic() + 5
                while temporary.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertFalse(temporary.exists())
                self.assertGreaterEqual(calls, 2)
            finally:
                stop.set()
                await task

    async def test_stop_wakes_hourly_wait_without_another_scan(self):
        stop = asyncio.Event()
        with patch("app.services.maintenance.recover_uploads") as scan:
            task = asyncio.create_task(maintain_uploads(self.settings, stop))
            await asyncio.sleep(.01)
            stop.set()
            await asyncio.wait_for(task, timeout=1)
            scan.assert_not_called()
