"""Publish all segments and job success in the same short transaction."""
from uuid import uuid4

from app.repositories.database import connect
from app.repositories.queue import QueueConflict, now_ms
from app.transcription.whisper import validate_segments


def publish(database, job_id, worker_id, *, duration_ms, media_type, segments,
            engine, model, processing_version):
    validate_segments(segments, duration_ms)
    if media_type not in ("audio", "video"):
        raise ValueError("Invalid media type")
    with connect(database) as db, db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute(
            """SELECT jobs.* FROM jobs JOIN media ON media.id=jobs.media_id
               WHERE jobs.id=? AND jobs.worker_id=? AND jobs.status='running'
                 AND jobs.cancel_requested=0 AND media.lifecycle='active'
                 AND jobs.attempt=(SELECT MAX(attempt) FROM jobs j WHERE j.media_id=jobs.media_id)""",
            (job_id, worker_id),
        ).fetchone()
        if not job:
            raise QueueConflict("Job ownership or media lifecycle changed")
        media_id = job["media_id"]
        db.execute("DELETE FROM transcript_segments WHERE media_id=?", (media_id,))
        db.execute(
            """INSERT INTO transcripts(media_id, job_id, version, published_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET job_id=excluded.job_id,
                 version=excluded.version, published_at=excluded.published_at""",
            (media_id, job_id, job["attempt"], now_ms()),
        )
        db.executemany(
            """INSERT INTO transcript_segments(id, media_id, sequence, start_ms, end_ms, text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(str(uuid4()), media_id, index, item["start_ms"], item["end_ms"], item["text"])
             for index, item in enumerate(segments)],
        )
        db.execute("UPDATE media SET duration_ms=?, media_type=? WHERE id=?",
                   (duration_ms, media_type, media_id))
        db.execute(
            """UPDATE jobs SET status='succeeded', stage='organizing', finished_at=?,
               engine=?, model=?, backend='cpu', processing_version=? WHERE id=?""",
            (now_ms(), engine, model, processing_version, job_id),
        )


def get_transcript(database, media_id):
    with connect(database) as db:
        db.execute("BEGIN")
        media = db.execute("SELECT lifecycle FROM media WHERE id=?", (media_id,)).fetchone()
        if not media:
            return None
        row = db.execute(
            """SELECT transcripts.* FROM transcripts JOIN jobs ON jobs.id=transcripts.job_id
               WHERE transcripts.media_id=? AND jobs.status='succeeded'
                 AND jobs.attempt=(SELECT MAX(attempt) FROM jobs j WHERE j.media_id=jobs.media_id)""",
            (media_id,),
        ).fetchone()
        if media["lifecycle"] != "active" or not row:
            return {"ready": False}
        segments = [dict(item) for item in db.execute(
            """SELECT id, sequence, start_ms, end_ms, text FROM transcript_segments
               WHERE media_id=? ORDER BY sequence""", (media_id,),
        )]
        return {"media_id": media_id, "version": row["version"], "segments": segments}
