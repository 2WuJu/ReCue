import asyncio
import errno
import logging
import sqlite3
from uuid import uuid4

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request

from app.errors import ServiceError
from app.locking import AlreadyRunning
from app.repositories.queue import find_receipt, received
from app.storage.uploads import (
    MAX_UPLOAD_BYTES, flush_file, remove_uncommitted, upload_lock,
)

logger = logging.getLogger(__name__)


def storage_error(exc):
    if ((isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT))
            or getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL):
        return ServiceError(507, "storage_full", "存储空间不足，请释放空间后重试", True)
    return ServiceError(503, "storage_unavailable", "存储暂不可用，请检查服务端存储后重试", True)


def lookup_receipt(database, request_id):
    try:
        return find_receipt(database, request_id)
    except sqlite3.Error as exc:
        raise storage_error(exc) from exc


def finalize_upload(settings, temporary, source, request_id, media_id, display_name, total):
    committed = False
    source_owned = False
    try:
        source.parent.mkdir()
        source_owned = True
        temporary.replace(source)
        receipt = received(
            settings.database, request_id=request_id, media_id=media_id,
            display_name=display_name, source_path=source.relative_to(settings.data_dir).as_posix(),
            size_bytes=total,
        )
        committed = receipt["media_id"] == media_id
        return receipt, committed
    finally:
        if not committed:
            remove_uncommitted(temporary, source, source_owned=source_owned)


async def finish_before_cancelling(function, *args):
    """Keep the OS lock until the finalizer thread commits or cleans up.

    A disconnected/cancelled request must never unlink a committed source file,
    or release the recovery lock while a commit is still in flight.
    """
    task = asyncio.create_task(run_in_threadpool(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def receive_upload(request: Request, settings, request_id: str, display_name: str,
                         *, max_bytes=MAX_UPLOAD_BYTES, idle_timeout=60):
    # Replays never consume the body or create a second file/job.
    receipt = await run_in_threadpool(lookup_receipt, settings.database, request_id)
    if receipt:
        return receipt, False
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise ServiceError(415, "unsupported_content_type", "请以application/octet-stream上传原始文件")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise ServiceError(400, "invalid_content_length", "上传长度无效")
        # Bound integer parsing too; an untrusted header need not fit Python's int limit.
        if len(length) > 20 or int(length) > max_bytes:
            raise ServiceError(413, "upload_too_large", "单文件不能超过4GiB")
        length = int(length)
    temporary = source = None
    finalizing = False
    temporary_owned = False
    try:
        with upload_lock(settings.data_dir):
            receipt = await run_in_threadpool(lookup_receipt, settings.database, request_id)
            if receipt:
                return receipt, False
            media_id = str(uuid4())
            temporary = settings.data_dir / "tmp" / f"{media_id}.part"
            source = settings.data_dir / "media" / media_id / "source"
            try:
                total = 0
                with temporary.open("xb") as handle:
                    temporary_owned = True
                    stream = request.stream().__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(anext(stream), timeout=idle_timeout)
                        except StopAsyncIteration:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ServiceError(413, "upload_too_large", "单文件不能超过4GiB")
                        if length is not None and total > length:
                            raise ServiceError(400, "upload_length_mismatch", "接收长度与声明不符")
                        if chunk:
                            await run_in_threadpool(handle.write, chunk)
                    if total == 0:
                        raise ServiceError(400, "empty_upload", "上传文件不能为空")
                    if length is not None and total != length:
                        raise ServiceError(400, "upload_length_mismatch", "文件接收不完整，请重新上传", True)
                    await run_in_threadpool(flush_file, handle)
                finalizing = True
                with CancelScope(shield=True):
                    return await finish_before_cancelling(
                        finalize_upload, settings, temporary, source,
                        request_id, media_id, display_name, total,
                    )
            finally:
                # Cleanup stays inside the lock, including cancellation/disconnect paths.
                # Synchronous bounded deletion cannot be cancelled halfway through.
                if temporary_owned and not finalizing:
                    remove_uncommitted(temporary, source)
    except AlreadyRunning as exc:
        raise ServiceError(409, "upload_busy", "已有文件正在接收，请稍后查询请求ID或重试", True) from exc
    except ClientDisconnect as exc:
        raise ServiceError(400, "upload_interrupted", "上传连接已中断，请重新上传", True) from exc
    except TimeoutError as exc:
        raise ServiceError(408, "upload_timeout", "上传长时间未收到数据，请重试", True) from exc
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Upload storage operation failed: %s", type(exc).__name__)
        raise storage_error(exc) from exc
