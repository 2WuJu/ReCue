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
