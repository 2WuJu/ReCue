"""Server-owned paths and recovery, serialized with all upload finalization."""
import logging
import os
from pathlib import Path
import time
from uuid import UUID

from app.locking import AlreadyRunning, instance_lock
from app.repositories.database import connect

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 4 * 1024**3


def upload_lock(data_dir: Path):
    return instance_lock(data_dir / "runtime" / "uploads.lock")


def flush_file(handle):
    handle.flush()
    os.fsync(handle.fileno())


def remove_uncommitted(temporary: Path, source: Path, *, source_owned=False):
    """Best effort after failure; startup recovery retries files still in use."""
    for path in ((temporary, source) if source_owned else (temporary,)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Upload cleanup deferred for %s", path.name)
    if source_owned:
        try:
            source.parent.rmdir()  # Only an empty directory; never recursive deletion.
        except OSError:
            pass


def _server_id(value: str):
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def recover_uploads(data_dir: Path, database: Path):
    """Startup scan; skip if another API instance is receiving a file.

    Only owned UUID .part files and unreferenced UUID/source files are eligible.
    All DB references (including deleting media) protect files. Other artifacts,
    symlinks and directories with unknown contents are left alone.
    """
    try:
        with upload_lock(data_dir):
            with connect(database) as db:
                referenced = {row[0] for row in db.execute("SELECT source_path FROM media")}
            for path in (data_dir / "tmp").glob("*.part"):
                if _server_id(path.stem) and not path.is_symlink():
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Temporary upload cleanup deferred for %s", path.name)
            cutoff = time.time() - 24 * 60 * 60
            for directory in (data_dir / "media").iterdir():
                if (not _server_id(directory.name) or directory.is_symlink()
                        or directory.is_junction() or not directory.is_dir()):
                    continue
                source = directory / "source"
                relative = source.relative_to(data_dir).as_posix()
                if relative in referenced or source.is_symlink():
                    continue
                try:
                    # A grace period also retains recent rename-before-commit evidence.
                    if source.is_file() and source.stat().st_mtime < cutoff:
                        source.unlink()
                    directory.rmdir()
                except OSError:
                    logger.debug("Retaining recent or occupied upload directory %s", directory.name)
    except AlreadyRunning:
        logger.info("Upload recovery skipped: a receiver holds the upload lock")
