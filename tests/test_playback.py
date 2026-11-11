"""Actual HTTP Range contract; fixture bytes do not establish browser decoding."""
import http.client
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.repositories.database import connect
from app.repositories.queue import claim_next, received
from app.repositories.transcripts import publish
from http_support import HTTPAPICase


class PlaybackHTTPTests(HTTPAPICase):
    def test_range_and_publication_lifecycle(self):
        database = self.data / "recue.sqlite3"
        media_id = str(uuid4())
        receipt = received(database, request_id=str(uuid4()), media_id=media_id, display_name="test",
                           source_path=f"media/{media_id}/source", size_bytes=10)
        url = f"/api/media/{media_id}/playback"
        self.assertEqual(self.http("GET", url)[0], 409)
        self.assertEqual(self.http("GET", f"/api/media/{uuid4()}/playback")[0], 404)
        claim_next(database, "owner")
        file = self.data / "media" / media_id / "playback.mp4"
        file.parent.mkdir(parents=True)
        payload = bytes(range(256)) * 4000
        file.write_bytes(payload)
        publish(database, receipt["job_id"], "owner", duration_ms=1000, media_type="video", segments=[],
                engine="fixture", model="fixture", processing_version="test",
                playback_path=file.relative_to(self.data).as_posix())

        def read(headers):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                connection.request("GET", url, headers=headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        status, headers, body = read({})
        self.assertEqual((status, body), (200, payload))
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["content-type"], "video/mp4")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("HEAD", url)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.getheader("content-length")), len(payload))
            self.assertEqual(response.read(), b"")
        finally:
            connection.close()
        for value, start, end in [("bytes=900000-900099", 900000, 900099),
                                  ("bytes=-100", len(payload)-100, len(payload)-1),
                                  ("bytes=1000000-", 1000000, len(payload)-1)]:
            status, headers, body = read({"Range": value})
            self.assertEqual(status, 206)
            self.assertEqual(body, payload[start:end+1])
            self.assertEqual(headers["content-range"], f"bytes {start}-{end}/{len(payload)}")
        status, headers, body = read({"Range": f"bytes={len(payload)}-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"], f"bytes */{len(payload)}")
        with connect(database) as db, db:
            db.execute("UPDATE media SET lifecycle='deleting'")
        self.assertEqual(self.http("GET", url)[0], 409)
        with connect(database) as db, db:
            db.execute("UPDATE media SET lifecycle='active'")
        file.unlink()
        self.assertEqual(self.http("GET", url)[0], 503)
