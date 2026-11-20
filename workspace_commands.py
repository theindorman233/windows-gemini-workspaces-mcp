"""Controlled PowerShell command execution and background job management for live workspaces."""

import base64
import codecs
from collections import OrderedDict, deque
import ctypes
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time

from live_workspaces import DRIVE_PATH, live_target
from workspace_files import WORKSPACES


PWSH = Path(shutil.which("pwsh") or r"C:\Program Files\PowerShell\7\pwsh.exe")
MAX_COMMAND_CHARS = 16000
MAX_CAPTURE_CHARS = 32768
MAX_RETURN_CHARS = 8000
HEAD_RETURN_CHARS = 2000
TAIL_RETURN_CHARS = MAX_RETURN_CHARS - HEAD_RETURN_CHARS
_HEAD_CAPTURE_LIMIT = 8192
MAX_RETAINED_JOBS = 32
MAX_RUNNING_JOBS = 8
SYNC_WAIT_CAP_SECONDS = 25
MAX_COMMAND_TIMEOUT_SECONDS = 600
MAX_JOB_WAIT_SECONDS = 20
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SENSITIVE_ENV_TOKENS = ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
)

if os.name == "nt":
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
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

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:
    _kernel32 = None


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return upper.startswith("MCP_PROBE_") or any(token in upper for token in _SENSITIVE_ENV_TOKENS)


def _build_command_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not _is_sensitive_env_key(key)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    env["FORCE_COLOR"] = "0"
    env["TERM"] = "dumb"
    return env


def _create_and_assign_job_object(process: subprocess.Popen) -> int | None:
    if os.name != "nt" or _kernel32 is None:
        return None
    proc_handle = getattr(process, "_handle", None)
    if not proc_handle:
        return None
    h_job = None
    try:
        h_job = _kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            h_job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(h_job)
            return None
        assigned = _kernel32.AssignProcessToJobObject(h_job, wintypes.HANDLE(int(proc_handle)))
        if not assigned:
            _kernel32.CloseHandle(h_job)
            return None
        return int(h_job)
    except Exception:
        if h_job:
            try:
                _kernel32.CloseHandle(h_job)
            except Exception:
                pass
        return None


def _close_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or _kernel32 is None or not job_handle:
        return
    try:
        _kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except Exception:
        pass


def _terminate_and_close_job_object(job_handle: int | None) -> bool:
    if os.name != "nt" or _kernel32 is None or not job_handle:
        return False
    terminated = False
    try:
        terminated = bool(_kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1))
    except Exception:
        terminated = False
    finally:
        _close_job_object(job_handle)
    return terminated


@dataclass
class StreamCapture:
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )
    _head_chunks: list[str] = field(default_factory=list)
    _head_len: int = 0
    _tail_chunks: deque[str] = field(default_factory=deque)
    _tail_len: int = 0
    total_chars: int = 0
    truncated: bool = False

    def _push_piece(self, piece: str) -> None:
        if not piece:
            return
        self.total_chars += len(piece)
        if self._head_len < _HEAD_CAPTURE_LIMIT:
            need = _HEAD_CAPTURE_LIMIT - self._head_len
            head_part = piece[:need]
            self._head_chunks.append(head_part)
            self._head_len += len(head_part)
        self._tail_chunks.append(piece)
        self._tail_len += len(piece)
        while self._tail_len > MAX_CAPTURE_CHARS and self._tail_chunks:
            first_len = len(self._tail_chunks[0])
            if self._tail_len - first_len >= MAX_CAPTURE_CHARS:
                self._tail_chunks.popleft()
                self._tail_len -= first_len
                self.truncated = True
            elif len(self._tail_chunks) == 1:
                excess = self._tail_len - MAX_CAPTURE_CHARS
                self._tail_chunks[0] = self._tail_chunks[0][excess:]
                self._tail_len -= excess
                self.truncated = True
                break
            else:
                break

    def append(self, chunk: bytes) -> None:
        self._push_piece(self.decoder.decode(chunk, final=False))

    def finish(self) -> None:
        self._push_piece(self.decoder.decode(b"", final=True))

    def text(self) -> tuple[str, bool]:
        if not self.truncated:
            full = strip_ansi("".join(self._tail_chunks))
            if len(full) <= MAX_RETURN_CHARS:
                return full, False
            head = full[:HEAD_RETURN_CHARS]
            tail = full[-TAIL_RETURN_CHARS:] if TAIL_RETURN_CHARS > 0 else ""
            dropped = len(full) - len(head) - len(tail)
            return f"{head}\n...[truncated {dropped} chars]...\n{tail}", True
        head = strip_ansi("".join(self._head_chunks))[:HEAD_RETURN_CHARS]
        tail_clean = strip_ansi("".join(self._tail_chunks))
        tail = tail_clean[-TAIL_RETURN_CHARS:] if TAIL_RETURN_CHARS > 0 else ""
        dropped = max(0, self.total_chars - len(head) - len(tail))
        return f"{head}\n...[truncated {dropped} chars]...\n{tail}", True


@dataclass
class CommandJob:
    job_id: str
    cwd: Path
    process: subprocess.Popen
    timeout_seconds: int
    stdout: StreamCapture = field(default_factory=StreamCapture)
    stderr: StreamCapture = field(default_factory=StreamCapture)
    status: str = "running"
    returncode: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    timer: threading.Timer | None = None
    readers: list[threading.Thread] = field(default_factory=list)
    job_handle: int | None = None


_JOBS: OrderedDict[str, CommandJob] = OrderedDict()
_PENDING_STARTS = 0
_JOBS_LOCK = threading.Lock()


def valid_int_range(value: int, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def sanitize_error(error: Exception) -> str:
    return DRIVE_PATH.sub("<path>", str(error))[:300]


def resolve_command_cwd(store: Path, workspace: str, directory: str = "") -> Path:
    if workspace not in WORKSPACES:
        raise ValueError("unknown workspace")
    prefix = f"{workspace}/{directory}" if directory else workspace
    try:
        folder = live_target(store, f"{prefix}/cmd-scope-check.txt").parent
    except OSError:
        raise ValueError("workspace directory is unavailable") from None
    if not folder.is_dir():
        raise ValueError("workspace directory is unavailable")
    return folder


def encode_pwsh_command(command: str) -> str:
    payload_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    script = (
        "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f"$__c = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload_b64}'))\n"
        "$__errs = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseInput($__c, [ref]$null, [ref]$__errs)\n"
        "if ($__errs.Count -gt 0) { [Console]::Error.WriteLine($__errs[0].Message); exit 1 }\n"
        "$global:LASTEXITCODE = 0\n"
        "$__w = $__c + \"`nif (-not `$?) { if (`$LASTEXITCODE) { exit `$LASTEXITCODE } else { exit 1 } }`nexit 0\"\n"
        "& ([scriptblock]::Create($__w))"
    )
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def terminate_process_tree(
    pid: int,
    *,
    job_handle: int | None = None,
    process: subprocess.Popen | None = None,
) -> None:
    terminated_by_job = False
    if job_handle is not None:
        terminated_by_job = _terminate_and_close_job_object(job_handle)
    if pid == os.getpid() or pid <= 0:
        return
    if terminated_by_job:
        return
    try:
        if os.name == "nt":
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode != 0 and process is not None and process.poll() is None:
                process.kill()
        else:
            os.kill(pid, 9)
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass


def _drain_stream(stream, capture: StreamCapture, job_lock: threading.Lock) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            with job_lock:
                capture.append(chunk)
    finally:
        with job_lock:
            capture.finish()
        try:
            stream.close()
        except OSError:
            pass


def _finalize_job(job: CommandJob) -> None:
    code = job.process.wait()
    with job.lock:
        if job.timer is not None:
            job.timer.cancel()
        job.returncode = code
        if job.status == "running":
            job.status = "completing"
        job_handle = job.job_handle
        job.job_handle = None
    _close_job_object(job_handle)
    for reader in job.readers:
        reader.join(timeout=1.5)
    with job.lock:
        if job.status == "completing":
            job.status = "completed"
    job.done.set()


def _expire_job(job: CommandJob) -> None:
    with job.lock:
        if job.status != "running":
            return
        job.status = "timed_out"
        job_handle = job.job_handle
        job.job_handle = None
    terminate_process_tree(job.process.pid, job_handle=job_handle, process=job.process)


def _prune_finished_jobs_locked() -> None:
    while len(_JOBS) >= MAX_RETAINED_JOBS:
        evict_key = next((key for key, item in _JOBS.items() if item.done.is_set()), None)
        if evict_key is None:
            break
        _JOBS.pop(evict_key, None)


def _snapshot_job(job: CommandJob, *, note: str = "") -> dict:
    with job.lock:
        stdout_text, out_trunc = job.stdout.text()
        stderr_text, err_trunc = job.stderr.text()
        status = "running" if job.status == "completing" else job.status
        returncode = job.returncode
    ok = status == "running" or (status == "completed" and returncode == 0)
    payload = {
        "ok": ok,
        "job_id": job.job_id,
        "status": status,
        "returncode": returncode,
        "cwd": str(job.cwd),
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": out_trunc or err_trunc,
    }
    if status == "timed_out":
        payload["error"] = f"command timed out after {job.timeout_seconds}s"
    if note:
        payload["note"] = note
    return payload


def run_workspace_command(store: Path, workspace: str, command: str, directory: str = "",
                          timeout_seconds: int = 20, background: bool = False) -> dict:
    """Execute a PowerShell command inside one workspace directory, synchronously or as a job."""
    global _PENDING_STARTS
    if not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return {"ok": False, "error": f"provide a non-empty command of at most {MAX_COMMAND_CHARS} characters"}
    if not valid_int_range(timeout_seconds, 1, MAX_COMMAND_TIMEOUT_SECONDS):
        return {"ok": False, "error": f"timeout_seconds must be an integer between 1 and {MAX_COMMAND_TIMEOUT_SECONDS}"}
    if isinstance(background, bool) is False:
        return {"ok": False, "error": "background must be a boolean"}
    if not PWSH.is_file():
        return {"ok": False, "error": "pwsh executable is unavailable"}
    try:
        cwd = resolve_command_cwd(store, workspace, directory)
        encoded = encode_pwsh_command(command)
    except UnicodeEncodeError:
        return {"ok": False, "error": "command contains invalid Unicode surrogates"}
    except ValueError as error:
        return {"ok": False, "error": sanitize_error(error)}

    env = _build_command_env()
    job_id = f"cmd-{int(time.time())}-{secrets.token_hex(4)}"

    with _JOBS_LOCK:
        running = sum(1 for item in _JOBS.values() if not item.done.is_set())
        if running + _PENDING_STARTS >= MAX_RUNNING_JOBS:
            return {"ok": False, "error": f"too many running background jobs (max {MAX_RUNNING_JOBS}); stop or wait for one first"}
        _PENDING_STARTS += 1
        reserved = True

    try:
        try:
            process = subprocess.Popen(
                [str(PWSH), "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as error:
            return {"ok": False, "error": f"failed to start command: {sanitize_error(error)}"}

        job_handle = _create_and_assign_job_object(process)
        job = CommandJob(
            job_id=job_id,
            cwd=cwd,
            process=process,
            timeout_seconds=timeout_seconds,
            job_handle=job_handle,
        )
        out_thread = threading.Thread(target=_drain_stream, args=(process.stdout, job.stdout, job.lock), daemon=True)
        err_thread = threading.Thread(target=_drain_stream, args=(process.stderr, job.stderr, job.lock), daemon=True)
        job.readers = [out_thread, err_thread]
        out_thread.start()
        err_thread.start()

        timer = threading.Timer(timeout_seconds, _expire_job, args=(job,))
        timer.daemon = True
        job.timer = timer
        timer.start()

        wait_thread = threading.Thread(target=_finalize_job, args=(job,), daemon=True)
        wait_thread.start()

        with _JOBS_LOCK:
            _prune_finished_jobs_locked()
            _JOBS[job_id] = job
            _PENDING_STARTS -= 1
            reserved = False
    finally:
        if reserved:
            with _JOBS_LOCK:
                _PENDING_STARTS -= 1

    if background:
        return _snapshot_job(job, note="started in background; poll with read_command_job")

    wait_cap = min(timeout_seconds, SYNC_WAIT_CAP_SECONDS)
    finished = job.done.wait(timeout=wait_cap)
    if not finished and timeout_seconds > SYNC_WAIT_CAP_SECONDS:
        return _snapshot_job(job, note="still running in background; poll with read_command_job")
    if not finished:
        _expire_job(job)
        job.done.wait(timeout=5)
    return _snapshot_job(job)


def read_command_job(job_id: str, wait_seconds: int = 0) -> dict:
    """Wait optionally up to 20s and return current status and output of a command job."""
    if not valid_int_range(wait_seconds, 0, MAX_JOB_WAIT_SECONDS):
        return {"ok": False, "error": f"wait_seconds must be an integer between 0 and {MAX_JOB_WAIT_SECONDS}"}
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "unknown command job ID"}
    if wait_seconds > 0 and not job.done.is_set():
        job.done.wait(timeout=wait_seconds)
    return _snapshot_job(job)


def stop_command_job(job_id: str) -> dict:
    """Terminate a running command job tree and return its captured output."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "unknown command job ID"}
    job_handle = None
    with job.lock:
        was_running = job.status == "running"
        if was_running:
            job.status = "stopped"
            job_handle = job.job_handle
            job.job_handle = None
    if was_running:
        terminate_process_tree(job.process.pid, job_handle=job_handle, process=job.process)
    if not job.done.is_set():
        job.done.wait(timeout=5)
    return _snapshot_job(job)
