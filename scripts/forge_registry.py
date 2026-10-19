"""Where a repo's run directories live, outside the repo.

``$XDG_CONFIG_HOME/forge/runs/<sha256(realpath(repo))[:16]>.txt`` holds one absolute
run-dir path per line, with a ``#`` header on a new file. ``forge jev``, ``forge
dataset import --registry`` and the forge-ui launcher all use it, so it does not
live inside the Jev package.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def registry_dir() -> Path:
    """The ``runs`` directory under forge's config home.

    Resolved the same way ``forge_jev.config_path()`` resolves ``jev.json``:
    ``XDG_CONFIG_HOME`` when set, otherwise ``~/.config``, then ``expanduser()``.
    ``config_path()`` is ``<that>/forge/jev.json``; the registry is the sibling
    ``runs`` directory.
    """
    config = (Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser()
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

    Never raises: failing to note where a run lives must not fail the run.
    """
    try:
        registry = runs_registry(repo)
        registry.parent.mkdir(parents=True, exist_ok=True)
        line = str(Path(run_dir).resolve())
        existing = []
        if registry.is_file():
            existing = [x.strip() for x in registry.read_text(errors='replace').splitlines()]
        if line in existing:
            return
        header = [] if existing else ['# run dirs that have scored ' + str(Path(repo).resolve())]
        with registry.open('a') as handle:
            for entry in header:
                handle.write(entry + '\n')
            handle.write(line + '\n')
    except OSError:
        pass
