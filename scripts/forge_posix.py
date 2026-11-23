"""Cross-platform POSIX open-flag and directory-handle helpers.

On POSIX, keeps exact kernel-enforced ``O_NOFOLLOW``, ``O_NONBLOCK``,
``O_CLOEXEC``, ``O_DIRECTORY``, and ``dir_fd`` semantics. On Windows, validates
symlinks and NTFS reparse points (junctions) via ``os.lstat`` before opening
(note: the lstat-then-open fallback is best-effort/TOCTOU on Windows).
"""
from __future__ import annotations

import errno
import os
import re
import stat

IS_WINDOWS = os.name == 'nt'
_MSYS_PATH_RE = re.compile(r'^/([a-zA-Z])(?:/(.*))?$')

O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)

_REPARSE_ATTR = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)

SUPPORTS_DIR_FD = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


def _is_link_or_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & _REPARSE_ATTR)


def _reject_symlink(path: os.PathLike | str) -> None:
    """Raise ELOOP if ``path`` is a symlink or reparse point (Windows fallback)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if _is_link_or_reparse(st):
        raise OSError(errno.ELOOP, 'Too many levels of symbolic links', os.fspath(path))


def open_nofollow(path: os.PathLike | str, flags: int, mode: int = 0o777) -> int:
    """Open ``path`` without following symlinks or junctions."""
    if O_NOFOLLOW != 0:
        flags |= O_NOFOLLOW
    else:
        _reject_symlink(path)
    return os.open(path, flags, mode)


def open_dir(path: os.PathLike | str, *, nofollow: bool = True) -> int | str:
    """Open a real directory safely; returns an int dir_fd on POSIX or path str on Windows."""
    flags = os.O_RDONLY | O_DIRECTORY | (O_NOFOLLOW if nofollow else 0) | O_CLOEXEC
    if SUPPORTS_DIR_FD:
        return os.open(path, flags)
    st = os.lstat(path)
    if nofollow and _is_link_or_reparse(st):
        raise OSError(errno.ELOOP, 'Too many levels of symbolic links', os.fspath(path))
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, 'Not a directory', os.fspath(path))
    return os.fspath(path)


def close_dir(handle: int | str) -> None:
    if isinstance(handle, int):
        os.close(handle)


def enter_dir(handle: int | str, part: str, *, nofollow: bool = True) -> int | str:
    if isinstance(handle, int):
        flags = os.O_RDONLY | O_DIRECTORY | (O_NOFOLLOW if nofollow else 0) | O_CLOEXEC
        return os.open(part, flags, dir_fd=handle)
    return open_dir(os.path.join(handle, part), nofollow=nofollow)


def mkdir_at(handle: int | str, part: str) -> None:
    if isinstance(handle, int):
        os.mkdir(part, dir_fd=handle)
    else:
        os.mkdir(os.path.join(handle, part))


def stat_at(handle: int | str, leaf: str, *, follow_symlinks: bool = False) -> os.stat_result:
    if isinstance(handle, int):
        return os.stat(leaf, dir_fd=handle, follow_symlinks=follow_symlinks)
    return os.stat(os.path.join(handle, leaf), follow_symlinks=follow_symlinks)


def open_at(handle: int | str, leaf: str, flags: int, mode: int = 0o644) -> int:
    if isinstance(handle, int):
        return os.open(leaf, flags, mode, dir_fd=handle)
    return open_nofollow(os.path.join(handle, leaf), flags, mode)


def replace_at(handle: int | str, src: str, dst: str) -> None:
    if isinstance(handle, int):
        os.replace(src, dst, src_dir_fd=handle, dst_dir_fd=handle)
    else:
        os.replace(os.path.join(handle, src), os.path.join(handle, dst))


def unlink_at(handle: int | str, leaf: str) -> None:
    if isinstance(handle, int):
        os.unlink(leaf, dir_fd=handle)
    else:
        os.unlink(os.path.join(handle, leaf))


def host_path(path: os.PathLike | str) -> str:
    """Convert an MSYS/Git-Bash `/c/...` path to `C:/...` on native Windows."""
    text = os.fspath(path)
    if IS_WINDOWS and text:
        if text.startswith('\\\\?\\') and len(text) >= 6 and text[5] == ':':
            text = text[4:]
        m = _MSYS_PATH_RE.fullmatch(text)
        if m:
            rest = m.group(2) or ''
            return f'{m.group(1).upper()}:/{rest}'
    return text
