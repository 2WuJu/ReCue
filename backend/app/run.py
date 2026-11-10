"""Foreground supervisor used by Windows start/stop scripts."""
import argparse
import logging
import signal
import subprocess
import sys
import threading
from app.config import Settings
from app.locking import instance_lock
from app.preflight import check


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=("all", "api", "worker"), default="all")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    settings.prepare()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runtime = settings.data_dir / "runtime"
    marker = runtime / f"{args.service}.stop"
    with instance_lock(runtime / f"launcher-{args.service}.lock"):
        check(check_port=args.service != "worker")
        marker.unlink(missing_ok=True)
        commands = []
        if args.service in ("all", "api"):
            commands.append([sys.executable, "-m", "uvicorn", "app.api.main:create_app", "--factory",
                             "--host", settings.host, "--port", str(settings.port), "--no-access-log"])
        if args.service in ("all", "worker"):
            commands.append([sys.executable, "-m", "app.worker"])
        children = []
        try:
            for command in commands:
                children.append(subprocess.Popen(command))
            logging.info("Started %s; stop with Ctrl+C or scripts/stop.ps1", args.service)
            while not stop.wait(0.5) and not marker.exists():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("A service exited unexpectedly; see its output above")
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            marker.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"ReCue startup/run failed: {exc}")
