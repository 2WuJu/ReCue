"""Durable delete intent; files are removed only after all owned tools drain."""
import shutil
from uuid import UUID

from app.locking import AlreadyRunning, instance_lock
from app.repositories.database import connect
from app.repositories.queue import now_ms


def deletion_lock(settings):
    return instance_lock(settings.data_dir / "runtime" / "deletion.lock")


def request_delete(settings, media_id):
    # Serialize retry with cleanup, so a concurrent failed pass cannot lose it.
    with deletion_lock(settings), connect(settings.database) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT request_id FROM media WHERE id=?", (media_id,)).fetchone()
        if not row:
            return  # Repeated DELETE after completion is also accepted.
        db.execute("INSERT OR IGNORE INTO deleted_uploads VALUES (?, ?)", (row[0], media_id))
        db.execute("UPDATE media SET lifecycle='deleting', delete_error=NULL WHERE id=?", (media_id,))
        db.execute("UPDATE jobs SET cancel_requested=1 WHERE media_id=?", (media_id,))
        db.execute("""UPDATE jobs SET status='cancelled', finished_at=?
                      WHERE media_id=? AND status='queued'""", (now_ms(), media_id))


def cleanup_deletions(settings):
    try:
        with deletion_lock(settings):
            with connect(settings.database) as db:
                rows = db.execute("""SELECT id FROM media WHERE lifecycle='deleting'
                    AND NOT EXISTS (SELECT 1 FROM jobs WHERE media_id=media.id AND status='running')""").fetchall()
            for row in rows:
                media_id = row[0]
                try:
                    # Never use a stored/client path for recursive removal.
                    if str(UUID(media_id)) != media_id:
                        raise ValueError("Invalid storage ID")
                    root = settings.data_dir / "media"
                    directory = root / media_id
                    if (root.is_symlink() or root.is_junction() or directory.is_symlink()
                            or directory.is_junction() or directory.resolve().parent != root.resolve()):
                        raise ValueError("Unsafe media directory")
                    if directory.exists():
                        shutil.rmtree(directory)
                except (OSError, ValueError):
                    with connect(settings.database) as db, db:
                        db.execute("""UPDATE media SET lifecycle='delete_failed', delete_error=?
                                      WHERE id=? AND lifecycle='deleting'""",
                                   ("文件清理未完成，可能被占用或无权限。关闭相关程序并检查存储后重试删除。", media_id))
                    continue
                with connect(settings.database) as db, db:
                    db.execute("DELETE FROM media WHERE id=? AND lifecycle='deleting'", (media_id,))
    except AlreadyRunning:
        pass  # Another API process is cleaning; its durable intent remains.
