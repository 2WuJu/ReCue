from contextlib import asynccontextmanager
import asyncio
import shutil
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.uploads import upload_router
from app.api.media import media_router
from app.config import Settings
from app.errors import ServiceError
from app.repositories.database import connect, migrate
from app.storage.uploads import recover_uploads
from app.services.maintenance import maintain_uploads


class WorkerHealth(BaseModel):
    status: str
    heartbeat_age_seconds: float | None = None


class Health(BaseModel):
    status: str
    database: str
    worker: WorkerHealth
    ffmpeg_available: bool
    model_file_available: bool
    transcription_ready: bool


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        settings.prepare()
        migrate(settings.database)
        await run_in_threadpool(recover_uploads, settings.data_dir, settings.database)
        stop = asyncio.Event()
        maintenance = asyncio.create_task(maintain_uploads(settings, stop))
        try:
            yield
        finally:
            stop.set()
            await maintenance

    # Disable CDN-backed Swagger/ReDoc pages: OpenAPI JSON stays available.
    app = FastAPI(title="ReCue", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=exc.status, content={
            "code": exc.code, "message": exc.message, "retryable": exc.retryable,
        })

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={
            "code": "invalid_request", "message": "请求参数无效，请检查ID、文件名及分页参数", "retryable": False,
        })

    app.include_router(upload_router(settings))
    app.include_router(media_router(settings))

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={
                    "code": "origin_rejected", "message": "请求来源与服务地址不一致", "retryable": False,
                })
        return await call_next(request)

    @app.get("/api/health", response_model=Health)
    def health():
        with connect(settings.database) as db:
            db.execute("SELECT 1")
            row = db.execute("SELECT * FROM worker_status WHERE singleton=1").fetchone()
        age = max(0, time.time() - row["heartbeat"]) if row else None
        state = "offline"
        if row and row["state"] != "stopped" and age < 10:
            state = "idle"
        return Health(status="ok", database="ok",
                      worker=WorkerHealth(status=state, heartbeat_age_seconds=age),
                      ffmpeg_available=shutil.which(settings.ffmpeg) is not None,
                      model_file_available=bool(settings.model_path and settings.model_path.is_file()),
                      transcription_ready=False)

    if (settings.frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        page = settings.frontend_dir / "index.html"
        if not page.is_file():
            return JSONResponse(status_code=503, content={
                "code": "frontend_missing", "message": "请先运行 scripts/setup.ps1 构建前端", "retryable": False,
            })
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    return app
