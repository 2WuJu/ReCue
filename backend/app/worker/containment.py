"""Retained outer Job handle bridges worker death and restart recovery."""
from contextlib import contextmanager
import ctypes as c
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from app.processes import ProcessCleanupError, tool_scope


def await_cleanup(runtime, timeout=15):
    deadline = time.monotonic() + timeout
    marker = runtime / "processing.guard"
    while marker.exists():
        if time.monotonic() >= deadline:
            raise ProcessCleanupError("Previous tool cleanup is unconfirmed; inspect runtime/guardian.log")
        time.sleep(0.05)


@contextmanager
def containment(runtime):
    from app import _windows_process as win
    name = "Local\\ReCue-" + str(uuid4())
    marker = runtime / "processing.guard"
    ready = runtime / "guardian.ready"
    done = runtime / "guardian.done"
    ready.unlink(missing_ok=True)
    done.unlink(missing_ok=True)
    # Durable fail-closed marker precedes any external tool creation. Only the
    # guardian clears it after a successful drain, including on owner hard kill.
    with marker.open("x") as stream:
        stream.write(name)
        stream.flush()
        os.fsync(stream.fileno())
    job = win.checked(win.create_job(None, name))
    guardian = None
    try:
        limits = win.ExtendedLimits()
        limits.basic.flags = 0x2000
        win.checked(win.set_limits(job, 9, c.byref(limits), c.sizeof(limits)))
        with (runtime / "guardian.log").open("ab") as log:
            guardian = subprocess.Popen(
                [sys.executable, "-m", "app.worker.containment", str(os.getpid()), str(runtime), name],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                cwd=Path(__file__).resolve().parents[2],
                creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 10
        while not ready.exists():
            if guardian.poll() is not None or time.monotonic() >= deadline:
                raise ProcessCleanupError("Cleanup guardian failed to become ready")
            time.sleep(.02)
        with tool_scope(job):
            yield guardian
    finally:
        # The guardian holds a second handle and drains explicitly, rather than
        # relying on asynchronous KILL_ON_JOB_CLOSE after worker.lock releases.
        done.touch()
        win.close(job)
        if guardian is not None:
            try:
                code = guardian.wait(timeout=15)
            except subprocess.TimeoutExpired as exc:
                raise ProcessCleanupError("Cleanup guardian did not finish") from exc
            if code or marker.exists():
                raise ProcessCleanupError("Cleanup guardian failed; recovery remains blocked")


def guard(parent_pid, runtime, name):
    from app import _windows_process as win
    open_process = win.bind("OpenProcess", win.HANDLE, win.w.DWORD, win.w.BOOL, win.w.DWORD)
    parent = win.checked(open_process(0x100000, False, parent_pid))  # SYNCHRONIZE
    job = None
    try:
        job = win.checked(win.open_job(0x0004 | 0x0008, False, name))
        (runtime / "guardian.ready").touch()
        while not (runtime / "guardian.done").exists():
            result = win.wait(parent, 50)
            if result == 0:
                break
            if result != 0x102:
                raise c.WinError(c.get_last_error())
        win.drain_job(job)
        (runtime / "guardian.ready").unlink(missing_ok=True)
        (runtime / "guardian.done").unlink(missing_ok=True)
        (runtime / "processing.guard").unlink()
    finally:
        if job:
            win.close(job)
        win.close(parent)


if __name__ == "__main__":
    guard(int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
