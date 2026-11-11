"""One owned attempt: private artifacts first, one atomic publication last."""
import json
import threading

from app.media.prepare import PreparationError, prepare
from app.processes import ProcessCancelled
from app.repositories.database import connect
from app.repositories.queue import QueueConflict, fail_owned, now_ms
from app.repositories.transcripts import publish
from app.transcription.whisper import TranscriptionError, transcribe


def owned(database, job, owner):
    with connect(database) as db:
        return db.execute("""SELECT media.source_path FROM jobs JOIN media ON media.id=jobs.media_id
            WHERE jobs.id=? AND worker_id=? AND status='running' AND cancel_requested=0
              AND lifecycle='active' AND attempt=(SELECT MAX(attempt) FROM jobs j
                  WHERE j.media_id=jobs.media_id)""", (job["id"], owner)).fetchone()


def process_attempt(settings, config, job, stop):
    database, owner = settings.database, job["worker_id"]
    cancel, finished = threading.Event(), threading.Event()
    monitor_errors = []

    def monitor():
        try:
            while not finished.wait(.1):
                if stop.is_set() or not owned(database, job, owner):
                    cancel.set()
                    return
                with connect(database) as db, db:
                    db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND worker_id=? AND status='running'",
                               (now_ms(), job["id"], owner))
        except Exception as exc:
            monitor_errors.append(exc)
            cancel.set()

    def stage(value, snapshot=None):
        if stop.is_set() or cancel.is_set() or not owned(database, job, owner):
            raise ProcessCancelled("Attempt stopped or ownership changed")
        with connect(database) as db, db:
            db.execute("UPDATE jobs SET stage=? WHERE id=? AND worker_id=? AND status='running'",
                       (value, job["id"], owner))
            if snapshot:
                db.execute("UPDATE jobs SET parameters_json=? WHERE id=? AND worker_id=?",
                           (json.dumps(snapshot), job["id"], owner))

    row = owned(database, job, owner)
    thread = threading.Thread(target=monitor)
    thread.start()
    try:
        if not row:
            raise ProcessCancelled("Attempt no longer owned")
        source = (settings.data_dir / row["source_path"]).resolve()
        source.relative_to(settings.data_dir / "media")
        work = settings.data_dir / "media" / job["media_id"] / "attempts" / job["id"]
        stage("preprocessing")
        prepared = prepare(source, work / "prepared", ffmpeg=config.ffmpeg,
                           ffprobe=config.ffprobe, cancel=cancel)
        snapshot = {**config.snapshot, "source_origin_seconds": prepared.source_origin_seconds}
        stage("transcribing", snapshot)
        segments = transcribe(prepared.audio, config.model, config.executable, work / "recognition",
                              prepared.duration_ms, cancel=cancel)
        stage("organizing")
        publish(database, job["id"], owner, duration_ms=prepared.duration_ms,
                media_type=prepared.media_type, segments=segments, engine="whisper.cpp",
                model=config.snapshot["model_name"], processing_version=config.snapshot["processing_version"],
                playback_path=prepared.playback.relative_to(settings.data_dir).as_posix())
    except (ProcessCancelled, QueueConflict):
        # Tools have already drained before these exceptions propagate. Preserve
        # attempt history, never publish, and allow an explicit retry on shutdown.
        with connect(database) as db, db:
            db.execute("""UPDATE jobs SET status=?, finished_at=?, error_code=?, error_message=?
                WHERE id=? AND worker_id=? AND status='running'""",
                       ("interrupted" if stop.is_set() else "cancelled", now_ms(),
                        "worker_stopped" if stop.is_set() else "attempt_cancelled",
                        "处理已停止，请重试" if stop.is_set() else "处理已取消",
                        job["id"], owner))
    except (PreparationError, TranscriptionError, OSError, ValueError):
        try:
            fail_owned(database, job["id"], owner, "processing_failed", "媒体处理失败，请查看私有任务日志")
        except QueueConflict:
            if owned(database, job, owner):
                raise
            with connect(database) as db, db:
                db.execute("""UPDATE jobs SET status='cancelled', finished_at=?
                    WHERE id=? AND worker_id=? AND status='running'""", (now_ms(), job["id"], owner))
    finally:
        finished.set()
        thread.join()
    if monitor_errors:
        raise RuntimeError("Attempt monitor failed; processing stopped") from monitor_errors[0]
