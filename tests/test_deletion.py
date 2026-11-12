"""Deletion races use real SQLite/HTTP/files; fixtures do not claim ASR quality."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import os
import unittest
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from http_support import HTTPAPICase
from app.config import Settings
from app.repositories.database import connect
from app.repositories.queue import QueueConflict, claim_next, retry
from app.repositories.transcripts import publish
from app.services.deletion import request_delete, cleanup_deletions


class DeletionTests(HTTPAPICase):
    def settings(self):
        return Settings(self.data, ROOT / 'frontend/dist', None, 'ffmpeg', '127.0.0.1', self.port)

    def upload(self):
        rid = str(uuid4())
        code, receipt = self.http('POST', '/api/media?display_name=delete.wav', b'fixture',
            {'X-Upload-ID': rid, 'Content-Type': 'application/octet-stream'})
        self.assertEqual(code, 201)
        return rid, receipt

    def wait_deleted(self, mid):
        for _ in range(100):
            if self.http('GET', '/api/media/' + mid)[0] == 404:
                return
            time.sleep(.05)
        self.fail('Deletion did not finish')

    def publish(self, job):
        publish(self.settings().database, job['id'], 'owner', duration_ms=1000,
                media_type='audio', segments=[{'start_ms': 0, 'end_ms': 900, 'text': 'fixture'}],
                engine='fixture', model='fixture', processing_version='fixture')

    def test_http_repeated_delete_restart_and_old_upload_never_returns(self):
        rid, receipt = self.upload()
        mid = receipt['media_id']
        url = '/api/media/' + mid
        self.assertEqual(self.http('DELETE', url, headers={'Origin': 'https://other.example'})[0], 403)
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(lambda _: self.http('DELETE', url)[0], range(6)))
        self.assertTrue(all(code in (202, 503) for code in codes))
        self.wait_deleted(mid)
        self.assertFalse((self.data / 'media' / mid).exists())
        self.assertEqual(self.http('DELETE', url)[0], 202)
        self.stop(); self.start()
        self.assertEqual(self.http('GET', '/api/uploads/' + rid)[0], 410)
        self.assertEqual(self.http('POST', '/api/media?display_name=replay.wav', b'replay',
            {'X-Upload-ID': rid, 'Content-Type': 'application/octet-stream'})[0], 410)
        self.assertEqual(self.http('GET', '/api/media')[1]['items'], [])

    def test_running_blocks_cleanup_and_late_publication(self):
        _, receipt = self.upload()
        settings = self.settings()
        job = claim_next(settings.database, 'owner')
        request_delete(settings, receipt['media_id'])
        cleanup_deletions(settings)
        self.assertTrue((self.data / 'media' / receipt['media_id'] / 'source').exists())
        with self.assertRaises(QueueConflict): self.publish(job)
        with self.assertRaises(QueueConflict): retry(settings.database, receipt['media_id'])
        with connect(settings.database) as db, db:
            row = db.execute('SELECT * FROM jobs').fetchone()
            self.assertEqual(row['cancel_requested'], 1)
            db.execute("UPDATE jobs SET status='cancelled'")
        cleanup_deletions(settings)
        self.wait_deleted(receipt['media_id'])
        with self.assertRaises(QueueConflict): self.publish(job)

    def test_permission_failure_visible_retry_survives_restart(self):
        _, receipt = self.upload()
        self.stop()  # Deterministic cleaner fault injection without another API pass.
        settings = self.settings()
        request_delete(settings, receipt['media_id'])
        with patch('app.services.deletion.shutil.rmtree', side_effect=PermissionError('occupied')):
            cleanup_deletions(settings)
        self.start()
        url = '/api/media/' + receipt['media_id']
        detail = self.http('GET', url)[1]
        self.assertEqual(detail['lifecycle'], 'delete_failed')
        self.assertIn('占用', detail['delete_error'])
        self.assertEqual(self.http('POST', url + '/retry')[0], 409)
        self.assertEqual(self.http('DELETE', url)[0], 202)
        self.wait_deleted(receipt['media_id'])

    def test_publication_delete_race_cascades_all_artifacts(self):
        # Both transaction orders are valid; neither may leave a visible result.
        for _ in range(8):
            _, receipt = self.upload()
            settings = self.settings()
            job = claim_next(settings.database, 'owner')
            def publish_once():
                try: self.publish(job)
                except QueueConflict: pass
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(publish_once), pool.submit(request_delete, settings, receipt['media_id'])]
                for future in futures: future.result()
            with connect(settings.database) as db, db:
                db.execute("UPDATE jobs SET status='cancelled' WHERE status='running'")
            self.wait_deleted(receipt['media_id'])
        with connect(settings.database) as db:
            for table in ('media', 'jobs', 'transcripts', 'transcript_segments', 'playback_progress'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_real_open_file_failure_and_pending_cleanup_on_api_restart(self):
        _, receipt = self.upload()
        self.stop()
        settings = self.settings()
        mid = receipt['media_id']
        source = self.data / 'media' / mid / 'source'
        request_delete(settings, mid)
        with source.open('rb'):
            cleanup_deletions(settings)
            with connect(settings.database) as db:
                self.assertEqual(db.execute('SELECT lifecycle FROM media').fetchone()[0], 'delete_failed')
        request_delete(settings, mid)
        self.start()
        self.wait_deleted(mid)

    def test_completed_media_deletes_saved_progress_and_transcript(self):
        from app.repositories.progress import progress
        _, receipt = self.upload()
        settings = self.settings()
        job = claim_next(settings.database, 'owner')
        self.publish(job)
        progress(settings.database, receipt['media_id'], (700, False))
        with connect(settings.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM playback_progress').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM transcript_segments').fetchone()[0], 1)
        self.assertEqual(self.http('DELETE', '/api/media/' + receipt['media_id'])[0], 202)
        self.wait_deleted(receipt['media_id'])
        with connect(settings.database) as db:
            for table in ('jobs', 'transcripts', 'transcript_segments', 'playback_progress'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
