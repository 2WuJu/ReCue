import logging
import json
from dataclasses import replace
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
from app.worker.configuration import (file_signature, load_processing, model_wait_reasons,
                                      refresh_models)
from app.worker.resources import available_memory
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
                    (worker_id, os.getpid(), time.time(), state, model_catalog),
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
                config = load_processing(settings, verify_default=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                return blocked(exc)
            with containment(settings.data_dir / "runtime") as guardian:
                entries = {}

                def publish_models(models):
                    nonlocal model_catalog
                    model_catalog = json.dumps(list(models.values()))

                def pulse():
                    while not stop.wait(2):
                        if guardian is not None and guardian.poll() is not None:
                            stop.set()
                            return
                        heartbeat("processing")
                pulse_thread = threading.Thread(target=pulse)
                pulse_thread.start()
                refresh_at = 0
                try:
                    while not stop.is_set():
                        if guardian is not None and guardian.poll() is not None:
                            raise RuntimeError("Cleanup guardian exited unexpectedly")
                        if time.monotonic() >= refresh_at:
                            entries = refresh_models(settings, config, entries, stop, publish_models)
                            refresh_at = time.monotonic() + 2
                        if stop.is_set():
                            break
                        heartbeat("processing")
                        verified = {name: entry for name, entry in entries.items()
                                    if entry["status"] == "verified"
                                    and file_signature(entry["path"]) == entry["signature"]}
                        blocked_models = model_wait_reasons(entries, available_memory())
                        job = claim_next(settings.database, worker_id, configuration=config.snapshot,
                                         model_revisions={name: entry["revision"] for name, entry in verified.items()},
                                         default_model=config.model.name, blocked_models=blocked_models)
                        if job:
                            process_attempt(settings, replace(config, models=verified), job, stop)
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
