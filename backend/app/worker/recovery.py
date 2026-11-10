from app.repositories.database import connect
from app.repositories.queue import now_ms


def interrupt_previous_jobs(db_path):
    """Only call after acquiring worker.lock, proving the old worker has exited.

    API startup and heartbeat expiry must never call this recovery operation.
    Queued jobs and completed attempts remain untouched.
    """
    with connect(db_path) as db, db:
        return db.execute(
            """UPDATE jobs SET status = 'interrupted', finished_at = ?,
               error_code = 'worker_interrupted', error_message = '工作进程已退出，请手动重试'
               WHERE status = 'running'""",
            (now_ms(),),
        ).rowcount
