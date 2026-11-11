"""Real Windows descendants and owner termination, not mocked Popen behavior."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.processes import ProcessCancelled, run_tool

FIXTURE = str(Path(__file__).with_name("process_fixture.py"))


@unittest.skipUnless(os.name == "nt", "Windows process containment")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ReCue process 测试 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]

    def open_process(self, pid):
        handle = self.kernel.OpenProcess(0x100001, False, pid)
        self.assertTrue(handle, f"Cannot track real process {pid}")

        def cleanup():
            if self.kernel.WaitForSingleObject(handle, 0) == 0x102:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 5000)
            self.kernel.CloseHandle(handle)
        self.addCleanup(cleanup)
        return handle

    def tree_handles(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            files = list(self.directory.glob("*.pid"))
            if len(files) == 3 and all(file.read_text() for file in files):
                return [self.open_process(int(file.read_text())) for file in files]
            time.sleep(0.02)
        self.fail("Real three-level process tree failed to start")

    def assert_exited(self, handles):
        for handle in handles:
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)

    def execute(self, args, **kwargs):
        with (self.directory / "out").open("wb") as out, (self.directory / "err").open("wb") as err:
            return run_tool([sys.executable, *args], stdout=out, stderr=err, **kwargs)

    def launch_tree(self, mode="root", **kwargs):
        errors = []

        def execute():
            try:
                self.execute([FIXTURE, mode, str(self.directory)], **kwargs)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        self.addCleanup(lambda: thread.join(12))
        return thread, errors

    def test_timeout_reaps_all_three_levels(self):
        thread, errors = self.launch_tree(timeout=3)
        handles = self.tree_handles()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], subprocess.TimeoutExpired)
        self.assert_exited(handles)

    def test_cancellation_waits_for_all_descendants(self):
        cancel = threading.Event()
        thread, errors = self.launch_tree(timeout=30, cancel=cancel)
        handles = self.tree_handles()
        cancel.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ProcessCancelled)
        self.assert_exited(handles)

    def test_killing_owner_closes_job_and_kills_descendants(self):
        host = subprocess.Popen([sys.executable, FIXTURE, "host", str(self.directory)])
        self.addCleanup(lambda: host.wait(timeout=10))
        self.addCleanup(lambda: host.kill() if host.poll() is None else None)
        handles = self.tree_handles()
        host.kill()
        host.wait(timeout=10)
        self.assert_exited(handles)

    def test_successful_root_cannot_leave_background_descendants(self):
        thread, errors = self.launch_tree(mode="root-exit", timeout=15)
        handles = self.tree_handles()
        (self.directory / "release-root").touch()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assert_exited(handles)

    def test_quoted_unicode_arguments_and_large_file_backed_logs(self):
        argument = '中文 路径 "quoted" trailing\\'
        self.execute(["-c", "import sys; sys.stdout.buffer.write(sys.argv[1].encode('utf-8')+b'\\n'); "
                      "sys.stdout.buffer.write(b'x'*1000000); sys.stderr.write('e'*1000000)", argument], timeout=10)
        self.assertEqual((self.directory / "out").read_bytes().split(b"\n", 1)[0], argument.encode("utf-8"))
        self.assertEqual((self.directory / "err").stat().st_size, 1000000)
        self.assertGreater((self.directory / "out").stat().st_size, 1000000)

    def test_nonzero_exit_and_creation_failure_propagate(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(["-c", "raise SystemExit(7)"], timeout=10)
        self.assertEqual(error.exception.returncode, 7)
        fake = self.directory / "not-executable.exe"
        fake.write_bytes(b"invalid executable")
        with (self.directory / "out").open("wb") as out, (self.directory / "err").open("wb") as err:
            with self.assertRaises(OSError):
                run_tool([str(fake)], stdout=out, stderr=err, timeout=10)

    def test_pre_cancel_and_invalid_timeout_do_not_launch(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProcessCancelled):
            self.execute([FIXTURE, "root", str(self.directory)], timeout=10, cancel=cancel)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.execute([FIXTURE, "root", str(self.directory)], timeout=timeout)
        self.assertEqual(list(self.directory.glob("*.pid")), [])

    def test_containment_setup_failure_never_launches_unprotected_process(self):
        from app import _windows_process
        with patch.object(_windows_process, "update", return_value=0), \
                patch.object(_windows_process, "create", wraps=_windows_process.create) as create:
            with self.assertRaises(OSError):
                self.execute([FIXTURE, "root", str(self.directory)], timeout=10)
            create.assert_not_called()
        self.assertEqual(list(self.directory.glob("*.pid")), [])

    def test_repeated_success_and_setup_failure_release_native_handles(self):
        # Isolate from other tests' executor/GC cleanup changing this process's
        # total handle count while the loop runs (including decreases).
        result = subprocess.run([sys.executable, FIXTURE, "handle-count", str(self.directory)],
                                capture_output=True, check=True, timeout=30)
        counts = json.loads(result.stdout)
        self.assertEqual(counts["after"], counts["before"])
