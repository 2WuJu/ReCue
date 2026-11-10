import asyncio
import logging
import sqlite3

from starlette.concurrency import run_in_threadpool

from app.storage.uploads import recover_uploads

logger = logging.getLogger(__name__)


async def maintain_uploads(settings, stop: asyncio.Event, *, interval=3600):
    """Retry recovery hourly; the OS upload lock protects active receivers."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await run_in_threadpool(recover_uploads, settings.data_dir, settings.database)
        except (OSError, sqlite3.Error) as exc:
            # A failed pass must not stop future recovery or the API.
            logger.warning("Scheduled upload recovery failed: %s", type(exc).__name__)
