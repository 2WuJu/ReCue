"""Collect the local paths needed for the first transcription setup."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory

from app.config import ROOT
from app.locking import AlreadyRunning, instance_lock
from app.transcription import qwen3


def ask_path(prompt, accepts):
    while True:
        value = input(prompt).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            if os.name == "nt":
                value = re.sub(r"^/([a-zA-Z])/", r"\1:/", value)
            path = (ROOT / Path(value).expanduser()).absolute()
            if accepts(path):
                return path
        print("找不到符合要求的路径，请重新输入。")


def is_checkpoint(path, *, aligner=False):
    try:
        qwen3.check_checkpoint(path, aligner=aligner)
    except (OSError, ValueError) as error:
        print(error)
        return False
    return True


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def first_config():
    model = ask_path("请输入 Whisper .bin 文件或 Qwen3-ASR 模型目录的路径：",
                     lambda path: path.is_file() or (path.is_dir() and is_checkpoint(path)))
    settings = dict(version=1, backend="cpu", model=str(model), model_name=model.name,
                    engine_commit="unrecorded", model_revision="unrecorded", ffprobe="ffprobe")
    if model.is_dir():
        aligner = model.parent / "Qwen3-ForcedAligner-0.6B"
        if not aligner.is_dir() or not is_checkpoint(aligner, aligner=True):
            aligner = ask_path("请输入 Qwen3-ForcedAligner-0.6B 目录的路径：",
                               lambda path: path.is_dir() and is_checkpoint(path, aligner=True))
        python_path = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        python = ROOT / ".local/qwen3-runtime" / python_path
        if not python.is_file():
            python = ask_path("请输入已安装 qwen-asr==0.0.6 的 Python 可执行文件路径：", Path.is_file)
        print("正在计算模型指纹……", flush=True)
        settings.update(engine=qwen3.ENGINE, engine_version=qwen3.VERSION,
                        model_sha256=qwen3.checkpoint_digest(model),
                        qwen3=dict(python=str(python), aligner=str(aligner),
                                   aligner_sha256=qwen3.checkpoint_digest(aligner)))
    else:
        cli = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
        found = shutil.which(cli)
        executable = Path(found).absolute() if found else ask_path(f"请输入 {cli} 的路径：", Path.is_file)
        print("正在计算模型指纹……", flush=True)
        settings.update(engine="whisper.cpp", engine_version="unrecorded", executable=str(executable),
                        executable_sha256=file_digest(executable), model_sha256=file_digest(model))
    return settings


def main():
    config = ROOT / "processing.local.json"
    if config.exists():
        return
    local = ROOT / ".local"
    try:
        local.mkdir(exist_ok=True)
        with instance_lock(local / "model-setup.lock"):
            if config.exists():
                return
            print("首次转写配置。模型下载和准备方式见项目的 USAGE.md。")
            settings = first_config()
            with TemporaryDirectory(prefix="model-setup-", dir=local) as directory:
                staged = Path(directory) / config.name
                staged.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
                staged.replace(config)
    except AlreadyRunning:
        sys.exit("另一个启动进程正在配置模型，请在该终端完成配置。")
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        detail = str(error) or "输入已结束，请重新运行 node recue.mjs。"
        sys.exit(f"转写配置失败：{detail}")


if __name__ == "__main__":
    main()
