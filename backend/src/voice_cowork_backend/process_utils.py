"""Shared subprocess-management primitives — used by cli.py (for the
backend + tunnel children) and opencode_process.py (for the opencode-
compatible server, whose lifecycle moved from the CLI into the backend
so a web request can control it — see PROGRESS.md's runtime AI-tool-
switcher entry).
"""

import queue
import subprocess
import sys
import threading

import structlog

log = structlog.get_logger()


class WindowsJobObject:
    """A Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    Every child process assigned to this job is forcibly terminated by the
    OS the instant the job's last handle closes — including when the
    owning process itself dies by any means (crash, force-kill, an
    unhandled exception, an external supervisor). A Python `finally` block
    (see ManagedProcess.terminate() / cli.py's start()) only runs cleanup
    if the interpreter is still alive to reach it; a hard kill bypasses it
    entirely, which is exactly what left real orphaned processes running
    after a simulated crash earlier in this project (see PROGRESS.md). The
    Job Object closes this gap at the OS level instead of relying on
    Python code running at all.

    No-op (assign() does nothing) on non-Windows platforms — there's no
    portable equivalent, and this project currently only runs on Windows.
    """

    def __init__(self) -> None:
        self._handle = None
        if sys.platform != "win32":
            log.warning(
                "job_object_unsupported",
                note="Not on Windows — child processes won't be auto-killed if the owning process is force-killed.",
            )
            return

        import ctypes
        import ctypes.wintypes as wintypes

        # Real Win32 struct layouts (see JOBOBJECT_BASIC_LIMIT_INFORMATION /
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION in winnt.h) — declared field-
        # by-field rather than as a raw byte buffer with hand-computed
        # offsets, since getting an offset wrong would silently write
        # LimitFlags into the wrong place and leave KILL_ON_JOB_CLOSE unset
        # with no error raised anywhere.
        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        self._kernel32 = kernel32

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError()

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        ok = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise ctypes.WinError()

        self._handle = handle
        log.info("job_object_created")

    def assign(self, proc: subprocess.Popen) -> None:
        if self._handle is None:
            return
        ok = self._kernel32.AssignProcessToJobObject(self._handle, int(proc._handle))
        if not ok:
            import ctypes

            raise ctypes.WinError()


class ManagedProcess:
    def __init__(self, args: list[str], job: "WindowsJobObject | None" = None) -> None:
        self.proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        if job is not None:
            job.assign(self.proc)
        self.lines: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in iter(self.proc.stdout.readline, ""):
            self._queue.put(line.rstrip("\n"))
        self.proc.stdout.close()

    def drain(self) -> list[str]:
        new_lines = []
        while not self._queue.empty():
            line = self._queue.get_nowait()
            self.lines.append(line)
            new_lines.append(line)
        return new_lines

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def tail(self, n: int = 20) -> str:
        return "\n".join(self.lines[-n:])

    def terminate(self) -> None:
        if not self.is_running():
            return
        if sys.platform == "win32":
            # taskkill /T kills the entire process tree rooted at this
            # PID, not just the directly-spawned process — necessary
            # because some "binaries" are actually wrapper scripts that
            # spawn further children (confirmed by testing: mimocode's
            # .cmd -> node.exe -> a native mimo.exe server survived a
            # plain .terminate() as an orphan still bound to the port).
            # Falls through to the plain terminate()/kill() below only if
            # taskkill itself fails.
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                capture_output=True,
            )
            if result.returncode == 0:
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
