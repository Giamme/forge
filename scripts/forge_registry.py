"""Where a repo's run directories live, outside the repo.

``$XDG_CONFIG_HOME/forge/runs/<sha256(realpath(repo))[:16]>.txt`` holds one absolute
run-dir path per line, with a ``#`` header on a new file. ``forge jev``, ``forge
dataset import --registry`` and the forge-ui launcher all use it, so it does not
live inside the Jev package.

Two writers change those files: ``register_run_dir`` (every runner, on every start) and
``unregister_run_dir`` (``forge runs rm``). Both take an flock on ``<registry dir>/.lock``,
a stable sibling file that is never replaced. Register never blocks: it waits at most
``REGISTER_LOCK_WAIT_S`` for the lock and then appends anyway, because a rare duplicate
line is harmless and a lost run is not. An unlocked append can then land in the file an
unregister has read and is about to replace, so the two meet halfway: unregister, once it
has replaced a file, copies onto the new one whatever was appended to the old one after
its read, and register, once it has appended, checks that the file it wrote is still the
registry and appends again to the new one if it is not. Either the append reached the old
file before unregister looked at it again, or register sees the replacement.

Run as a script, ``forge_registry.py register <run-dir>`` records a run dir from the
shell runners (forge-ui-options.sh ``forge_ui_register``): every plan, run and solo run
is registered, not only the ones opened with ``--ui``, because forge-ui's map of a
repo's runs reads this registry. ``FORGE_RUN_REGISTRY=off`` turns that off.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

# register_run_dir runs before forge ui open's own timeouts: it waits this long for the lock,
# then appends without it. unregister_run_dir is only ever called by `forge runs rm`, which
# can afford to wait for a busy lock, but not for ever.
REGISTER_LOCK_WAIT_S = 0.5
UNREGISTER_LOCK_WAIT_S = 10.0
# How many times an unlocked register appends again after an unregister replaced the file
# under it. One is enough unless unregisters keep finishing back to back.
REGISTER_TRIES = 5
LOCK_NAME = '.lock'


def registry_dir() -> Path:
    """The ``runs`` directory under forge's config home.

    Resolved the same way ``forge_jev.config_path()`` resolves ``jev.json``:
    ``XDG_CONFIG_HOME`` when set and not empty, otherwise ``~/.config``, then
    ``expanduser()``. An empty value counts as unset, as the XDG spec says and as
    forge-ui's run list reads it; taken literally it named a directory below the
    current one, usually the user's repo. ``config_path()`` is
    ``<that>/forge/jev.json``; the registry is the sibling ``runs`` directory.
    """
    config = (Path(os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')).expanduser()
              / 'forge/jev.json')
    return config.parent / 'runs'


def repo_key(repo) -> str:
    """16 hex chars of ``sha256(realpath(repo))``. The registry file name and the dataset key."""
    return hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]


def runs_registry(repo) -> Path:
    """Where this repo's run-dir list lives -- beside the forge config, never in the repo.

    The first version of this wrote <repo>/.forge/jev-runs.txt, which left the user's
    working tree dirty and made `integrate` refuse to run: "working tree is dirty --
    commit or stash before integrating". Forge's contract is that it does not touch the
    user's tree, and a file recording where forge keeps its own data is forge's
    bookkeeping, not the project's.
    """
    return registry_dir() / (repo_key(repo) + '.txt')


@contextlib.contextmanager
def _registry_lock(wait_s: float):
    """Hold an exclusive flock on ``<registry dir>/.lock`` for the ``with`` body.

    The lock is on a file of its own and never on a registry file, because unregister
    replaces those: a lock on a file that is renamed over locks nothing. Polls without
    blocking for up to ``wait_s`` seconds, then raises BlockingIOError; raises OSError if
    the lock file cannot be opened or is not a regular file (a FIFO would block a reader).
    A filesystem that cannot flock at all (ENOLCK, EOPNOTSUPP) gets no locking, as it
    would anywhere else in forge, rather than a registry nobody can change.
    """
    fd = os.open(registry_dir() / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'the registry lock is not a regular file', LOCK_NAME)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    break
                if time.monotonic() >= deadline:
                    raise BlockingIOError(errno.EWOULDBLOCK, 'the run registry is locked', LOCK_NAME) from error
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def register_run_dir(repo: Path, run_dir: Path) -> None:
    """Record an absolute run-dir path for this repo, deduplicated.

    Never raises and never blocks: failing to note where a run lives must not fail the run,
    and ``forge ui open`` calls this before any of its own timeouts. The registry lock is
    tried for at most ``REGISTER_LOCK_WAIT_S``; if it stays busy the line is appended without
    it (see the module docstring), and appended again if an unregister replaced the file
    it went to.
    """
    try:
        # A relative XDG_CONFIG_HOME resolves below the current directory -- for a runner or
        # `forge ui open`, usually the user's repo. Forge does not write into the user's tree.
        if not registry_dir().is_absolute():
            return
        registry = runs_registry(repo)
        registry.parent.mkdir(parents=True, exist_ok=True)
        line = str(Path(run_dir).resolve())
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(_registry_lock(REGISTER_LOCK_WAIT_S))
            except OSError:
                pass   # busy past the wait, or no lock file to be had: append unlocked
            for _ in range(REGISTER_TRIES):
                existing = []
                if registry.is_file():
                    existing = [x.strip() for x in registry.read_text(errors='replace').splitlines()]
                if line in existing:
                    return
                header = [] if existing else ['# forge run dirs for ' + str(Path(repo).resolve())]
                # O_NONBLOCK: a FIFO with no reader fails at once (ENXIO) instead of blocking the
                # open, and anything that is not a regular file is left alone.
                fd = os.open(registry, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
                with os.fdopen(fd, 'a') as handle:
                    written = os.fstat(fd)
                    if not stat.S_ISREG(written.st_mode):
                        return
                    handle.write(''.join(entry + '\n' for entry in header + [line]))
                    handle.flush()
                # Still the registry: the line is in it, or in the old file an unregister copies
                # from once it has replaced it. Replaced already: the line may be in neither.
                try:
                    current = os.lstat(registry)
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) == (written.st_dev, written.st_ino):
                    return
    except (OSError, ValueError, RuntimeError):
        # ValueError: a NUL byte in a path; RuntimeError: a symlink loop (pathlib before 3.13)
        pass


def _rewrite_without(path: Path, wanted: set) -> bool:
    """Atomically drop the lines of one registry file that name a path in ``wanted``.

    Every other byte is kept as it was. Returns whether the file changed. A registry file
    that is not a regular file is skipped, and read without blocking.

    The caller holds the registry lock, but a register that gave up waiting for it appends
    anyway, possibly to this file after it was read. So the old file stays open across the
    replace, and whatever it gained since the read is appended to the new one.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ELOOP):
            return False
        raise
    with os.fdopen(fd, 'rb') as old:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return False
        data = old.read()
        lines = data.splitlines(keepends=True)
        kept = [line for line in lines if os.fsdecode(line).strip() not in wanted]
        if len(kept) == len(lines):
            return False
        content = b''.join(kept)
        temporary = None
        try:
            out, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
            with os.fdopen(out, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        # An append that comes later still is register's to notice: the path no longer
        # names the file it wrote.
        late = old.read()
    if late:
        extra = [line for line in late.splitlines(keepends=True) if os.fsdecode(line).strip() not in wanted]
        if extra:
            if content and not content.endswith(b'\n'):
                extra.insert(0, b'\n')
            out = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(out, 'ab') as handle:
                handle.write(b''.join(extra))
    return True


def unregister_run_dir(run_dir) -> list:
    """Remove a run dir from every registry file that lists it; return the files rewritten.

    The reverse of ``register_run_dir``, for ``forge runs rm``, so unlike it this reports
    failure: it raises OSError (BlockingIOError when the registry lock stays busy) and the
    caller says the registry needs repair. Each file is rewritten atomically under the
    registry lock, so a concurrent register loses nothing. ``run_dir`` need not exist any
    more: entries were written resolved, so the path is resolved the same way, and the
    path as given is matched too.
    """
    directory = registry_dir()
    if not directory.is_absolute() or not directory.is_dir():
        return []
    wanted = {os.path.realpath(run_dir), os.path.abspath(run_dir)}
    changed = []
    with _registry_lock(UNREGISTER_LOCK_WAIT_S):
        for path in sorted(directory.glob('*.txt')):
            if _rewrite_without(path, wanted):
                changed.append(path)
    return changed


# A path, not a file's worth of data: the first line of <run-dir>/repo is all that is read.
_REPO_LINE_MAX = 64 * 1024


def _repo_of(run_dir: Path) -> Path | None:
    """The repo named on the first line of ``<run-dir>/repo``, or None. Never blocks.

    The runners write that file themselves, but a run dir is any directory a user passes,
    so the read is as careful as forge-ui's: O_NONBLOCK so a FIFO with no writer cannot
    hold the open, and nothing but a regular file is read (a FIFO, a device or a directory
    named ``repo`` is ignored).
    """
    fd = os.open(run_dir / 'repo', os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        line = handle.readline(_REPO_LINE_MAX).decode(errors='replace').strip()
    # Empty, or text no path can hold, or relative: a relative repo would be resolved
    # against whatever directory the runner happened to start in, and keyed to the wrong repo.
    if not line or '\x00' in line or not os.path.isabs(line):
        return None
    return Path(line)


def _register(run_dir_arg: str) -> None:
    if os.environ.get('FORGE_RUN_REGISTRY', '').strip().lower() == 'off':
        return
    run_dir = Path(run_dir_arg).resolve()
    repo = _repo_of(run_dir)
    if repo is not None:
        register_run_dir(repo, run_dir)


def main() -> int:
    """``register <run-dir>``: record the run dir under the repo its ``repo`` file names.

    Always exits 0 and prints nothing. The runners call it on every plan, run and solo run,
    and noting where a run lives must never fail, block, slow or change one: a missing
    run dir, an unreadable or hostile ``repo`` file, an unwritable registry and arguments
    it does not understand are all silently nothing. Ctrl-C is not swallowed, so an
    interrupted runner still stops.
    """
    try:
        args = sys.argv[1:]
        if len(args) == 2 and args[0] == 'register':
            _register(args[1])
    except Exception:  # noqa: BLE001  -- see the docstring: nothing here may reach the run
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
