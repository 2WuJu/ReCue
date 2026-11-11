import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = [
    (1, ["""CREATE TABLE worker_status (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        worker_id TEXT NOT NULL,
        pid INTEGER NOT NULL,
        heartbeat REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('idle', 'stopped'))
    )"""]),
    (2, [
        """CREATE TABLE media (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            source_path TEXT NOT NULL UNIQUE,
            playback_path TEXT,
            media_type TEXT CHECK(media_type IN ('audio', 'video')),
            duration_ms INTEGER CHECK(duration_ms > 0),
            size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
            created_at INTEGER NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'active'
                CHECK(lifecycle IN ('active', 'deleting', 'delete_failed'))
        )""",
        """CREATE TABLE jobs (
            queue_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN
                ('queued', 'running', 'succeeded', 'failed', 'interrupted', 'cancelled')),
            stage TEXT CHECK(stage IN ('preprocessing', 'transcribing', 'organizing')),
            attempt INTEGER NOT NULL CHECK(attempt > 0),
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            worker_id TEXT,
            heartbeat INTEGER,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
            error_code TEXT,
            error_message TEXT,
            engine TEXT,
            model TEXT,
            backend TEXT,
            processing_version TEXT,
            parameters_json TEXT,
            UNIQUE(media_id, attempt),
            CHECK(status != 'running' OR (worker_id IS NOT NULL AND started_at IS NOT NULL))
        )""",
        """CREATE UNIQUE INDEX one_active_job_per_media ON jobs(media_id)
            WHERE status IN ('queued', 'running')""",
        "CREATE UNIQUE INDEX one_running_job ON jobs((1)) WHERE status = 'running'",
        "CREATE INDEX job_fifo ON jobs(queue_order) WHERE status = 'queued'",
        "CREATE INDEX media_listing ON media(created_at, id)",
    ]),
    (3, [
        """CREATE TABLE transcripts (
            media_id TEXT PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
            version INTEGER NOT NULL CHECK(version > 0),
            published_at INTEGER NOT NULL
        )""",
        """CREATE TABLE transcript_segments (
            id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
            end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
            text TEXT NOT NULL CHECK(length(trim(text)) > 0),
            UNIQUE(media_id, sequence)
        )""",
    ]),
]


@contextmanager
def connect(path: Path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    try:
        yield db
    finally:
        db.close()


def migrate(path: Path):
    with connect(path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        # Each migration and its version advance share a transaction; concurrent
        # API/worker startup rechecks the version after acquiring the write lock.
        for version, statements in MIGRATIONS:
            try:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("PRAGMA user_version").fetchone()[0]
                if current > MIGRATIONS[-1][0]:
                    raise RuntimeError("Database is newer than this application")
                if current < version:
                    for statement in statements:
                        db.execute(statement)
                    db.execute(f"PRAGMA user_version = {version}")
                db.commit()
            except BaseException:
                db.rollback()
                raise
