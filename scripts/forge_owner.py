#!/usr/bin/env python3
"""Which task of a plan declares a path.

`<plan>/tasks.tsv` is tab-separated `id deps difficulty files dwarf qa title`.
`files` is a comma list of repo-relative paths or directories; `-` declares
nothing. A later decision asks this module which task owns a path, and whether
that path sits outside one task, using the same coverage rule the scheduler
uses to serialise tasks.

    python3 scripts/forge_owner.py <plan> <path>... [--task ID]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import posixpath
import sys


def _rows(plan: str | os.PathLike) -> list[tuple[str, str]]:
    """`(id, files)` from tasks.tsv, in file order.

    Comment lines, blank lines and rows with fewer than four columns are
    skipped. A missing tasks.tsv raises FileNotFoundError.
    """
    text = (Path(plan) / 'tasks.tsv').read_text(encoding='utf-8').replace('\r', '')
    found = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        columns = line.split('\t')
        if len(columns) < 4:
            continue
        found.append((columns[0], columns[3]))
    return found


def _entries(files: str) -> list[str]:
    """Normalised paths in one files cell. Empty tokens and `-` declare nothing."""
    return [posixpath.normpath(part) for part in files.split(',') if part and part != '-']


def declared(plan: str | os.PathLike, task: str) -> list[str]:
    """That task's normalised `files` entries. `[]` for `-` or an unknown task."""
    for ident, files in _rows(plan):
        if ident == task:
            return _entries(files)
    return []


def covers(entry: str, path: str) -> bool:
    """Whether one declared entry covers one path. An entry of `.` covers every path."""
    # forge-schedule.py overlap: posixpath.normpath on each side, then equal or a '/' prefix; '.' covers all.
    def piece(value: str) -> str | None:
        if not value or value == '-':
            return None
        return posixpath.normpath(value)

    left, right = piece(entry), piece(path)
    if left is None or right is None:
        return False
    return (left == '.' or right == '.' or left == right
            or left.startswith(right + '/') or right.startswith(left + '/'))


def _query(path: str) -> str | None:
    """Repo-relative form of a path, or None when no task can own it.

    Strip one leading `./`, then posixpath.normpath. An absolute result, or one
    that starts with `..`, is outside the repository.
    """
    text = str(path)
    if text.startswith('./'):
        text = text[2:]
    norm = posixpath.normpath(text)
    if posixpath.isabs(norm) or norm.startswith('..'):
        return None
    return norm


def owners(plan: str | os.PathLike, path: str) -> list[str]:
    """Every task id whose `files` cover `path`, in tasks.tsv order."""
    rows = _rows(plan)
    query = _query(path)
    if query is None:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for ident, files in rows:
        if ident in seen:
            continue
        seen.add(ident)  # the first row is the task's declaration, as declared() reads it
        if any(covers(entry, query) for entry in _entries(files)):
            found.append(ident)
    return found


def owner(plan: str | os.PathLike, path: str) -> str | None:
    """The first task in tasks.tsv order whose `files` cover `path`, or None."""
    found = owners(plan, path)
    return found[0] if found else None


def outside(plan: str | os.PathLike, task: str, path: str) -> bool:
    """True when none of `task`'s declared entries covers `path`.

    A task that declares `.` has nothing outside. A task whose `files` is `-`,
    or an unknown task, has everything outside.
    """
    return not any(covers(entry, path) for entry in declared(plan, task))


def _report(plan: str, paths: list[str], task: str | None) -> dict:
    idents = [ident for ident, _files in _rows(plan)]
    if task is not None and task not in idents:
        raise LookupError(task)
    items = []
    for path in paths:
        found = owners(plan, path)
        items.append({
            'path': path,
            'owner': found[0] if found else None,
            'owners': found,
            'outside': outside(plan, task, path) if task is not None else None,
        })
    return {'paths': items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='forge_owner.py', description='Which task of a plan declares a path.')
    parser.add_argument('plan', help='plan directory containing tasks.tsv')
    parser.add_argument('paths', nargs='+', help='repo-relative paths to look up')
    parser.add_argument('--task', help='also report whether each path is outside this task')
    args = parser.parse_args(argv)
    try:
        report = _report(args.plan, args.paths, args.task)
    except FileNotFoundError:
        print("forge_owner: no tasks.tsv in '%s'" % args.plan, file=sys.stderr)
        return 2
    except LookupError as error:
        print("forge_owner: unknown task '%s'" % error, file=sys.stderr)
        return 2
    json.dump(report, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
