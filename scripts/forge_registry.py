"""Where a repo's run directories live, outside the repo.

``$XDG_CONFIG_HOME/forge/runs/<sha256(realpath(repo))[:16]>.txt`` holds one absolute
run-dir path per line, with a ``#`` header on a new file. ``forge jev``, ``forge
dataset import --registry`` and the forge-ui launcher all use it, so it does not
live inside the Jev package.

Run as a script, ``forge_registry.py register <run-dir>`` records a run dir from the
shell runners (forge-ui-options.sh ``forge_ui_register``): every plan, run and solo run
is registered, not only the ones opened with ``--ui``, because forge-ui's map of a
repo's runs reads this registry. ``FORGE_RUN_REGISTRY=off`` turns that off.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


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


def register_run_dir(repo: Path, run_dir: Path) -> None:
    """Record an absolute run-dir path for this repo, deduplicated.

    Never raises and never blocks: failing to note where a run lives must not fail the run,
    and ``forge ui open`` calls this before any of its own timeouts.
    """
    try:
        # A relative XDG_CONFIG_HOME resolves below the current directory -- for a runner or
        # `forge ui open`, usually the user's repo. Forge does not write into the user's tree.
        if not registry_dir().is_absolute():
            return
        registry = runs_registry(repo)
        registry.parent.mkdir(parents=True, exist_ok=True)
        line = str(Path(run_dir).resolve())
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
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            for entry in header:
                handle.write(entry + '\n')
            handle.write(line + '\n')
    except (OSError, ValueError, RuntimeError):
        # ValueError: a NUL byte in a path; RuntimeError: a symlink loop (pathlib before 3.13)
        pass


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
