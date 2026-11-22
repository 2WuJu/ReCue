import logging
import json
import os
import signal
import threading
import time
from uuid import uuid4

from app.config import Settings
from app.locking import instance_lock
from app.processes import ProcessCancelled
from app.repositories.database import connect, migrate
from app.worker.recovery import interrupt_previous_jobs
from app.worker.containment import await_cleanup, containment
from app.worker.configuration import load_processing, verify_models
from app.worker.processor import process_attempt
from app.repositories.queue import claim_next


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    settings.prepare()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with instance_lock(settings.data_dir / "runtime" / "worker.lock"):
        migrate(settings.database)
        await_cleanup(settings.data_dir / "runtime")
        recovered = interrupt_previous_jobs(settings.database)
        worker_id = str(uuid4())

        model_catalog = "[]"

        def heartbeat(state):
            with connect(settings.database) as db, db:
                db.execute(
                    """INSERT OR REPLACE INTO worker_status
                       (singleton, worker_id, pid, heartbeat, state, models_json)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (worker_id, os.getpid(), time.time(), state, model_catalog if state == "processing" else "[]"),
                )

        def blocked(exc):
            logging.error("Processing configuration unavailable: %s", exc)
            while not stop.is_set():
                heartbeat("blocked_config")
                stop.wait(2)
            heartbeat("stopped")

        logging.info("Worker started; interrupted %s old jobs", recovered)
        try:
            heartbeat("idle")
            try:
                config = load_processing(settings)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                return blocked(exc)
            with containment(settings.data_dir / "runtime") as guardian:
                try:
                    config = verify_models(settings, config, stop)
                except ValueError as exc:
                    return blocked(exc)
                model_catalog = json.dumps(list(config.models.values()))

                def pulse():
                    while not stop.wait(2):
                        if guardian is not None and guardian.poll() is not None:
                            stop.set()
                            return
                        heartbeat("processing")
                pulse_thread = threading.Thread(target=pulse)
                pulse_thread.start()
                try:
                    while not stop.is_set():
                        if guardian is not None and guardian.poll() is not None:
                            raise RuntimeError("Cleanup guardian exited unexpectedly")
                        heartbeat("processing")
                        job = claim_next(settings.database, worker_id, configuration=config.snapshot)
                        if job:
                            process_attempt(settings, config, job, stop)
                        else:
                            stop.wait(.5)
                finally:
                    stop.set()
                    pulse_thread.join()
        except ProcessCancelled:
            heartbeat("stopped")
        except BaseException:
            heartbeat("error")
            raise
        else:
            heartbeat("stopped")


if __name__ == "__main__":
    main()
