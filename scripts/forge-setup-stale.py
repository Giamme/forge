#!/usr/bin/env python3
"""Track worktree setup commits and detect stale generated inputs before verify."""

from __future__ import annotations

import argparse
import fnmatch
import posixpath
import subprocess
import sys
from pathlib import Path

DEFAULT_LOCKFILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "npm-shrinkwrap.json",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
        "composer.lock",
        "Package.resolved",
        "Podfile.lock",
        "mix.lock",
        "pubspec.lock",
        "flake.lock",
        "gradle.lockfile",
    }
)


def _git_stdout(checkout: Path, *args: str) -> bytes | None:
    res = subprocess.run(
        ["git", "-C", str(checkout), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def _git_text(checkout: Path, *args: str) -> str:
    raw = _git_stdout(checkout, *args)
    if raw is None:
        return ""
    return raw.decode("utf-8", "surrogateescape").strip()


def _git_dir(checkout: Path) -> Path | None:
    gdir = _git_text(checkout, "rev-parse", "--git-dir")
    if not gdir:
        return None
    p = Path(gdir)
    if not p.is_absolute():
        p = checkout / p
    return p


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def cmd_record_commit(args: argparse.Namespace) -> int:
    checkout = Path(args.checkout)
    head = _git_text(checkout, "rev-parse", "HEAD")
    if not head:
        return 0
    gdir = _git_dir(checkout)
    if gdir is not None:
        _write_lf(gdir / "forge-setup-commit", head + "\n")
    if args.out:
        _write_lf(Path(args.out), head + "\n")
    return 0


def _load_setup_inputs(wt: Path, repo: Path) -> list[str] | None:
    for candidate in (wt / ".forge" / "setup-inputs", repo / ".forge" / "setup-inputs"):
        if candidate.is_file():
            raw = candidate.read_bytes().decode("utf-8", "surrogateescape")
            patterns: list[str] = []
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    patterns.append(stripped.replace("\\", "/").lstrip("./"))
            return patterns
    return None


def _matches_pattern(rel_path: str, pattern: str) -> bool:
    rel_path = rel_path.replace("\\", "/").lstrip("./")
    if not pattern:
        return False
    if pattern.endswith("/"):
        return rel_path.startswith(pattern)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return rel_path == prefix or rel_path.startswith(prefix + "/")
    has_glob = any(ch in pattern for ch in "*?[")
    if has_glob:
        if "/" in pattern:
            return fnmatch.fnmatch(rel_path, pattern)
        return fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
            posixpath.basename(rel_path), pattern
        )
    if "/" in pattern:
        return rel_path == pattern or rel_path.startswith(pattern + "/")
    return (
        rel_path == pattern
        or rel_path.startswith(pattern + "/")
        or posixpath.basename(rel_path) == pattern
    )


def _changed_files(wt: Path, base_commit: str, head_commit: str) -> list[str]:
    raw = _git_stdout(
        wt, "diff", "--name-only", "-z", base_commit, head_commit, "--", "."
    )
    if not raw:
        return []
    items = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\x00") if p]
    return [p.replace("\\", "/") for p in items]


def _resolve_setup_commit(plan: Path, wt: Path, override_path: str | None) -> str:
    if override_path:
        p = Path(override_path)
        if p.is_file():
            val = p.read_bytes().decode("utf-8", "surrogateescape").strip()
            if val:
                return val
    gdir = _git_dir(wt)
    if gdir is not None:
        marker = gdir / "forge-setup-commit"
        if marker.is_file():
            val = marker.read_bytes().decode("utf-8", "surrogateescape").strip()
            if val:
                return val
    integ_marker = plan / "tasks" / "_integration" / "setup.commit"
    try:
        wt_real = wt.resolve()
        integ_dir = (plan / "tasks" / "_integration").resolve()
        wt_is_integ = wt_real == integ_dir or integ_dir in wt_real.parents
    except OSError:
        wt_is_integ = False
    if wt_is_integ and integ_marker.is_file():
        val = integ_marker.read_bytes().decode("utf-8", "surrogateescape").strip()
        if val:
            return val
    return ""


def _emit(status: str, matched: list[str]) -> int:
    summary = ", ".join(matched[:5])
    if len(matched) > 5:
        summary += f" (+{len(matched) - 5} more)"
    line = f"{status}\t{summary}\n"
    sys.stdout.buffer.write(line.encode("utf-8", "surrogateescape"))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    plan = Path(args.plan)
    wt = Path(args.wt)
    repo = Path(args.repo)

    setup_commit = _resolve_setup_commit(plan, wt, args.setup_commit)
    if not setup_commit:
        return _emit("OK", [])
    head_commit = _git_text(wt, "rev-parse", "HEAD")
    if not head_commit or setup_commit == head_commit:
        return _emit("OK", [])

    changed = _changed_files(wt, setup_commit, head_commit)
    if not changed:
        return _emit("OK", [])

    patterns = _load_setup_inputs(wt, repo)
    setup_before_verify = (plan / "setup_before_verify").is_file()

    if setup_before_verify:
        if patterns is not None:
            matched = [
                p
                for p in changed
                if p in (".forge/setup", ".forge/setup-inputs")
                or any(_matches_pattern(p, pat) for pat in patterns)
            ]
            if matched:
                return _emit("RERUN", matched)
            return _emit("OK", [])
        return _emit("RERUN", changed)

    matched_stale: list[str] = []
    for p in changed:
        base = posixpath.basename(p)
        is_lock = base in DEFAULT_LOCKFILES
        is_declared = patterns is not None and any(
            _matches_pattern(p, pat) for pat in patterns
        )
        if is_lock or is_declared or p == ".forge/setup":
            matched_stale.append(p)
    if matched_stale:
        return _emit("STALE", matched_stale)
    return _emit("OK", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or record setup commit staleness.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record-commit")
    p_rec.add_argument("checkout")
    p_rec.add_argument("--out", default=None)
    p_rec.set_defaults(func=cmd_record_commit)

    p_chk = sub.add_parser("check")
    p_chk.add_argument("plan")
    p_chk.add_argument("wt")
    p_chk.add_argument("repo")
    p_chk.add_argument("--setup-commit", default=None)
    p_chk.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
