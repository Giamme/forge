"""Cross-platform process, signal, and bash resolution adapters for forge.

Keeps POSIX semantics (process groups, signals, /bin/bash, exec) unchanged on
macOS/Linux while providing pure-stdlib Windows equivalents using Win32 kernel32
APIs via ctypes.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

IS_WINDOWS = os.name == 'nt'

SIGKILL = getattr(signal, 'SIGKILL', signal.SIGTERM)
SIGUSR1 = getattr(signal, 'SIGUSR1', None)
SIGQUIT = getattr(signal, 'SIGQUIT', None)

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_ACCESS_DENIED = 5
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


def _is_windows_bash_stub(path: str) -> bool:
    low = path.replace('/', '\\').lower()
    return '\\system32\\bash.exe' in low or '\\syswow64\\bash.exe' in low or '\\windowsapps\\' in low


def bash_executable() -> str:
    """Path to GNU bash (/bin/bash on POSIX; Git Bash avoiding WSL stub on Windows)."""
    if not IS_WINDOWS:
        if os.path.exists('/bin/bash'):
            return '/bin/bash'
        return shutil.which('bash') or '/bin/bash'

    for env_key in ('FORGE_BASH', 'BASH'):
        cand = os.environ.get(env_key, '').strip()
        if cand and os.path.isfile(cand) and not _is_windows_bash_stub(cand):
            return cand

    which_bash = shutil.which('bash')
    if which_bash and not _is_windows_bash_stub(which_bash):
        if which_bash.replace('/', '\\').lower().endswith('\\usr\\bin\\bash.exe'):
            git_bin = Path(which_bash).resolve().parents[2] / 'bin' / 'bash.exe'
            if git_bin.is_file():
                return str(git_bin)
        return which_bash

    candidates: list[Path] = []
    which_git = shutil.which('git')
    if which_git:
        git_path = Path(which_git).resolve()
        for parent in git_path.parents:
            candidates.append(parent / 'bin' / 'bash.exe')
            candidates.append(parent / 'usr' / 'bin' / 'bash.exe')
    for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('LocalAppData')):
        if root:
            candidates.append(Path(root) / 'Git' / 'bin' / 'bash.exe')
            candidates.append(Path(root) / 'Git' / 'usr' / 'bin' / 'bash.exe')
            candidates.append(Path(root) / 'Programs' / 'Git' / 'bin' / 'bash.exe')
    candidates.extend((
        Path(r'C:\Program Files\Git\bin\bash.exe'),
        Path(r'C:\Program Files\Git\usr\bin\bash.exe'),
        Path(r'C:\Program Files (x86)\Git\bin\bash.exe'),
    ))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return which_bash or 'bash'


def bash_cmd(*args: str | os.PathLike[str]) -> list[str]:
    """Build an argv list starting with the resolved bash executable."""
    return [bash_executable(), *(os.fspath(a) for a in args)]


def resolve_win_command(argv: list[str]) -> tuple[Path, list[str]]:
    """Resolve an executable path and Windows wrapper (.cmd/.bat/shebang) for CreateProcess."""
    raw = argv[0]
    if not IS_WINDOWS:
        return Path(raw).resolve(), list(argv)
    found = shutil.which(raw)
    if found:
        binary = Path(found).resolve()
        if binary.suffix.lower() in ('.cmd', '.bat'):
            return binary, ['cmd.exe', '/c', str(binary), *argv[1:]]
        if binary.suffix.lower() in ('.exe', '.com'):
            return binary, [str(binary), *argv[1:]]
    else:
        binary = Path(raw).resolve()
    if binary.is_file():
        if binary.suffix.lower() in ('.cmd', '.bat'):
            return binary, ['cmd.exe', '/c', str(binary), *argv[1:]]
        try:
            with binary.open('rb') as handle:
                head = handle.read(256)
        except OSError:
            head = b''
        if head.startswith(b'#!'):
            line = head.splitlines()[0][2:].decode('utf-8', 'replace').strip()
            if 'python' in line:
                return binary, [sys.executable, str(binary), *argv[1:]]
            if 'sh' in line:
                return binary, bash_cmd(str(binary), *argv[1:])
    return binary, [str(binary), *argv[1:]] if found else list(argv)


def install_signal(sig: int | None, handler) -> None:
    """Install a signal handler when the signal exists on this platform."""
    if sig is not None:
        signal.signal(sig, handler)


@contextlib.contextmanager
def mask_sigterm():
    """Block SIGTERM around non-interruptible writes on POSIX; no-op on Windows."""
    if hasattr(signal, 'pthread_sigmask') and hasattr(signal, 'SIG_BLOCK'):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    else:
        yield


def popen_group_kwargs(*, detached: bool = False) -> dict[str, object]:
    """Keyword arguments placing a subprocess into its own group/session."""
    if IS_WINDOWS:
        flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | _CREATE_NO_WINDOW
        if detached:
            flags |= getattr(subprocess, 'DETACHED_PROCESS', 0)
        return {'creationflags': flags}
    return {'start_new_session': True}


def exec_or_exit(argv: list[str], env: dict[str, str] | None = None, *, inherit_fds: bool = False) -> None:
    """Replace the current process on POSIX; on Windows run and exit so inherited locks remain held."""
    if not IS_WINDOWS:
        if env is not None:
            os.execve(argv[0], argv, env)
        else:
            os.execvp(argv[0], argv)
        raise AssertionError('unreachable')
    done = subprocess.run(argv, env=env, close_fds=not inherit_fds)
    sys.exit(done.returncode)


if IS_WINDOWS:
    import ctypes.wintypes as _wintypes

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class _FILETIME(ctypes.Structure):
        _fields_ = [('dwLowDateTime', _wintypes.DWORD), ('dwHighDateTime', _wintypes.DWORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', _wintypes.DWORD),
            ('cntUsage', _wintypes.DWORD),
            ('th32ProcessID', _wintypes.DWORD),
            ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', _wintypes.DWORD),
            ('cntThreads', _wintypes.DWORD),
            ('th32ParentProcessID', _wintypes.DWORD),
            ('pcPriClassBase', _wintypes.LONG),
            ('dwFlags', _wintypes.DWORD),
            ('szExeFile', _wintypes.WCHAR * 260),
        ]

    _kernel32.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
    _kernel32.OpenProcess.restype = _wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
    _kernel32.CloseHandle.restype = _wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [_wintypes.HANDLE, ctypes.POINTER(_wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = _wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        _wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = _wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [_wintypes.HANDLE, _wintypes.UINT]
    _kernel32.TerminateProcess.restype = _wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [_wintypes.DWORD, _wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = _wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [_wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = _wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [_wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = _wintypes.BOOL


def pid_alive(pid: int, *, min_pid: int = 1) -> bool:
    """Return True iff ``pid`` names a running OS process (with ``pid >= min_pid``)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < min_pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError):
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    if pid > 0xFFFFFFFF:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = _wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def process_start_time(pid: int) -> str | None:
    """Stable opaque creation timestamp for an active Windows PID (None if exited/unavailable)."""
    if not IS_WINDOWS or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid > 0xFFFFFFFF:
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = _wintypes.DWORD()
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            return None
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not _kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return str((c.dwHighDateTime << 32) | c.dwLowDateTime)
    finally:
        _kernel32.CloseHandle(handle)


def win_ps_table() -> dict[int, tuple[int, str, str]]:
    """Windows snapshot matching forge-guard.ps_table(): {pid: (ppid, start_time, exe)}."""
    if not IS_WINDOWS:
        return {}
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _wintypes.HANDLE(-1).value:
        return {}
    table: dict[int, tuple[int, str, str]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            ppid = int(entry.th32ParentProcessID)
            if pid > 0:
                start = process_start_time(pid) or ''
                table[pid] = (ppid, start, entry.szExeFile)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return table


def _started_not_before(child_start: str, parent_start: str) -> bool:
    """Guard against stale Windows th32ParentProcessID when a PID was reused."""
    if not IS_WINDOWS:
        return True
    if child_start.isdigit() and parent_start.isdigit():
        return int(child_start) >= int(parent_start)
    return True


def win_descendants(table: dict[int, tuple[int, str, str]], roots: set[int]) -> set[int]:
    """Return descendant PIDs in ``table`` whose creation time >= their parent's."""
    by_parent: dict[int, list[int]] = {}
    for pid, (ppid, _start, _exe) in table.items():
        if pid != ppid:
            by_parent.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    queue = list(roots)
    while queue:
        parent = queue.pop(0)
        parent_start = table[parent][1] if parent in table else ''
        for child in by_parent.get(parent, ()):
            if child in found or child in roots:
                continue
            child_start = table[child][1]
            if not _started_not_before(child_start, parent_start):
                continue
            found.add(child)
            queue.append(child)
    return found


def _win_terminate_pid(pid: int) -> bool:
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


def signal_pid(pid: int, force: bool = False) -> bool:
    """Terminate a single process (`SIGTERM`/`SIGKILL` on POSIX; `TerminateProcess` on Windows)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not IS_WINDOWS:
        sig = SIGKILL if force else signal.SIGTERM
        try:
            os.kill(pid, sig)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    if not force:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False
    if _win_terminate_pid(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def kill_tree(pid: int, force: bool = False) -> None:
    """Signal/terminate a process and its descendant tree (start-time guarded on Windows)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    if not IS_WINDOWS:
        sig = SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        return

    table = win_ps_table()
    targets = ({pid} if pid in table else set()) | win_descendants(table, {pid})
    self_pid = os.getpid()
    # Signal descendants before root, validating start times against snapshot before terminating.
    for target in sorted(targets - {pid}, reverse=True):
        if target == self_pid:
            continue
        expected_start = table.get(target, (0, '', ''))[1]
        current_start = process_start_time(target)
        if expected_start and current_start and current_start != expected_start:
            continue
        _win_terminate_pid(target)
    if pid != self_pid:
        expected_start = table.get(pid, (0, '', ''))[1]
        current_start = process_start_time(pid)
        if not (expected_start and current_start and current_start != expected_start):
            if force or pid_alive(pid):
                _win_terminate_pid(pid)


def group_alive(pgid: int) -> bool:
    """Return True if process group (POSIX) or root PID (Windows) has live processes."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    return pid_alive(pgid)
