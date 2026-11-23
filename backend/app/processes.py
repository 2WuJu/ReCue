"""Bounded tool execution using Windows jobs or supervised macOS process groups."""
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar

_outer_job = ContextVar("outer_tool_job", default=None)


class ProcessCleanupError(RuntimeError):
    """Fatal containment failure: callers must stop claiming work."""


@contextmanager
def tool_scope(handle):
    token = _outer_job.set(handle)
    try:
        yield
    finally:
        _outer_job.reset(token)


class ProcessCancelled(RuntimeError):
    pass


def tool_log_tail(path):
    with path.open("rb") as stream:
        stream.seek(max(0, os.fstat(stream.fileno()).st_size - 4096))
        return stream.read(4096)


def run_tool(command, *, stdout, stderr, timeout, cancel=None, report=None,
             measure_memory=False, report_interval=None):
    """Run an argument array with file-backed output, never a shell or a pipe.

    cancel is an optional threading.Event. Return only after the entire job is
    empty, including descendants left behind by a successfully exited root.
    Supported on Windows 10+ and macOS.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Tool timeout must be finite and positive")
    if cancel is not None and cancel.is_set():
        raise ProcessCancelled("Local processing cancelled")
    executable = Path(command[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Tool executable must be an existing absolute file")
    if sys.platform == "darwin":
        from app._macos_process import run

        interval = report_interval if report_interval is not None else (.1 if measure_memory else 1)
        return run(command, stdout, stderr, timeout, cancel, report, _outer_job.get(),
                   measure_memory=measure_memory, report_interval=interval)
    if os.name != "nt":
        raise OSError("Contained tool execution requires Windows 10+ or macOS")
    from app._windows_process import ToolJob

    deadline = time.monotonic() + timeout
    with ToolJob(command, stdout, stderr, outer_job=_outer_job.get()) as job:
        sampled_at, previous_cpu = time.monotonic(), 0
        while True:
            now = time.monotonic()
            if report and now - sampled_at >= 1:
                cpu_seconds, peak_memory = job.resources()
                percent = 100 * (cpu_seconds - previous_cpu) / ((now - sampled_at) * (os.cpu_count() or 1))
                resident = job.resident_memory() if measure_memory else None
                report(min(100, max(0, percent)), peak_memory or None, resident)
                sampled_at, previous_cpu = now, cpu_seconds
            if cancel is not None and cancel.is_set():
                raise ProcessCancelled("Local processing cancelled")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            code = job.wait(50)
            if code is not None:
                if measure_memory and report:
                    report(None, job.resources()[1] or None, None)
                if code:
                    raise subprocess.CalledProcessError(code, command)
                return
