"""Cross-platform advisory file locking with ``fcntl.flock`` semantics.

Uses ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows so callers in
``scripts/``, ``ui/forge_ui/``, and ``tests/`` share one locking interface.
On Windows, ``LOCK_SH`` is promoted to an exclusive lock and locks byte
``_LOCK_OFFSET`` (1 GiB) rather than byte 0 so non-locking readers of normal
file data are not blocked by mandatory Win32 byte-range locks; ``LOCK_UN`` on
an unlocked descriptor is a no-op matching POSIX ``flock``.
"""
from __future__ import annotations

import errno
import os
import time

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

if _fcntl is not None:
    LOCK_SH = _fcntl.LOCK_SH
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
else:
    import msvcrt as _msvcrt

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

_LOCK_OFFSET = 0x40000000  # 1 GiB sentinel byte; msvcrt does not extend file size

_BUSY_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ('EACCES', 'EAGAIN', 'EWOULDBLOCK', 'EDEADLK')
    if hasattr(errno, name)
)


def _fileno(fd_or_file) -> int:
    return fd_or_file if isinstance(fd_or_file, int) else fd_or_file.fileno()


def _msvcrt_lock_sentinel(fd: int, mode: int) -> None:
    """Lock or unlock ``_LOCK_OFFSET`` on ``fd`` while preserving seek offset."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        _msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def flock(fd_or_file, operation: int) -> None:
    """Acquire or release an advisory lock matching ``fcntl.flock`` semantics."""
    if _fcntl is not None:
        _fcntl.flock(fd_or_file, operation)
        return

    fd = _fileno(fd_or_file)
    nonblocking = bool(operation & LOCK_NB)
    base_op = operation & ~LOCK_NB

    if base_op == LOCK_UN:
        try:
            _msvcrt_lock_sentinel(fd, _msvcrt.LK_UNLCK)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM, errno.EINVAL):
                raise
        return

    if base_op not in (LOCK_EX, LOCK_SH):
        raise ValueError(f'unsupported flock operation: {operation!r}')

    while True:
        try:
            _msvcrt_lock_sentinel(fd, _msvcrt.LK_NBLCK)
            return
        except OSError as exc:
            if exc.errno not in _BUSY_ERRNOS:
                raise
            if nonblocking:
                raise BlockingIOError(errno.EAGAIN, exc.strerror or 'Resource temporarily unavailable') from exc
            time.sleep(0.05)
