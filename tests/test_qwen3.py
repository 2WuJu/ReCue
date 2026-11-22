"""Qwen3 alignment and mixed-engine configuration regressions."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings
from app.repositories.database import connect, migrate
from app.transcription import qwen3
from app.transcription.segments import TranscriptionError
from app.worker.configuration import entry_signature, load_processing, refresh_models, select_model


def word(text, start, end):
    return dict(text=text, start_time=start, end_time=end)


class AlignmentTests(unittest.TestCase):
    def test_uses_native_boundaries_and_preserves_text(self):
        payload = dict(text="用 Node.js。再试！", words=[
            word("用", 0.08, 0.16), word("Node", 0.16, 0.32), word("js", 0.32, 0.48),
            word("再", 0.8, 0.88), word("试", 0.88, 1.04),
        ])
        self.assertEqual(qwen3.parse_result(payload, 1200), [
            dict(start_ms=80, end_ms=480, text="用 Node.js。"),
            dict(start_ms=800, end_ms=1040, text="再试！"),
        ])

    def test_silence_and_zero_width_alignment(self):
        self.assertEqual(qwen3.parse_result(dict(text="", words=[]), 1000), [])
        payload = dict(text="好。啊", words=[word("好", 0.08, 0.16), word("啊", 0.16, 0.16)])
        self.assertEqual(qwen3.parse_result(payload, 1000), [
            dict(start_ms=80, end_ms=160, text="好。啊"),
        ])

    def test_rejects_incomplete_or_invalid_alignment(self):
        for words in ([], [word("好", 0.16, 0.08)], [word("好", 0, 1.08)],
                      [word("坏", 0, 0.08)], [word("好", float("nan"), 0.08)],
                      [word("好", 0.08, 0.16), word("呀", 0, 0.08)],
                      [word("好", 0.08, 0.16)]):
            with self.subTest(words=words), self.assertRaises(TranscriptionError):
                qwen3.parse_result(dict(text="好呀", words=words), 1000)


class QwenConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.asr = self.checkpoint("recognizer")
        self.aligner = self.checkpoint("aligner", timestamp_token_id=42)
        self.config_path = self.root / "processing.json"
        self.settings = Settings(self.root, self.root, None, "ffmpeg", "127.0.0.1", 4318,
                                 self.config_path)
        self.settings.prepare()
        migrate(self.settings.database)

    def checkpoint(self, name, **config):
        directory = self.root / name
        directory.mkdir()
        (directory / "config.json").write_text(json.dumps(dict(model_type="qwen3_asr", **config)))
        (directory / "model.safetensors").write_bytes(b"checkpoint fixture")
        return directory

    def payload(self):
        return dict(version=1, backend="cpu", engine=qwen3.ENGINE, engine_version=qwen3.VERSION,
                    engine_commit="fixture", model_revision="fixture", model_name=self.asr.name,
                    model=str(self.asr), model_sha256=qwen3.checkpoint_digest(self.asr),
                    qwen3=dict(python=sys.executable, aligner=str(self.aligner),
                               aligner_sha256=qwen3.checkpoint_digest(self.aligner)))

    def load(self, payload):
        self.config_path.write_text(json.dumps(payload))
        with patch("app.worker.configuration.shutil.which", return_value=sys.executable):
            return load_processing(self.settings)

    def test_qwen_only_default_checks_checkpoint_digest(self):
        payload = self.payload()
        config = self.load(payload)
        self.assertIsNone(config.executable)
        self.assertEqual(config.snapshot["engine"], qwen3.ENGINE)
        (self.asr / "model.safetensors").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.load(payload)

    def test_mixed_catalog_selects_qwen_and_detects_aligner_changes(self):
        whisper = self.root / "ggml-fixture.bin"
        whisper.write_bytes(b"lmgg" + bytes(64))
        digest = hashlib.sha256(whisper.read_bytes()).hexdigest()
        config = self.load(dict(self.payload(), engine="whisper.cpp", model=str(whisper),
                                model_name=whisper.name, model_sha256=digest,
                                executable=str(whisper), executable_sha256=digest))
        with connect(self.settings.database) as db, db:
            db.execute("INSERT INTO model_controls(name, path) VALUES (?, ?)", ("qwen", str(self.asr)))
        with patch("app.worker.configuration.ROOT", self.root), \
             patch("app.worker.configuration.available_memory", return_value=8192), \
             patch("app.worker.configuration.probe_model") as whisper_probe, \
             patch("app.transcription.qwen3.probe_model") as qwen_probe:
            models = refresh_models(self.settings, config, {}, Event(), lambda _: None)
            self.assertEqual(set(models), {whisper.name, "qwen"})
            whisper_probe.assert_called_once()
            qwen_probe.assert_called_once()
            self.assertEqual(models["qwen"]["status"], "verified")
            selected = select_model(replace(config, models=models), "qwen")
            self.assertEqual(selected.snapshot["engine"], qwen3.ENGINE)
            self.assertEqual(selected.model, self.asr)
            self.assertEqual(selected.snapshot["model_name"], "qwen")
            (self.aligner / "model.safetensors").write_bytes(b"changed")
            self.assertNotEqual(entry_signature(models["qwen"]), models["qwen"]["signature"])
            changed = refresh_models(self.settings, config, models, Event(), lambda _: None)
            self.assertEqual(changed["qwen"]["status"], "check_failed")
            self.assertEqual(changed[whisper.name]["status"], "verified")
            qwen_probe.assert_called_once()

    def test_whisper_config_discovers_local_qwen_without_registration(self):
        group = self.root / "models" / "qwen3"
        group.mkdir(parents=True)
        asr = self.asr.rename(group / "Qwen3-ASR-0.6B")
        aligner = self.aligner.rename(group / qwen3.ALIGNER_NAME)
        incomplete = group / "incomplete"
        incomplete.mkdir()
        (incomplete / "config.json").write_text('{"model_type": "qwen3_asr"}')
        whisper = self.root / "ggml-fixture.bin"
        whisper.write_bytes(b"lmgg" + bytes(64))
        digest = hashlib.sha256(whisper.read_bytes()).hexdigest()
        config = self.load(dict(version=1, backend="cpu", engine_version="fixture", engine_commit="fixture",
                                model_revision="fixture", model_name=whisper.name, model=str(whisper),
                                model_sha256=digest, executable=str(whisper), executable_sha256=digest))

        with patch("app.worker.configuration.ROOT", self.root), \
             patch("app.worker.configuration.available_memory", return_value=8192), \
             patch("app.worker.configuration.probe_model"), \
             patch("app.transcription.qwen3.probe_model") as qwen_probe:
            models = refresh_models(self.settings, config, {}, Event(), lambda _: None)
            self.assertEqual(set(models), {whisper.name, asr.name})
            self.assertEqual(models[asr.name]["status"], "unsupported")
            qwen_probe.assert_not_called()

            python = self.root / qwen3.RUNTIME_PYTHON
            python.parent.mkdir(parents=True)
            python.touch()
            models = refresh_models(self.settings, config, models, Event(), lambda _: None)
            self.assertEqual(models[asr.name]["status"], "verified")
            self.assertTrue(models[whisper.name]["is_default"])
            qwen_probe.assert_called_once()
            selected = select_model(replace(config, models=models), asr.name)
            self.assertEqual(selected.snapshot["engine"], qwen3.ENGINE)
            self.assertEqual(selected.snapshot["qwen3"], dict(
                python=str(python), aligner=str(aligner), aligner_sha256=qwen3.checkpoint_digest(aligner)))
            self.assertEqual(selected.model, asr)
            self.assertNotIn("qwen3", config.snapshot)
