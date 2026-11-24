#!/usr/bin/env python3
"""Setup-generated and repo-configured ignored paths linked into review snapshots (#198).

Subcommands:
    capture-before CHECKOUT OUT_FILE
    capture-after CHECKOUT BEFORE_FILE [--out PATH] [--dep-dirs "a b ..."]
    plan-extra SOURCE SNAPSHOT [--extra-file PATH] [--dep-dirs "a b ..."]

`capture-before` / `capture-after` record non-secret ignored paths created during
worktree setup (difference of `git status --ignored --porcelain -z` before and after)
into `<git-dir>/forge-setup-generated` (or `--out`).
`plan-extra` combines those paths with `<source>/.forge/review-links` (if present) and
optional `--extra-file`, filters secrets and paths already covered by dependency dirs
or committed in the snapshot, and prints tab-separated actions for `forge_review_deps`:
    LINK_DIR<TAB><rel>
    LINK_FILE<TAB><rel>
    UNLINKED<TAB><rel>
Stdlib only. Cross-platform (Windows, macOS, Linux).
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import subprocess
import sys

DEFAULT_DEP_DIRS = (
    'node_modules', 'bower_components', '.venv', 'venv', 'vendor',
    '.bundle', 'Pods', 'deps', '_build', '.dart_tool', 'elm-stuff', '.build',
)

_SECRET_EXACT = {
    '.env', '.npmrc', '.pypirc', '.netrc', '.dockercfg', '.htpasswd',
    '.git-credentials', 'credentials.json', 'secrets.json', 'secrets.yaml',
    'secrets.yml',
}
_SECRET_EXTS = (
    '.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.kdbx',
    '.asc', '.gpg', '.secret', '.secrets',
)
_SECRET_PREFIXES = ('.env.', 'service-account', 'client_secret')


def normalize_rel(raw: str) -> str | None:
    """Normalize a repo-relative path to POSIX form, or None if unsafe/empty."""
    s = raw.replace('\\', '/').strip().rstrip('/')
    while s.startswith('./'):
        s = s[2:]
    if not s or s.startswith('/') or (len(s) >= 2 and s[1] == ':'):
        return None
    parts = [p for p in s.split('/') if p and p != '.']
    if not parts or any(p == '..' for p in parts):
        return None
    if parts[0] in ('.git', '.forge'):
        return None
    return '/'.join(parts)


def is_secret_path(rel: str) -> bool:
    """Return True for filenames that commonly carry credentials or private keys."""
    lower = PurePosixPath(rel).name.lower()
    if lower in _SECRET_EXACT:
        return True
    if lower.startswith('.env.') or lower.endswith('.env'):
        return True
    if any(lower.endswith(ext) for ext in _SECRET_EXTS):
        return True
    if any(lower.startswith(pref) for pref in _SECRET_PREFIXES) and lower.endswith('.json'):
        return True
    return False


def has_dep_dir_component(rel: str, dep_dirs: set[str]) -> bool:
    """Return True if `rel` is a dependency dir or lives inside one."""
    return any(part in dep_dirs for part in rel.split('/'))


def _git_dir(checkout: Path) -> Path | None:
    done = subprocess.run(
        ['git', '-C', str(checkout), 'rev-parse', '--git-dir'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if done.returncode != 0:
        return None
    raw = done.stdout.decode('utf-8', 'surrogateescape').strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (checkout / p).resolve()


def _ignored_paths(checkout: Path) -> list[str]:
    done = subprocess.run(
        ['git', '-C', str(checkout), '-c', 'core.quotepath=off',
         'status', '--ignored', '--porcelain', '-z'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if done.returncode != 0:
        return []
    out: list[str] = []
    for rec in done.stdout.split(b'\0'):
        if not rec.startswith(b'!! '):
            continue
        norm = normalize_rel(rec[3:].decode('utf-8', 'surrogateescape'))
        if norm:
            out.append(norm)
    return sorted(set(out))


def _collapse_nested(paths: list[str]) -> list[str]:
    """Drop child paths when an ancestor directory is already in the sorted list."""
    kept: list[str] = []
    for p in sorted(set(paths), key=lambda x: (x.count('/'), x)):
        if any(p == k or p.startswith(k + '/') for k in kept):
            continue
        kept.append(p)
    return sorted(kept)


def cmd_capture_before(checkout: Path, out_file: Path) -> int:
    paths = _ignored_paths(checkout)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(b'\0'.join(p.encode('utf-8', 'surrogateescape') for p in paths))
    return 0


def cmd_capture_after(checkout: Path, before_file: Path, out_file: Path | None, dep_dirs: set[str]) -> int:
    if before_file.exists():
        raw = before_file.read_bytes()
        before = {p.decode('utf-8', 'surrogateescape') for p in raw.split(b'\0') if p}
    else:
        before = set()
    after = set(_ignored_paths(checkout))
    created = [
        p for p in sorted(after - before)
        if not has_dep_dir_component(p, dep_dirs) and not is_secret_path(p)
    ]
    created = _collapse_nested(created)
    target = out_file
    if target is None:
        gdir = _git_dir(checkout)
        if gdir is None:
            return 0
        target = gdir / 'forge-setup-generated'
    if created:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(('\n'.join(created) + '\n').encode('utf-8', 'surrogateescape'))
    elif target.exists():
        target.unlink()
    return 0


def _read_lines_file(path: Path, dep_dirs: set[str], allow_comments: bool) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding='utf-8', errors='surrogateescape').splitlines():
        text = line.strip()
        if not text or (allow_comments and text.startswith('#')):
            continue
        norm = normalize_rel(text)
        if norm and not has_dep_dir_component(norm, dep_dirs):
            out.append(norm)
    return out


def cmd_plan_extra(source: Path, snapshot: Path, extra_file: Path | None, dep_dirs: set[str]) -> int:
    candidates: dict[str, str] = {}
    gdir = _git_dir(source)
    if gdir is not None:
        for p in _read_lines_file(gdir / 'forge-setup-generated', dep_dirs, allow_comments=False):
            candidates.setdefault(p, 'setup')
    for p in _read_lines_file(source / '.forge' / 'review-links', dep_dirs, allow_comments=True):
        candidates.setdefault(p, 'config')
    if extra_file is not None:
        for p in _read_lines_file(extra_file, dep_dirs, allow_comments=True):
            candidates.setdefault(p, 'config')

    if not candidates:
        return 0

    linked_dirs: list[str] = []
    rows: list[tuple[str, str]] = []
    for rel in sorted(candidates, key=lambda x: (x.count('/'), x)):
        if any(rel == d or rel.startswith(d + '/') for d in linked_dirs):
            continue
        origin = candidates[rel]
        if is_secret_path(rel):
            if origin == 'config':
                rows.append(('UNLINKED', rel))
            continue
        snap_path = snapshot / rel
        if snap_path.exists() or snap_path.is_symlink():
            continue  # committed or already linked dependency: keep snapshot's own entry
        src_path = source / rel
        if not (src_path.exists() or src_path.is_symlink()):
            if origin == 'config':
                rows.append(('UNLINKED', rel))
            continue
        parent = snap_path.parent
        if not parent.is_dir():
            rows.append(('UNLINKED', rel))
            continue
        if src_path.is_dir():
            linked_dirs.append(rel)
            rows.append(('LINK_DIR', rel))
        elif src_path.is_file():
            rows.append(('LINK_FILE', rel))
        else:
            rows.append(('UNLINKED', rel))

    lines = [f'{kind}\t{rel}\n' for kind, rel in sorted(rows, key=lambda item: item[1])]
    sys.stdout.buffer.write(''.join(lines).encode('utf-8', 'surrogateescape'))
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='forge-review-links.py')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_before = sub.add_parser('capture-before')
    p_before.add_argument('checkout', type=Path)
    p_before.add_argument('out_file', type=Path)

    p_after = sub.add_parser('capture-after')
    p_after.add_argument('checkout', type=Path)
    p_after.add_argument('before_file', type=Path)
    p_after.add_argument('--out', type=Path, default=None)
    p_after.add_argument('--dep-dirs', default='')

    p_plan = sub.add_parser('plan-extra')
    p_plan.add_argument('source', type=Path)
    p_plan.add_argument('snapshot', type=Path)
    p_plan.add_argument('--extra-file', type=Path, default=None)
    p_plan.add_argument('--dep-dirs', default='')

    args = parser.parse_args(argv)
    if args.cmd == 'capture-before':
        return cmd_capture_before(args.checkout, args.out_file)
    dep_dirs = set(args.dep_dirs.split()) if args.dep_dirs else set(DEFAULT_DEP_DIRS)
    if args.cmd == 'capture-after':
        return cmd_capture_after(args.checkout, args.before_file, args.out, dep_dirs)
    return cmd_plan_extra(args.source, args.snapshot, args.extra_file, dep_dirs)


if __name__ == '__main__':
    sys.exit(main())
