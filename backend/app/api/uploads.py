from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel

from app.errors import ServiceError
from app.services.uploads import lookup_receipt, receive_upload


class UploadReceipt(BaseModel):
    request_id: UUID
    media_id: UUID
    job_id: UUID
    status: Literal["received"] = "received"


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool


def upload_router(settings):
    router = APIRouter()
    errors = {code: {"model": ErrorResponse} for code in (400, 403, 408, 409, 413, 415, 422, 503, 507)}

    @router.post("/api/media", response_model=UploadReceipt, status_code=201,
                 responses={**errors, 200: {"model": UploadReceipt}},
                 summary="接收单文件并入队；不表示媒体校验或识别通过",
                 openapi_extra={"requestBody": {"required": True, "content": {
                     "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                 }}})
    async def upload(request: Request, response: Response,
                     request_id: Annotated[UUID, Header(alias="X-Upload-ID")],
                     display_name: Annotated[str, Query(min_length=1, max_length=255)]):
        if not display_name.strip() or any(ord(char) < 32 for char in display_name):
            raise ServiceError(422, "invalid_display_name", "文件名不能为空或包含控制字符")
        receipt, created = await receive_upload(request, settings, str(request_id), display_name)
        response.status_code = 201 if created else 200
        return UploadReceipt(request_id=request_id, **receipt)

    @router.get("/api/uploads/{request_id}", response_model=UploadReceipt,
                responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
                           503: {"model": ErrorResponse}},
                summary="查询已完成接收的结果；404不代表活跃上传已停止")
    def receipt(request_id: UUID):
        result = lookup_receipt(settings.database, str(request_id))
        if not result:
            raise ServiceError(404, "upload_not_received", "未找到已完成的接收记录；可稍后查询", True)
        return UploadReceipt(request_id=request_id, **result)

    return router
