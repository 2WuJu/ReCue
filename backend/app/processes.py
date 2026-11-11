"""Bounded Windows tool execution. Every child belongs to a kill-on-close job.

Creation-time JOB_LIST assignment closes the orphan window between spawning a
process and assigning it to a job. Only the three standard handles are inherited;
in particular, children cannot keep the job alive after their owner is killed.
"""
import math
import os
from pathlib import Path
import subprocess
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


def run_tool(command, *, stdout, stderr, timeout, cancel=None):
    """Run an argument array with file-backed output, never a shell or a pipe.

    cancel is an optional threading.Event. Return only after the entire job is
    empty, including descendants left behind by a successfully exited root.
    Windows 10+ is required; never fall back to uncontained execution.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Tool timeout must be finite and positive")
    if cancel is not None and cancel.is_set():
        raise ProcessCancelled("Local processing cancelled")
    if os.name != "nt":
        raise OSError("Contained tool execution requires Windows 10 or newer")
    executable = Path(command[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Tool executable must be an existing absolute file")
    from app._windows_process import ToolJob

    deadline = time.monotonic() + timeout
    with ToolJob(command, stdout, stderr, outer_job=_outer_job.get()) as job:
        while True:
            if cancel is not None and cancel.is_set():
                raise ProcessCancelled("Local processing cancelled")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            code = job.wait(50)
            if code is not None:
                if code:
                    raise subprocess.CalledProcessError(code, command)
                return
