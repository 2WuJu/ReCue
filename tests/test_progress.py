import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from http_support import HTTPAPICase
from app.repositories.database import connect
from app.repositories.queue import received, claim_next
from app.repositories.transcripts import publish


class ProgressHTTPTests(HTTPAPICase):
    def setUp(self):
        super().setUp()
        self.media_id = str(uuid4())
        self.path = f'/api/media/{self.media_id}/progress'
        self.database = self.data / 'recue.sqlite3'

    def prepare(self):
        received(self.database, request_id=str(uuid4()), media_id=self.media_id,
                 display_name='progress.wav', source_path=f'media/{self.media_id}/source', size_bytes=100)

    def publish(self):
        job = claim_next(self.database, 'owner')
        publish(self.database, job['id'], 'owner', duration_ms=1000, media_type='audio',
                segments=[], engine='test', model='test', processing_version='test')

    def put(self, position, completed=False):
        return self.http('PUT', self.path, json.dumps(dict(position_ms=position, completed=completed)),
                         {'Content-Type': 'application/json'})

    def test_lifecycle_validation_and_restart(self):
        self.assertEqual(self.http('GET', self.path)[0], 404)
        self.assertEqual(self.put(0)[0], 404)
        self.prepare()
        self.assertEqual(self.put(0)[0], 409)
        self.publish()
        initial = self.http('GET', self.path)[1]
        self.assertEqual(initial['position_ms'], 0)
        self.assertIsNone(initial['updated_at'])
        for value in [-1, 1001, 1.2, True, '20', None]:
            with self.subTest(value=value):
                self.assertEqual(self.put(value)[0], 422)
        self.assertEqual(self.put(100, 'false')[0], 422)
        self.assertEqual(self.put(100, True)[0], 422)
        self.assertEqual(self.http('GET', self.path)[1], initial)
        status, saved = self.put(1000, True)
        self.assertEqual(status, 200)
        self.assertTrue(saved['completed'])
        self.assertIsInstance(saved['updated_at'], int)
        self.stop()
        self.start()
        self.assertEqual(self.http('GET', self.path)[1], saved)
        self.assertEqual(self.put(0)[1]['completed'], False)
        for lifecycle in ['deleting', 'delete_failed']:
            with connect(self.database) as db, db:
                db.execute('UPDATE media SET lifecycle=?', (lifecycle,))
            self.assertEqual(self.put(10)[0], 409)
            self.assertEqual(self.http('GET', self.path)[0], 409)
        with connect(self.database) as db, db:
            db.execute('DELETE FROM media')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM playback_progress').fetchone()[0], 0)

    def test_migration_keeps_published_media(self):
        self.prepare()
        self.publish()
        with connect(self.database) as db, db:
            db.execute('ALTER TABLE worker_status DROP COLUMN models_json')
            for column in ('requested_model', 'cpu_percent', 'peak_memory_mib', 'progress_percent'):
                db.execute(f'ALTER TABLE jobs DROP COLUMN {column}')
            db.execute('DROP TABLE deleted_uploads')
            db.execute('ALTER TABLE media DROP COLUMN delete_error')
            db.execute('DROP TABLE playback_progress')
            db.execute('PRAGMA user_version=4')
        self.stop()
        self.start()
        self.assertEqual(self.http('GET', self.path)[0], 200)
        self.assertEqual(self.http('GET', f'/api/media/{self.media_id}/transcript')[0], 200)
