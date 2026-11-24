"""Where a checkout's project memory lives (#159), for the Python readers of `.forge/memory.md`.

forge-memory.sh's `memory_home` decides it for every write; this is the same rule for the readers that
open the file themselves (the decision-model gates, the run dataset). A linked worktree (`git worktree
add`) whose `.forge/memory.md` and `.forge/ledger.tsv` are not tracked keeps its memory in the main
checkout's `.forge/`: `git worktree remove` deletes ignored files without a word, and the main checkout is
where later runs start. Everything else keeps it in the checkout itself: the main checkout, a tracked
`.forge/` (git refuses to remove a worktree with changes to it), a bare repository's worktree, a path
outside git, or FORGE_MEMORY_PER_CHECKOUT=1.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

_TIMEOUT = 10


def _git(repo, *args) -> str | None:
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def memory_root(repo) -> Path:
    """The directory whose `.forge/` holds `repo`'s project memory."""
    repo = Path(repo)
    if os.environ.get('FORGE_MEMORY_PER_CHECKOUT') == '1':
        return repo
    git_dir, common = _git(repo, 'rev-parse', '--git-dir'), _git(repo, 'rev-parse', '--git-common-dir')
    if git_dir is None or common is None:
        return repo
    try:
        if (repo / git_dir.strip()).resolve() == (repo / common.strip()).resolve():
            return repo                                     # the main checkout, or no linked worktree
    except OSError:
        return repo
    listing = _git(repo, 'worktree', 'list', '--porcelain') or ''
    blocks = listing.split('\n\n')
    first = blocks[0].splitlines() if blocks and blocks[0] else []
    if not first or not first[0].startswith('worktree ') or 'bare' in first:
        return repo
    main = Path(first[0][len('worktree '):])
    if not main.is_dir():
        return repo
    if (_git(repo, 'ls-files', '--', '.forge/memory.md', '.forge/ledger.tsv') or '').strip():
        return repo                                         # tracked: git already protects it
    return main


def memory_file(repo) -> Path:
    """`repo`'s `.forge/memory.md`, wherever its memory lives."""
    return memory_root(repo) / '.forge' / 'memory.md'
