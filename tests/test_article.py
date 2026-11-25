"""Lossless punctuation mapping, publication/cache boundaries and HTTP contract."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings
from app.errors import ServiceError
from app.repositories.database import connect, migrate
from app.repositories.queue import claim_next, received
from app.repositories.transcripts import get_transcript, publish, update_segment
from app.services.article import ArticleFormatter
from app.transcription.article import contextual_insertions, format_article, restore_insertions
from http_support import HTTPAPICase


def rendered(segments, paragraphs):
    return ["".join(segments[part["segment"]]["text"].encode("utf-16-le")[
        part["start"] * 2:part["end"] * 2].decode("utf-16-le") + part["suffix"]
        for part in paragraph) for paragraph in paragraphs]


def seed(database, data):
    media_id = str(uuid4())
    (data / "media" / media_id).mkdir(parents=True)
    received(database, request_id=str(uuid4()), media_id=media_id,
             display_name="article.wav", source_path=f"media/{media_id}/source", size_bytes=100)
    job = claim_next(database, "owner")
    publish(database, job["id"], "owner", duration_ms=1000, media_type="audio",
            segments=[{"start_ms": 0, "end_ms": 900, "text": "你好世界"}],
            engine="test", model="test", processing_version="test")
    return media_id


class ArticleFormatTests(unittest.TestCase):
    def test_long_context_dropped_words_retry_with_shorter_contexts(self):
        source = "前半句话" * 20 + " " + "后半句话" * 20
        def model(value):
            return value[4:] + "。" if len(value) > 100 else value + "。"
        additions = contextual_insertions(source, model)
        self.assertTrue(additions)
        self.assertEqual(max(additions), len(source))
        with self.assertRaises(ValueError):
            contextual_insertions("不能删除原文", lambda value: "错误输出。")

    def test_context_punctuation_can_split_inside_segments_and_preserves_emoji_offsets(self):
        segments = [{"start_ms": 0, "end_ms": 500, "text": "你好😀世界"},
                    {"start_ms": 500, "end_ms": 1000, "text": "你好吗"}]
        paragraphs = format_article(segments, lambda text: "你好😀，世界。你好吗？")
        self.assertEqual(rendered(segments, paragraphs), ["你好😀，世界。你好吗？"])
        self.assertEqual(paragraphs[0][0], {"segment": 0, "start": 0, "end": 4, "suffix": "，"})
        self.assertEqual(paragraphs[0][-1]["segment"], 1)

    def test_existing_punctuation_and_code_are_kept_exactly(self):
        source = "Docker docker.io C++ C# foo_bar stdio.h，你好！"
        predicted = "Docker docker . io C + + C # foo _ bar stdio . h，你好！"
        additions = restore_insertions(source, predicted)
        self.assertEqual(additions, {})
        self.assertEqual(restore_insertions("你好世界", "你好，世界。"), {2: "，", 4: "。"})
        with self.assertRaisesRegex(ValueError, "changed source"):
            restore_insertions("React", "Vue。")

    def test_paragraphs_have_real_sentence_breaks_and_no_characters_are_lost(self):
        text = "第一句第二句第三句第四句"
        segments = [{"start_ms": 0, "end_ms": 1000, "text": text}]
        paragraphs = format_article(segments, lambda value: "第一句。第二句。第三句。第四句。")
        self.assertEqual(rendered(segments, paragraphs), ["第一句。第二句。第三句。", "第四句。"])
        self.assertEqual("".join(rendered(segments, paragraphs)).replace("。", ""), text)
        self.assertEqual(format_article([], lambda value: self.fail("empty input must not infer")), [])


class ArticleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = Settings(self.data, ROOT / "frontend/dist", None, "ffmpeg", "127.0.0.1", 4318)
        self.settings.prepare(); migrate(self.settings.database)
        self.media_id = seed(self.settings.database, self.data)
        self.formatter = ArticleFormatter(self.settings)
        self.model = self.data / "fake-model.onnx"; self.model.touch()
        self.env = patch.dict("os.environ", {"RECUE_PUNCTUATION_MODEL": str(self.model)})
        self.env.start(); self.addCleanup(self.env.stop)

    def fake_infer(self, command, **kwargs):
        Path(command[-1]).write_text(json.dumps([[{"segment": 0, "start": 0, "end": 4, "suffix": "。"}]]), encoding="utf-8")

    def test_cache_survives_formatter_restart_and_original_transcript_is_unchanged(self):
        original = get_transcript(self.settings.database, self.media_id)
        with patch("app.services.article.run_tool", side_effect=self.fake_infer) as inference:
            article = self.formatter.create(self.media_id)
            self.assertEqual(ArticleFormatter(self.settings).create(self.media_id), article)
            self.assertEqual(inference.call_count, 1)
        self.assertEqual(get_transcript(self.settings.database, self.media_id), original)

    def test_edit_rebuilds_article_offsets_from_current_text(self):
        database = self.settings.database
        original = get_transcript(database, self.media_id)

        def format_input(command, **kwargs):
            segments = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
            result = format_article(segments, lambda value: value + "。")
            Path(command[-1]).write_text(json.dumps(result), encoding="utf-8")

        with patch("app.services.article.run_tool", side_effect=format_input) as inference:
            before = self.formatter.create(self.media_id)
            updated = update_segment(database, self.media_id, original["segments"][0]["id"],
                                     original["version"], "已经修订的句子😀")
            after = self.formatter.create(self.media_id)
            self.assertEqual(inference.call_count, 2)
        self.assertNotEqual(before["version"], after["version"])
        self.assertEqual(after["version"], updated["version"])
        self.assertEqual(rendered(updated["segments"], after["paragraphs"]), ["已经修订的句子😀。"])

    def test_deletion_during_inference_cannot_publish_or_recreate_media(self):
        def deleting(command, **kwargs):
            self.fake_infer(command, **kwargs)
            with connect(self.settings.database) as db, db:
                db.execute("UPDATE media SET lifecycle='deleting' WHERE id=?", (self.media_id,))
            (self.data / "media" / self.media_id).rmdir()
        with patch("app.services.article.run_tool", side_effect=deleting), self.assertRaises(ServiceError) as error:
            self.formatter.create(self.media_id)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse((self.data / "media" / self.media_id).exists())
        self.assertEqual(list((self.data / "tmp").iterdir()), [])

    def test_busy_and_missing_model_return_actionable_errors(self):
        self.formatter.lock.acquire()
        try:
            with self.assertRaises(ServiceError) as error:
                self.formatter.create(self.media_id)
            self.assertEqual(error.exception.code, "article_busy")
        finally:
            self.formatter.lock.release()
        self.model.unlink()
        with self.assertRaises(ServiceError) as error:
            self.formatter.create(self.media_id)
        self.assertEqual(error.exception.code, "punctuation_model_missing")


class ArticleHTTPTests(HTTPAPICase):
    def test_unknown_and_unpublished_article_have_explicit_errors(self):
        media_id = str(uuid4())
        self.assertEqual(self.http("POST", f"/api/media/{media_id}/article")[0], 404)
        received(self.data / "recue.sqlite3", request_id=str(uuid4()), media_id=media_id,
                 display_name="pending.wav", source_path=f"media/{media_id}/source", size_bytes=100)
        self.assertEqual(self.http("POST", f"/api/media/{media_id}/article")[0], 409)


if __name__ == "__main__":
    unittest.main()
