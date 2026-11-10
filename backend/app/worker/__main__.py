import logging
import os
import signal
import threading
import time
from uuid import uuid4

from app.config import Settings
from app.locking import instance_lock
from app.repositories.database import connect, migrate


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    settings.prepare()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with instance_lock(settings.data_dir / "runtime" / "worker.lock"):
        migrate(settings.database)
        worker_id = str(uuid4())

        def heartbeat(state):
            with connect(settings.database) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO worker_status VALUES (1, ?, ?, ?, ?)",
                    (worker_id, os.getpid(), time.time(), state),
                )

        logging.info("Worker started; queue processing is not implemented (T03/T04)")
        try:
            while not stop.is_set():
                heartbeat("idle")
                stop.wait(2)
        finally:
            heartbeat("stopped")


if __name__ == "__main__":
    main()
