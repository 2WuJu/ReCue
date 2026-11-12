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


async def maintain_deletions(settings, stop: asyncio.Event, *, interval=2):
    """Resume persisted intent on startup; failures stay visible until explicit retry."""
    from app.services.deletion import cleanup_deletions
    while not stop.is_set():
        try:
            await run_in_threadpool(cleanup_deletions, settings)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Deletion cleanup deferred: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
