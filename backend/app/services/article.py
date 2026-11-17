"""Generate/cache the article separately from the original, published transcript."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock

from app.config import ROOT
from app.errors import ServiceError
from app.processes import run_tool
from app.repositories.transcripts import get_transcript
from app.transcription.article import FORMAT_VERSION


class ArticleFormatter:
    def __init__(self, settings):
        self.settings = settings
        self.lock = Lock()

    def create(self, media_id):
        transcript = get_transcript(self.settings.database, media_id)
        if transcript is None:
            raise ServiceError(404, "media_not_found", "媒体不存在")
        if transcript.get("ready") is False:
            raise ServiceError(409, "transcript_not_ready", "讲稿尚未发布", True)
        digest = hashlib.sha256(json.dumps([FORMAT_VERSION, transcript], sort_keys=True).encode()).hexdigest()
        cache = self.settings.data_dir / "media" / media_id / f"article-{digest}.json"
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8"))
        model = Path(os.getenv("RECUE_PUNCTUATION_MODEL", str(ROOT / "models/punctuation/model.int8.onnx")))
        if not model.is_file():
            raise ServiceError(503, "punctuation_model_missing", "文字稿模型尚未安装，请在处理电脑运行 uv run python scripts/setup-punctuation.py", True)
        if not self.lock.acquire(blocking=False):
            raise ServiceError(503, "article_busy", "正在整理另一份文字稿，请稍后重试", True)
        try:
            with TemporaryDirectory(prefix="article-", dir=self.settings.data_dir / "tmp") as directory:
                work = Path(directory)
                source, output = work / "input.json", work / "output.json"
                source.write_text(json.dumps(transcript["segments"], ensure_ascii=False), encoding="utf-8")
                try:
                    with (work / "process.log").open("wb") as log:
                        run_tool([sys.executable, str(ROOT / "backend/app/transcription/article.py"),
                                  str(model.resolve()), str(source), str(output)],
                                 stdout=log, stderr=log, timeout=120)
                    paragraphs = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    raise ServiceError(503, "article_failed", "本地文字稿整理失败，请检查标点模型并重试。原始讲稿仍可阅读。", True) from exc
                result = {"media_id": media_id, "version": transcript["version"],
                          "model": FORMAT_VERSION, "paragraphs": paragraphs}
                # Recheck before publishing; never recreate a deleted media directory.
                if get_transcript(self.settings.database, media_id) != transcript:
                    raise ServiceError(409, "transcript_changed", "媒体或讲稿已变化，请重新加载", True)
                output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                try:
                    output.replace(cache)
                except OSError as exc:
                    raise ServiceError(503, "article_save_failed", "文字稿保存失败，请检查存储空间后重试", True) from exc
                return result
        finally:
            self.lock.release()
