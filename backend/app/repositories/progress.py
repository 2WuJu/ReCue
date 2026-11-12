"""Progress reads and lifecycle-checked writes share a database snapshot."""
from app.repositories.database import connect
from app.repositories.queue import MediaNotFound, QueueConflict, now_ms


def progress(database, media_id, value=None):
    with connect(database) as db, db:
        db.execute("BEGIN IMMEDIATE" if value is not None else "BEGIN")
        media = db.execute("""SELECT media.*, (SELECT status FROM jobs
            WHERE media_id=media.id ORDER BY attempt DESC LIMIT 1) AS status
            FROM media WHERE id=?""", (media_id,)).fetchone()
        if not media:
            raise MediaNotFound(media_id)
        if (media["lifecycle"] != "active" or media["status"] != "succeeded"
                or media["duration_ms"] is None):
            raise QueueConflict("Progress is unavailable")
        if value is not None:
            position, completed = value
            if not 0 <= position <= media["duration_ms"]:
                raise ValueError("Position exceeds duration")
            if completed and position != media["duration_ms"]:
                raise ValueError("Completed progress must be at the end")
            db.execute("""INSERT INTO playback_progress VALUES (?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET position_ms=excluded.position_ms,
                completed=excluded.completed, updated_at=excluded.updated_at""",
                (media_id, position, int(completed), now_ms()))
        row = db.execute("SELECT * FROM playback_progress WHERE media_id=?", (media_id,)).fetchone()
        if row:
            return {**dict(row), "completed": bool(row["completed"])}
        return dict(media_id=media_id, position_ms=0, completed=False, updated_at=None)
