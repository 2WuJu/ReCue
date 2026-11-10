"""T01 integration checks. Run from root with unittest discovery."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.repositories.database import connect, migrate


class FoundationTests(unittest.TestCase):
    def test_launcher_stops_children(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = {**os.environ, "RECUE_DATA_DIR": directory, "RECUE_PORT": str(port)}
            process = subprocess.Popen([sys.executable, "-m", "app.run"],
                                       cwd=ROOT / "backend", env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                            if json.load(response)["worker"]["status"] == "idle":
                                break
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        self.fail("Launcher did not start both services")
                    time.sleep(0.1)
                (Path(directory) / "runtime" / "all.stop").write_text("stop")
                self.assertEqual(process.wait(timeout=15), 0)
                with socket.socket() as sock:
                    self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
                # No old worker can retain the lock after the launcher exits.
                from app.locking import instance_lock
                with instance_lock(Path(directory) / "runtime" / "worker.lock"):
                    pass
            finally:
                if process.poll() is None:
                    (Path(directory) / "runtime" / "all.stop").write_text("stop")
                    process.wait(timeout=15)

    def test_migration_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            migrate(path)
            with connect(path) as db, db:
                db.execute("INSERT INTO worker_status VALUES (1, 'existing', 1, 0, 'stopped')")
            migrate(path)
            with connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT worker_id FROM worker_status").fetchone()[0], "existing")
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_worker_lock_and_api_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "RECUE_DATA_DIR": directory}
            children = []
            def launch(*args):
                child = subprocess.Popen([sys.executable, *args], cwd=ROOT / "backend", env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.append(child)
                return child
            def wait_for(callback):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        result = callback()
                        if result:
                            return result
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.1)
                self.fail("Timed out waiting for service")
            try:
                worker = launch("-m", "app.worker")
                db_path = Path(directory) / "recue.sqlite3"
                def worker_record():
                    if not db_path.exists():
                        return None
                    with connect(db_path) as db:
                        return db.execute("SELECT * FROM worker_status").fetchone()
                original = wait_for(worker_record)["worker_id"]
                duplicate = launch("-m", "app.worker")
                self.assertNotEqual(duplicate.wait(timeout=15), 0)
                self.assertIsNone(worker.poll())
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                url = f"http://127.0.0.1:{port}"
                for _ in range(2):
                    api = launch("-m", "uvicorn", "app.api.main:create_app", "--factory", "--port", str(port))
                    def get_health():
                        with urlopen(url + "/api/health", timeout=1) as response:
                            return json.load(response)
                    health = wait_for(get_health)
                    self.assertEqual(health["worker"]["status"], "idle")
                    self.assertFalse(health["transcription_ready"])
                    self.assertEqual(wait_for(worker_record)["worker_id"], original)
                    with urlopen(url) as response:
                        self.assertIn(b'<div id="root">', response.read())
                    api.terminate()
                    api.wait(timeout=10)
                self.assertIsNone(worker.poll())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
