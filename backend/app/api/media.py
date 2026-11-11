import base64
import binascii
import json
import sqlite3
from fastapi.responses import FileResponse
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.uploads import ErrorResponse
from app.errors import ServiceError
from app.repositories.media import get_media, list_media
from app.repositories.transcripts import get_transcript
from app.repositories.queue import MediaNotFound, QueueConflict, retry
from app.services.uploads import storage_error


class JobView(BaseModel):
    id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "interrupted", "cancelled"]
    stage: Literal["preprocessing", "transcribing", "organizing"] | None
    attempt: int
    created_at: int
    started_at: int | None
    finished_at: int | None
    error_code: str | None
    error_message: str | None
    engine: str | None
    model: str | None
    backend: str | None
    processing_version: str | None


class MediaView(BaseModel):
    id: UUID
    display_name: str
    media_type: Literal["audio", "video"] | None
    duration_ms: int | None
    size_bytes: int
    created_at: int
    lifecycle: Literal["active", "deleting", "delete_failed"]
    latest_job: JobView | None


class MediaPage(BaseModel):
    items: list[MediaView]
    next_cursor: str | None


class RetryReceipt(BaseModel):
    media_id: UUID
    job_id: UUID


class TranscriptSegment(BaseModel):
    id: UUID
    sequence: int
    start_ms: int
    end_ms: int
    text: str


class Transcript(BaseModel):
    media_id: UUID
    version: int
    segments: list[TranscriptSegment]


def encode_cursor(media):
    return base64.urlsafe_b64encode(json.dumps(
        [media["created_at"], media["id"]], separators=(",", ":"),
    ).encode()).decode().rstrip("=")


def decode_cursor(cursor):
    if cursor is None:
        return None
    try:
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                           altchars=b"-_", validate=True))
        if (not isinstance(value, list) or len(value) != 2 or type(value[0]) is not int
                or not 0 <= value[0] <= 2**63 - 1 or not isinstance(value[1], str)
                or str(UUID(value[1])) != value[1]):
            raise ValueError("Invalid cursor tuple")
        return tuple(value)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ServiceError(422, "invalid_cursor", "分页游标无效，请重新加载列表") from exc


def media_router(settings):
    router = APIRouter()
    errors = {code: {"model": ErrorResponse} for code in (422, 503)}

    @router.get("/api/media", response_model=MediaPage, responses=errors,
                summary="按创建时间和ID倒序读取媒体及最新任务")
    def listing(limit: Annotated[int, Query(ge=1, le=100)] = 20,
                cursor: Annotated[str | None, Query(max_length=256)] = None):
        before = decode_cursor(cursor)
        try:
            items, more = list_media(settings.database, limit, before)
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        return MediaPage(items=[MediaView.model_validate(item) for item in items],
                         next_cursor=encode_cursor(items[-1]) if more else None)

    @router.get("/api/media/{media_id}", response_model=MediaView,
                responses={**errors, 404: {"model": ErrorResponse}})
    def detail(media_id: UUID):
        try:
            item = get_media(settings.database, str(media_id))
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        if not item:
            raise ServiceError(404, "media_not_found", "媒体不存在")
        return MediaView.model_validate(item)

    @router.get("/api/media/{media_id}/playback", response_class=FileResponse,
                responses={206: {"description": "Requested byte range"},
                           416: {"description": "Unsatisfiable byte range"},
                           **{code: {"model": ErrorResponse} for code in (404, 409, 503)}})
    @router.head("/api/media/{media_id}/playback", include_in_schema=False)
    def playback(media_id: UUID):
        try:
            item = get_media(settings.database, str(media_id))
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        if not item:
            raise ServiceError(404, "media_not_found", "媒体不存在")
        if (item["lifecycle"] != "active" or not item["latest_job"]
                or item["latest_job"]["status"] != "succeeded" or not item["playback_path"]):
            raise ServiceError(409, "playback_not_ready", "播放副本尚未就绪", True)
        path = (settings.data_dir / item["playback_path"]).resolve()
        if not path.is_relative_to((settings.data_dir / "media").resolve()) or not path.is_file():
            raise ServiceError(503, "playback_missing", "播放副本不可用，请检查本地存储", True)
        return FileResponse(path, media_type="video/mp4" if item["media_type"] == "video" else "audio/mp4",
                            headers={"Cache-Control": "private, no-cache"})

    @router.post("/api/media/{media_id}/retry", response_model=RetryReceipt, status_code=201,
                 responses={**errors, **{code: {"model": ErrorResponse} for code in (403, 404, 409)}},
                 summary="为失败或中断的媒体创建新attempt；保留旧任务")
    def retry_media(media_id: UUID):
        try:
            job_id = retry(settings.database, str(media_id))
        except MediaNotFound as exc:
            raise ServiceError(404, "media_not_found", "媒体不存在") from exc
        except QueueConflict as exc:
            raise ServiceError(409, "retry_conflict", "当前任务或媒体状态不允许重试") from exc
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        return RetryReceipt(media_id=media_id, job_id=job_id)

    @router.get("/api/media/{media_id}/transcript", response_model=Transcript,
                responses={**errors, **{code: {"model": ErrorResponse} for code in (404, 409)}},
                summary="读取完整发布讲稿；处理中或尚未发布返回409")
    def transcript(media_id: UUID):
        try:
            result = get_transcript(settings.database, str(media_id))
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        if result is None:
            raise ServiceError(404, "media_not_found", "媒体不存在")
        if result.get("ready") is False:
            raise ServiceError(409, "transcript_not_ready", "讲稿尚未发布", True)
        return Transcript.model_validate(result)

    return router
