"""Streaming/storage checks use arbitrary bytes, never simulated ASR results."""
import asyncio
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from starlette.requests import Request
from app.config import Settings
from app.errors import ServiceError
from app.locking import AlreadyRunning
from app.repositories.database import connect, migrate
from app.repositories.queue import find_receipt, received
from app.services.uploads import receive_upload
from app.storage.uploads import recover_uploads, upload_lock


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = Settings(self.data, ROOT / "frontend/dist", None, "ffmpeg", "127.0.0.1", 4318)
        self.settings.prepare()
        migrate(self.settings.database)

    def request(self, chunks, headers=None):
        iterator = iter(chunks)
        async def receive():
            return next(iterator, {"type": "http.request", "body": b"", "more_body": False})
        return Request({"type": "http", "headers": headers or [
            (b"content-type", b"application/octet-stream"),
        ]}, receive)

    def assert_empty(self):
        self.assertEqual(list((self.data / "tmp").iterdir()), [])
        self.assertEqual(list((self.data / "media").iterdir()), [])
        with connect(self.settings.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM media").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    async def test_stream_writes_before_eof_and_memory_does_not_scale_with_file(self):
        request_id = str(uuid4())
        block = b"x" * 65536
        count = 0
        async def receive():
            nonlocal count
            if count:
                partial = list((self.data / "tmp").glob("*.part"))
                self.assertEqual(len(partial), 1)
                self.assertEqual(partial[0].stat().st_size, count * len(block))
                self.assertIsNone(find_receipt(self.settings.database, request_id))
            count += 1
            return {"type": "http.request", "body": block, "more_body": count < 512}
        request = Request({"type": "http", "headers": [
            (b"content-type", b"application/octet-stream"),
        ]}, receive)
        tracemalloc.start()
        try:
            result, created = await receive_upload(request, self.settings, request_id, "../../课程.mp4")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertTrue(created)
        self.assertLess(peak, 16 * 1024**2)
        source = self.data / "media" / result["media_id"] / "source"
        self.assertEqual(source.stat().st_size, 32 * 1024**2)
        with connect(self.settings.database) as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "queued")
        # Replaying a completed ID does not even read the new body.
        async def forbidden():
            raise AssertionError("Replay consumed body")
        replay = Request({"type": "http", "headers": []}, forbidden)
        self.assertEqual(await receive_upload(replay, self.settings, request_id, "different"), (result, False))

    async def test_limit_empty_length_mismatch_disconnect_and_timeout_cleanup(self):
        cases = [
            (self.request([{"type": "http.request", "body": b"123456", "more_body": False}]),
             {"max_bytes": 5}, "upload_too_large"),
            (self.request([]), {}, "empty_upload"),
            (self.request([{"type": "http.request", "body": b"12", "more_body": False}], [
                (b"content-type", b"application/octet-stream"), (b"content-length", b"3"),
            ]), {}, "upload_length_mismatch"),
            (self.request([{"type": "http.request", "body": b"12", "more_body": True},
                           {"type": "http.disconnect"}]), {}, "upload_interrupted"),
        ]
        for request, options, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ServiceError) as raised:
                    await receive_upload(request, self.settings, str(uuid4()), "test", **options)
                self.assertEqual(raised.exception.code, code)
                self.assert_empty()
        async def stalled():
            await asyncio.sleep(5)
        request = Request({"type": "http", "headers": [
            (b"content-type", b"application/octet-stream"),
        ]}, stalled)
        with self.assertRaises(ServiceError) as raised:
            await receive_upload(request, self.settings, str(uuid4()), "test", idle_timeout=.01)
        self.assertEqual(raised.exception.status, 408)
        self.assert_empty()

    async def test_disk_and_database_failures_leave_no_false_success(self):
        for target, error, status in [
            ("app.services.uploads.flush_file", OSError(errno.ENOSPC, "full"), 507),
            ("app.services.uploads.received", sqlite3.OperationalError("locked"), 503),
        ]:
            with self.subTest(target=target), patch(target, side_effect=error):
                request = self.request([{"type": "http.request", "body": b"bytes", "more_body": False}])
                with self.assertRaises(ServiceError) as raised:
                    await receive_upload(request, self.settings, str(uuid4()), "test")
                self.assertEqual(raised.exception.status, status)
                self.assertTrue(raised.exception.retryable)
                self.assert_empty()

    async def test_exact_byte_limit_is_accepted_and_id_collision_preserves_source(self):
        request = self.request([{"type": "http.request", "body": b"12345", "more_body": False}])
        original, _ = await receive_upload(request, self.settings, str(uuid4()), "test", max_bytes=5)
        source = self.data / "media" / original["media_id"] / "source"
        # Even an unlikely UUID collision must not remove an existing source.
        with patch("app.services.uploads.uuid4", return_value=original["media_id"]):
            request = self.request([{"type": "http.request", "body": b"other", "more_body": False}])
            with self.assertRaises(ServiceError):
                await receive_upload(request, self.settings, str(uuid4()), "test")
        self.assertEqual(source.read_bytes(), b"12345")
        self.assertEqual(list((self.data / "tmp").iterdir()), [])

    async def test_cancellation_during_commit_preserves_file_and_holds_lock(self):
        started, release = threading.Event(), threading.Event()
        request_id = str(uuid4())
        def slow_commit(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test finalizer timed out")
            return received(*args, **kwargs)
        request = self.request([{"type": "http.request", "body": b"source", "more_body": False}])
        with patch("app.services.uploads.received", side_effect=slow_commit):
            task = asyncio.create_task(receive_upload(request, self.settings, request_id, "test"))
            try:
                deadline = time.monotonic() + 5
                while not started.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                with self.assertRaises(AlreadyRunning), upload_lock(self.data):
                    pass
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        result = find_receipt(self.settings.database, request_id)
        self.assertIsNotNone(result)
        self.assertEqual((self.data / "media" / result["media_id"] / "source").read_bytes(), b"source")
        recover_uploads(self.data, self.settings.database)
        self.assertTrue((self.data / "media" / result["media_id"] / "source").exists())

    async def test_recovery_preserves_live_and_referenced_files_and_reaps_owned_orphans(self):
        partial = self.data / "tmp" / f"{uuid4()}.part"
        partial.write_bytes(b"partial")
        with upload_lock(self.data):
            recover_uploads(self.data, self.settings.database)
            self.assertTrue(partial.exists())
        owned = self.data / "media" / str(uuid4()) / "source"
        owned.parent.mkdir()
        owned.write_bytes(b"orphan")
        recent = self.data / "media" / str(uuid4()) / "source"
        recent.parent.mkdir()
        recent.write_bytes(b"recent")
        protected = self.data / "media" / str(uuid4()) / "source"
        protected.parent.mkdir()
        protected.write_bytes(b"protected")
        received(self.settings.database, request_id=str(uuid4()), media_id=protected.parent.name,
                 display_name="test", source_path=protected.relative_to(self.data).as_posix(), size_bytes=9)
        for path in (owned, protected):
            os.utime(path, (0, 0))
        unknown = self.data / "tmp" / "notes.part"
        unknown.write_bytes(b"keep")
        recover_uploads(self.data, self.settings.database)
        self.assertFalse(partial.exists())
        self.assertFalse(owned.parent.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(protected.exists())
        self.assertTrue(unknown.exists())


class UploadHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = {**os.environ, "RECUE_DATA_DIR": str(self.data)}
        self.process = None
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        self.process = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "app.api.main:create_app", "--factory", "--port", str(self.port),
        ], cwd=ROOT / "backend", env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.http("GET", "/api/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(.05)
        self.fail("API failed to start")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def http(self, method, path, body=None, headers=None, chunked=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_upload_query_replay_persistence_and_openapi(self):
        request_id = str(uuid4())
        path = "/api/media?display_name=" + quote("../../中文课程.mp4")
        headers = {"X-Upload-ID": request_id, "Content-Type": "application/octet-stream"}
        status, result = self.http("POST", path, iter([b"abc", b"def"]), headers, chunked=True)
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "received")
        self.assertEqual(result["request_id"], request_id)
        source = self.data / "media" / result["media_id"] / "source"
        self.assertEqual(source.read_bytes(), b"abcdef")
        self.assertEqual(self.http("GET", f"/api/uploads/{request_id}"), (200, result))
        self.assertEqual(self.http("POST", path, b"different", headers), (200, result))
        self.stop()
        self.start()
        self.assertEqual(self.http("GET", f"/api/uploads/{request_id}"), (200, result))
        schema = self.http("GET", "/openapi.json")[1]
        post = schema["paths"]["/api/media"]["post"]
        self.assertIn("application/octet-stream", post["requestBody"]["content"])
        self.assertIn("201", post["responses"])
        self.assertEqual(len(list((self.data / "media").iterdir())), 1)

    def test_errors_origin_limit_and_disconnected_socket(self):
        path = "/api/media?display_name=test.wav"
        headers = {"X-Upload-ID": str(uuid4()), "Content-Type": "application/octet-stream"}
        cases = [
            (path, b"bytes", {**headers, "Origin": "https://other.example"}, 403, "origin_rejected"),
            (path, b"", {**headers, "Content-Length": str(4 * 1024**3 + 1)}, 413, "upload_too_large"),
            (path, b"bytes", {**headers, "Content-Type": "multipart/form-data"}, 415, "unsupported_content_type"),
            (path, b"bytes", {**headers, "X-Upload-ID": "invalid"}, 422, "invalid_request"),
            ("/api/media?display_name=", b"bytes", headers, 422, "invalid_request"),
            (path, b"", headers, 400, "empty_upload"),
        ]
        for url, body, values, status, code in cases:
            with self.subTest(code=code):
                actual, error = self.http("POST", url, body, values)
                self.assertEqual(actual, status)
                self.assertEqual(error["code"], code)
                self.assertEqual(set(error), {"code", "message", "retryable"})
        self.assertEqual(self.http("GET", f"/api/uploads/{uuid4()}")[0], 404)
        # Keep an actual upload incomplete: health stays responsive; another upload is busy.
        request_id = str(uuid4())
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                          f"X-Upload-ID: {request_id}\r\nContent-Type: application/octet-stream\r\n"
                          "Content-Length: 100000\r\n\r\npartial").encode())
            deadline = time.monotonic() + 5
            while not list((self.data / "tmp").glob("*.part")):
                if time.monotonic() > deadline:
                    self.fail("Upload was not streamed to disk")
                time.sleep(.01)
            self.assertEqual(self.http("GET", "/api/health")[0], 200)
            self.assertEqual(self.http("GET", f"/api/uploads/{request_id}")[0], 404)
            self.assertEqual(self.http("POST", path, b"bytes", headers)[1]["code"], "upload_busy")
        finally:
            sock.close()
        deadline = time.monotonic() + 5
        while list((self.data / "tmp").glob("*.part")) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(list((self.data / "tmp").iterdir()), [])
        self.assertEqual(list((self.data / "media").iterdir()), [])
        self.assertEqual(self.http("GET", f"/api/uploads/{request_id}")[0], 404)

    def test_killed_api_upload_is_cleaned_on_restart(self):
        request_id = str(uuid4())
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall((f"POST /api/media?display_name=test HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{self.port}\r\nX-Upload-ID: {request_id}\r\n"
                          "Content-Type: application/octet-stream\r\n"
                          "Content-Length: 100000\r\n\r\npartial").encode())
            deadline = time.monotonic() + 5
            while not list((self.data / "tmp").glob("*.part")):
                if time.monotonic() > deadline:
                    self.fail("Upload was not started")
                time.sleep(.01)
            self.process.kill()
            self.process.wait(timeout=10)
        finally:
            sock.close()
        self.assertEqual(len(list((self.data / "tmp").glob("*.part"))), 1)
        self.start()
        self.assertEqual(list((self.data / "tmp").iterdir()), [])
        self.assertEqual(self.http("GET", f"/api/uploads/{request_id}")[0], 404)
