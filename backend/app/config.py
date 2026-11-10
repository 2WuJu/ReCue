import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    frontend_dir: Path
    model_path: Path | None
    ffmpeg: str
    host: str
    port: int

    @property
    def database(self) -> Path:
        return self.data_dir / "recue.sqlite3"

    @classmethod
    def load(cls):
        model = os.getenv("RECUE_MODEL_PATH")
        port = int(os.getenv("RECUE_PORT", "4318"))
        if not 1 <= port <= 65535:
            raise ValueError("RECUE_PORT must be between 1 and 65535")
        return cls(
            Path(os.getenv("RECUE_DATA_DIR", str(ROOT / "data"))).resolve(),
            ROOT / "frontend" / "dist",
            Path(model).resolve() if model else None,
            os.getenv("RECUE_FFMPEG", "ffmpeg"),
            os.getenv("RECUE_HOST", "127.0.0.1"),
            port,
        )

    def prepare(self):
        for name in ("", "tmp", "media", "runtime", "logs"):
            directory = self.data_dir / name
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write-check-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
