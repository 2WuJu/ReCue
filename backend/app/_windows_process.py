"""Private Win32 bindings for processes.run_tool (Windows 10+)."""
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
import subprocess
import time
from app.processes import ProcessCleanupError

SIZE = c.c_size_t
HANDLE = w.HANDLE
kernel = c.WinDLL("kernel32", use_last_error=True)


class BasicLimits(c.Structure):
    _fields_ = [("per_process_time", c.c_longlong), ("per_job_time", c.c_longlong),
                ("flags", w.DWORD), ("min_working_set", SIZE), ("max_working_set", SIZE),
                ("active_limit", w.DWORD), ("affinity", SIZE),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class IO(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(c.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IO), ("process_memory", SIZE),
                ("job_memory", SIZE), ("peak_process_memory", SIZE), ("peak_job_memory", SIZE)]


class Accounting(c.Structure):
    _fields_ = [(name, c.c_longlong) for name in
                ("user_time", "kernel_time", "period_user_time", "period_kernel_time")] + [
                (name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]


class Startup(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR)] + [(name, w.DWORD) for name in
                ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")] + [
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_ptr", c.c_void_p),
                ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE)]


class StartupEx(c.Structure):
    _fields_ = [("startup", Startup), ("attributes", c.c_void_p)]


class ProcessInfo(c.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def bind(name, result, *args):
    function = getattr(kernel, name)
    function.restype, function.argtypes = result, args
    return function


create_job = bind("CreateJobObjectW", HANDLE, c.c_void_p, w.LPCWSTR)
open_job = bind("OpenJobObjectW", HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
close = bind("CloseHandle", w.BOOL, HANDLE)
set_limits = bind("SetInformationJobObject", w.BOOL, HANDLE, c.c_int, c.c_void_p, w.DWORD)
query = bind("QueryInformationJobObject", w.BOOL, HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p)
terminate = bind("TerminateJobObject", w.BOOL, HANDLE, w.UINT)
initialize = bind("InitializeProcThreadAttributeList", w.BOOL, c.c_void_p, w.DWORD, w.DWORD, c.POINTER(SIZE))
update = bind("UpdateProcThreadAttribute", w.BOOL, c.c_void_p, w.DWORD, SIZE, c.c_void_p, SIZE,
              c.c_void_p, c.c_void_p)
delete = bind("DeleteProcThreadAttributeList", None, c.c_void_p)
create = bind("CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p,
              w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(StartupEx), c.POINTER(ProcessInfo))
current = bind("GetCurrentProcess", HANDLE)
duplicate = bind("DuplicateHandle", w.BOOL, HANDLE, HANDLE, HANDLE, c.POINTER(HANDLE), w.DWORD,
                 w.BOOL, w.DWORD)
wait = bind("WaitForSingleObject", w.DWORD, HANDLE, w.DWORD)
exit_code = bind("GetExitCodeProcess", w.BOOL, HANDLE, c.POINTER(w.DWORD))


def checked(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def drain_job(handle):
    try:
        checked(terminate(handle, 1))
        deadline = time.monotonic() + 10
        while True:
            accounting = Accounting()
            checked(query(handle, 1, c.byref(accounting), c.sizeof(accounting), None))
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Tool process tree did not exit within 10 seconds")
            time.sleep(0.01)
    except Exception as exc:
        raise ProcessCleanupError("Cannot confirm tool process tree exit") from exc


class ToolJob:
    def __init__(self, command, stdout, stderr, outer_job=None):
        self.job = None
        self.process = None
        try:
            self.job = checked(create_job(None, None))
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
            checked(set_limits(self.job, 9, c.byref(limits), c.sizeof(limits)))
            self._spawn(command, stdout, stderr, outer_job)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _spawn(self, command, stdout, stderr, outer_job):
        inherited = []
        attributes = None
        initialized = False
        try:
            with open(os.devnull, "rb") as stdin:
                for file in (stdin, stdout, stderr):
                    handle = HANDLE()
                    checked(duplicate(current(), msvcrt.get_osfhandle(file.fileno()), current(),
                                      c.byref(handle), 0, True, 2))  # DUPLICATE_SAME_ACCESS
                    inherited.append(handle.value)
            size = SIZE()
            initialize(None, 2, 0, c.byref(size))  # Expected insufficient-buffer response
            attributes = c.create_string_buffer(size.value)
            checked(initialize(attributes, 2, 0, c.byref(size)))
            initialized = True
            handles = (HANDLE * 3)(*inherited)
            job_handles = [outer_job, self.job] if outer_job else [self.job]
            jobs = (HANDLE * len(job_handles))(*job_handles)
            checked(update(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None))
            checked(update(attributes, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
            startup = StartupEx()
            startup.startup.cb = c.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = inherited
            startup.attributes = c.cast(attributes, c.c_void_p)
            info = ProcessInfo()
            # No shell; job assignment is atomic with creation, before user code runs.
            checked(create(str(command[0]), c.create_unicode_buffer(subprocess.list2cmdline(command)),
                           None, None, True, 0x80000 | 0x8000000, None, None,
                           c.byref(startup), c.byref(info)))  # EXTENDED_STARTUPINFO / NO_WINDOW
            self.process = info.process
            close(info.thread)
        finally:
            if initialized:
                delete(attributes)
            for handle in inherited:
                close(handle)

    def wait(self, milliseconds):
        result = wait(self.process, milliseconds)
        if result == 0x102:  # WAIT_TIMEOUT
            return None
        if result != 0:
            raise c.WinError(c.get_last_error())
        code = w.DWORD()
        checked(exit_code(self.process, c.byref(code)))
        return code.value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.job:
                drain_job(self.job)
        finally:
            if self.process:
                close(self.process)
                self.process = None
            if self.job:
                close(self.job)
                self.job = None
