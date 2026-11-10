"""Snapshot reads keep media and its latest attempt consistent during retries."""
from app.repositories.database import connect


def _view(db, media):
    result = dict(media)
    job = db.execute(
        "SELECT * FROM jobs WHERE media_id = ? ORDER BY attempt DESC LIMIT 1", (media["id"],),
    ).fetchone()
    result["latest_job"] = dict(job) if job else None
    return result


def get_media(database, media_id):
    with connect(database) as db:
        db.execute("BEGIN")
        media = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return _view(db, media) if media else None


def list_media(database, limit, before=None):
    with connect(database) as db:
        db.execute("BEGIN")
        where = "WHERE (created_at, id) < (?, ?)" if before else ""
        parameters = (*before, limit + 1) if before else (limit + 1,)
        rows = db.execute(
            f"SELECT * FROM media {where} ORDER BY created_at DESC, id DESC LIMIT ?", parameters,
        ).fetchall()
        return [_view(db, media) for media in rows[:limit]], len(rows) > limit
