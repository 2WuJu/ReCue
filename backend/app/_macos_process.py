"""A separate process owns each tool group and watches its caller via pipe EOF."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def sample_tree(parent, previous):
    import psutil

    rss = cpu = 0
    samples = {}
    try:
        children = psutil.Process(parent).children(recursive=True)
    except psutil.Error:
        return None, None, previous
    for child in children:
        try:
            with child.oneshot():
                times = child.cpu_times()
                use = times.user + times.system
                identity = (child.pid, child.create_time())
                memory = child.memory_info().rss
        except psutil.Error:
            continue
        cpu += max(0, use - previous.get(identity, 0))
        rss += memory
        samples[identity] = use
    return (cpu, rss / 1024 ** 2, samples) if samples else (None, None, previous)


def run(command, stdout, stderr, timeout, cancel, report, marker, *, measure_memory, report_interval):
    from app.processes import ProcessCancelled, ProcessCleanupError

    if marker is not None:
        try:
            with marker.open("x") as stream:
                stream.write("macOS tool cleanup pending\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise ProcessCleanupError("Cannot record pending tool cleanup") from exc
    deadline = time.monotonic() + timeout
    try:
        guardian = subprocess.Popen(
            [sys.executable, __file__, str(marker or ""), str(stdout.fileno()),
             str(stderr.fileno()), *command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            pass_fds=(stdout.fileno(), stderr.fileno()), start_new_session=True,
        )
    except OSError:
        if marker is not None:
            marker.unlink()
        raise
    with guardian.stdin, guardian.stdout:
        try:
            reported_at = time.monotonic()
            next_sample = reported_at
            peak = current = None
            cpu, previous = 0, {}
            has_cpu = False
            while guardian.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise ProcessCancelled("Local processing cancelled")
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                if report and measure_memory and now >= next_sample:
                    elapsed_cpu, current, previous = sample_tree(guardian.pid, previous)
                    if current is not None:
                        peak = max(peak or 0, current)
                    if elapsed_cpu is not None:
                        cpu += elapsed_cpu
                        has_cpu = True
                    next_sample = now + .1
                if report and now - reported_at >= report_interval:
                    percent = 100 * cpu / ((now - reported_at) * (os.cpu_count() or 1))
                    report(min(100, percent) if has_cpu else None, peak, current)
                    reported_at, cpu, has_cpu = now, 0, False
                time.sleep(min(remaining, .05))
        finally:
            guardian.stdin.close()
            try:
                code = guardian.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise ProcessCleanupError("macOS cleanup guardian did not finish") from exc
            if code or (marker is not None and marker.exists()):
                raise ProcessCleanupError("macOS tool cleanup is unconfirmed; inspect the tool log")
        result = json.load(guardian.stdout)
    if report and measure_memory and peak is not None:
        report(None, peak, None)
    if "error" in result:
        raise OSError(result["error"])
    if result["returncode"]:
        raise subprocess.CalledProcessError(result["returncode"], command)


def drain(tool):
    # Keep the leader unreaped until the first signal so its group ID can't
    # be recycled while the tool exits and we clean up any surviving children.
    try:
        os.killpg(tool.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Darwin returns EPERM when the group only contains zombies. The
        # subsequent wait and group check still have to confirm cleanup.
        pass
    code = tool.wait(timeout=5)
    deadline = time.monotonic() + 4
    while True:
        try:
            os.killpg(tool.pid, 0)
        except ProcessLookupError:
            return code
        except PermissionError:
            pass  # Wait for reparented zombies to be reaped as well.
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Tool process group {tool.pid} did not exit")
        time.sleep(.02)


def guard(marker, stdout, stderr, command):
    with closing(select.kqueue()) as events:
        owner = select.kevent(0, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD)
        # The caller may have died before the guardian finished starting.
        if events.control([owner], 1, 0):
            result = {"returncode": -signal.SIGKILL}
        else:
            try:
                tool = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=stderr, start_new_session=True)
            except OSError as exc:
                result = {"error": str(exc)[:1000]}
            else:
                try:
                    if marker:
                        marker.write_text(f"macOS tool process group {tool.pid}\n")
                    exited = select.kevent(tool.pid, filter=select.KQ_FILTER_PROC,
                                           flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
                    events.control([exited], 1, None)
                finally:
                    code = drain(tool)
                result = {"returncode": code}
    if marker:
        marker.unlink()
    try:
        print(json.dumps(result), flush=True)
    except BrokenPipeError:
        pass  # The worker may have been killed while cleanup was in progress.


if __name__ == "__main__":
    marker, stdout, stderr, *command = sys.argv[1:]
    guard(Path(marker) if marker else None, int(stdout), int(stderr), command)
