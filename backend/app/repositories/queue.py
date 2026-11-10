"""Short queue transactions; no filesystem IO or model work inside a transaction."""
from pathlib import Path
import time
from uuid import uuid4

from app.repositories.database import connect


class QueueConflict(Exception):
    """The media lifecycle or current attempt does not allow this operation."""


def now_ms():
    return time.time_ns() // 1_000_000


def _enqueue(db, media_id, attempt):
    job_id = str(uuid4())
    db.execute(
        """INSERT INTO jobs(id, media_id, status, attempt, created_at)
           VALUES (?, ?, 'queued', ?, ?)""",
        (job_id, media_id, attempt, now_ms()),
    )
    return job_id


def received(db_path: Path, *, request_id: str, media_id: str,
             display_name: str, source_path: str, size_bytes: int):
    """Register a fully received file and its first job, or return the original receipt.

    Internal storage boundary: caller must persist the file first, use server-owned
    IDs/paths, and clean up its own unused file on replay or transaction failure.
    This records receipt only, not media validation or recognition success.
    """
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        receipt = db.execute(
            """SELECT media.id AS media_id, jobs.id AS job_id FROM media JOIN jobs
               ON jobs.media_id = media.id AND jobs.attempt = 1 WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        if receipt:
            return dict(receipt)
        db.execute(
            """INSERT INTO media(id, request_id, display_name, source_path, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (media_id, request_id, display_name, source_path, size_bytes, now_ms()),
        )
        return {"media_id": media_id, "job_id": _enqueue(db, media_id, 1)}


def retry(db_path: Path, media_id: str):
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        media = db.execute("SELECT lifecycle FROM media WHERE id = ?", (media_id,)).fetchone()
        job = db.execute(
            "SELECT * FROM jobs WHERE media_id = ? ORDER BY attempt DESC LIMIT 1", (media_id,),
        ).fetchone()
        if not media or media["lifecycle"] != "active" or not job or job["status"] not in (
            "failed", "interrupted",
        ):
            raise QueueConflict("Only failed/interrupted active media can be retried")
        return _enqueue(db, media_id, job["attempt"] + 1)


def claim_next(db_path: Path, worker_id: str):
    """Caller holds worker.lock for the entire processing lifetime.

    A global database constraint also prevents parallel running jobs. No claim is
    made by the skeleton worker until a real processor is connected.
    """
    if not worker_id:
        raise ValueError("worker_id is required")
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM jobs WHERE status = 'running'").fetchone():
            return None
        job = db.execute(
            """SELECT jobs.id FROM jobs JOIN media ON media.id = jobs.media_id
               WHERE jobs.status = 'queued' AND media.lifecycle = 'active'
                 AND jobs.cancel_requested = 0 ORDER BY queue_order LIMIT 1""",
        ).fetchone()
        if not job:
            return None
        timestamp = now_ms()
        db.execute(
            """UPDATE jobs SET status = 'running', stage = 'preprocessing',
               worker_id = ?, started_at = ?, heartbeat = ? WHERE id = ?""",
            (worker_id, timestamp, timestamp, job["id"]),
        )
        return dict(db.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())


def fail_owned(db_path: Path, job_id: str, worker_id: str, code: str, message: str):
    """Reject late writes by an old attempt/worker or a deleting media item."""
    with connect(db_path) as db, db:
        updated = db.execute(
            """UPDATE jobs SET status = 'failed', finished_at = ?, error_code = ?,
               error_message = ? WHERE id = ? AND status = 'running' AND worker_id = ?
               AND cancel_requested = 0 AND EXISTS
               (SELECT 1 FROM media WHERE media.id = jobs.media_id AND lifecycle = 'active')""",
            (now_ms(), code, message, job_id, worker_id),
        )
        if updated.rowcount != 1:
            raise QueueConflict("Job ownership or media lifecycle changed")
