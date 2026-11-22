"""Short queue transactions; no filesystem IO or model work inside a transaction."""
import json
from pathlib import Path
import time
from uuid import uuid4

from app.repositories.database import connect

MODEL_DISABLED_WAIT = "模型已停用，等待恢复或更换模型"


class QueueConflict(Exception):
    """The media lifecycle or current attempt does not allow this operation."""


class UploadDeleted(QueueConflict):
    pass


class MediaNotFound(QueueConflict):
    pass


class ModelDisabled(QueueConflict):
    pass


def ensure_enabled(db, model):
    if model and db.execute("SELECT 1 FROM model_controls WHERE name=? AND disabled=1",
                            (model,)).fetchone():
        raise ModelDisabled("Model disabled by server")


def set_model_disabled(db, name, disabled):
    db.execute("BEGIN IMMEDIATE")
    db.execute("""INSERT INTO model_controls(name, disabled) VALUES (?, ?)
                  ON CONFLICT(name) DO UPDATE SET disabled=excluded.disabled""", (name, disabled))
    worker = db.execute("SELECT models_json FROM worker_status WHERE singleton=1").fetchone()
    default = next((entry["name"] for entry in json.loads(worker["models_json"])
                    if entry.get("is_default")), None) if worker else None
    if disabled:
        db.execute("""UPDATE jobs SET wait_reason=? WHERE status='queued'
                      AND coalesce(requested_model, ?)=?""", (MODEL_DISABLED_WAIT, default, name))
    else:
        db.execute("""UPDATE jobs SET wait_reason=NULL WHERE status='queued'
                      AND coalesce(requested_model, ?)=? AND wait_reason=?""",
                   (default, name, MODEL_DISABLED_WAIT))


def now_ms():
    return time.time_ns() // 1_000_000


def find_receipt(db_path: Path, request_id: str):
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM deleted_uploads WHERE request_id=?", (request_id,)).fetchone():
            raise UploadDeleted("Upload was deleted")
        row = db.execute(
            """SELECT media.id AS media_id, jobs.id AS job_id FROM media JOIN jobs
               ON jobs.media_id = media.id AND jobs.attempt = 1 WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None


def _enqueue(db, media_id, attempt, requested_model=None):
    job_id = str(uuid4())
    db.execute(
        """INSERT INTO jobs(id, media_id, status, attempt, created_at, requested_model)
           VALUES (?, ?, 'queued', ?, ?, ?)""",
        (job_id, media_id, attempt, now_ms(), requested_model),
    )
    return job_id


def received(db_path: Path, *, request_id: str, media_id: str,
             display_name: str, source_path: str, size_bytes: int, requested_model=None):
    """Register a fully received file and its first job, or return the original receipt.

    Internal storage boundary: caller must persist the file first, use server-owned
    IDs/paths, and clean up its own unused file on replay or transaction failure.
    This records receipt only, not media validation or recognition success.
    """
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM deleted_uploads WHERE request_id=?", (request_id,)).fetchone():
            raise UploadDeleted("Upload was deleted")
        receipt = db.execute(
            """SELECT media.id AS media_id, jobs.id AS job_id FROM media JOIN jobs
               ON jobs.media_id = media.id AND jobs.attempt = 1 WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        if receipt:
            return dict(receipt)
        ensure_enabled(db, requested_model)
        db.execute(
            """INSERT INTO media(id, request_id, display_name, source_path, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (media_id, request_id, display_name, source_path, size_bytes, now_ms()),
        )
        return {"media_id": media_id, "job_id": _enqueue(db, media_id, 1, requested_model)}


def retry(db_path: Path, media_id: str, model=None, *, default_model=None):
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        media = db.execute("SELECT lifecycle FROM media WHERE id = ?", (media_id,)).fetchone()
        if not media:
            raise MediaNotFound("Media does not exist")
        job = db.execute(
            "SELECT * FROM jobs WHERE media_id = ? ORDER BY attempt DESC LIMIT 1", (media_id,),
        ).fetchone()
        if media["lifecycle"] != "active" or not job or job["status"] not in (
            "failed", "interrupted",
        ):
            raise QueueConflict("Only failed/interrupted active media can be retried")
        selected = model or job["requested_model"] or default_model
        ensure_enabled(db, selected)
        return _enqueue(db, media_id, job["attempt"] + 1, selected)


def change_queued_model(db_path: Path, media_id: str, model: str):
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        media = db.execute("SELECT lifecycle FROM media WHERE id=?", (media_id,)).fetchone()
        if not media:
            raise MediaNotFound("Media does not exist")
        ensure_enabled(db, model)
        changed = db.execute(
            """UPDATE jobs SET requested_model=?, wait_reason=NULL
               WHERE id=(SELECT id FROM jobs WHERE media_id=? ORDER BY attempt DESC LIMIT 1)
               AND status='queued' AND cancel_requested=0""", (model, media_id),
        ).rowcount if media["lifecycle"] == "active" else 0
        if not changed:
            raise QueueConflict("Only a queued attempt can change model")


def claim_next(db_path: Path, worker_id: str, *, configuration=None, model_revisions=None,
               default_model=None, blocked_models=None):
    """Caller holds worker.lock for the entire processing lifetime.

    A global database constraint also prevents parallel running jobs. Runtime
    callers persist the validated deployment configuration with the claim.
    """
    if not worker_id:
        raise ValueError("worker_id is required")
    with connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM jobs WHERE status = 'running'").fetchone():
            return None
        jobs = db.execute(
            """SELECT jobs.id, requested_model, wait_reason FROM jobs
               JOIN media ON media.id = jobs.media_id
               WHERE jobs.status = 'queued' AND media.lifecycle = 'active'
                 AND jobs.cancel_requested = 0 ORDER BY queue_order""",
        )
        job = None
        if model_revisions is None:
            job = jobs.fetchone()
        else:
            controls = {row["name"]: row for row in db.execute("SELECT * FROM model_controls")}
            for candidate in jobs:
                name = candidate["requested_model"] or default_model
                control = controls.get(name)
                revision = control["revision"] if control else 0
                message = blocked_models.get(name)
                if control and control["disabled"]:
                    message = MODEL_DISABLED_WAIT
                elif message is None and model_revisions.get(name) != revision:
                    message = "等待验证或恢复所选模型"
                if candidate["wait_reason"] != message:
                    db.execute("UPDATE jobs SET wait_reason=? WHERE id=?", (message, candidate["id"]))
                if message is None and job is None:
                    job = candidate
        if job is None:
            return None
        timestamp = now_ms()
        db.execute(
            """UPDATE jobs SET status = 'running', stage = 'preprocessing',
               worker_id = ?, started_at = ?, heartbeat = ?, wait_reason=NULL WHERE id = ?""",
            (worker_id, timestamp, timestamp, job["id"]),
        )
        if configuration is not None:
            db.execute("""UPDATE jobs SET engine=?, model=?, backend=?, processing_version=?,
                          parameters_json=? WHERE id=?""",
                       (configuration["engine"], configuration["model_name"], configuration["backend"],
                        configuration["processing_version"], json.dumps(configuration), job["id"]))
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
