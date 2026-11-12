"""Exercise the public Node entry point against real isolated services."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
ENTRY = ROOT / "scripts" / "recue.mjs"


@unittest.skipUnless(shutil.which("node"), "Node.js required for public launcher")
class ScriptTests(unittest.TestCase):
    def test_start_stop_from_unrelated_directory(self):
        services = ("api", "all") if os.name == "nt" else ("api",)
        for service in services:
            with self.subTest(service=service), tempfile.TemporaryDirectory(prefix="recue scripts ") as directory:
                data = Path(directory) / "custom data"
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                env = {**os.environ, "RECUE_DATA_DIR": str(data), "RECUE_PORT": str(port),
                       "RECUE_HOST": "127.0.0.1", "RECUE_PROCESSING_CONFIG": ""}
                command = ["node", str(ENTRY)]
                with (Path(directory) / "launcher.log").open("w+") as log:
                    process = subprocess.Popen(command + ["start", "--service", service],
                                               cwd=directory, env=env, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            self.assertIsNone(process.poll(), "Launcher exited before readiness")
                            try:
                                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                                    health = json.load(response)
                                if service == "api" or health["worker"]["status"] == "blocked_config":
                                    break
                            except OSError:
                                pass
                            time.sleep(.1)
                        else:
                            self.fail("Public launcher did not become ready")
                        self.assertTrue((data / "recue.sqlite3").is_file())
                        stopped = subprocess.run(command + ["stop", "--service", service],
                                                 cwd=directory, env=env, capture_output=True, timeout=10)
                        self.assertEqual(stopped.returncode, 0, stopped.stderr)
                        self.assertEqual(process.wait(timeout=20), 0)
                        with socket.socket() as sock:
                            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
                        self.assertFalse((data / "runtime" / f"{service}.stop").exists())
                        if service == "all":
                            from app.locking import instance_lock
                            with instance_lock(data / "runtime" / "worker.lock"):
                                pass
                    finally:
                        if process.poll() is None:
                            runtime = data / "runtime"
                            runtime.mkdir(parents=True, exist_ok=True)
                            (runtime / f"{service}.stop").write_text("stop")
                            process.wait(timeout=20)
                        log.seek(0)
                        output = log.read()
                        if process.returncode:
                            print(output)
