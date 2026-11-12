import argparse
import json
import shutil
import socket
import subprocess

from app.config import Settings
from app.repositories.database import migrate


def check(require_model=False, check_port=True):
    settings = Settings.load()
    settings.prepare()
    migrate(settings.database)
    executable = shutil.which(settings.ffmpeg)
    if not executable:
        raise RuntimeError("FFmpeg missing: install FFmpeg or set RECUE_FFMPEG to ffmpeg.exe")
    subprocess.run([executable, "-version"], check=True, capture_output=True, timeout=10)
    if not (settings.frontend_dir / "index.html").is_file():
        raise RuntimeError("Frontend missing: run node scripts/recue.mjs setup or npm run build in frontend")
    model = bool(settings.model_path and settings.model_path.is_file())
    if require_model and not model:
        raise RuntimeError("Model missing: set RECUE_MODEL_PATH to an existing model file (M0 pending)")
    if check_port:
        with socket.socket() as sock:
            # Windows must reject an occupied port rather than share its binding.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind((settings.host, settings.port))
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot bind {settings.host}:{settings.port}; port occupied or reserved by Windows. "
                    "Stop the conflicting service or set RECUE_PORT to an available port."
                ) from exc
    return {"database": "ok", "ffmpeg": "ok", "frontend": "ok",
            "model_file_available": model, "transcription_ready": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.require_model), ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"Preflight failed: {exc}\n")
