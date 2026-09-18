"""Git-history corpus extractor: labeled (change -> tests touched) examples for Jev calibration."""
from __future__ import annotations

from collections.abc import Iterator
import re
import subprocess

# Path segment for tests/, test/, spec/, __tests__/, e2e/, or a filename convention
# (_test., .test., .spec., test_ prefix). Covers the Python and TypeScript repos this runs against.
TEST_PATTERN = re.compile(
    r'(?:^|/)(?:tests?|specs?|__tests__|e2e)/'
    r'|(?:^|/)test_[^/]*$'
    r'|[^/]*(?:_test|\.test|\.spec)\.[^/]+$'
)

# Generated/vendored/binary paths carry no signal about human test selection.
VENDOR_PATTERN = re.compile(
    r'(?:^|/)(?:node_modules|vendor|dist|build|\.next|coverage|__pycache__)/'
    r'|\.min\.js$'
    r'|(?:^|/)package-lock\.json$'
    r'|(?:^|/)yarn\.lock$'
    r'|(?:^|/)poetry\.lock$'
    r'|\.snap$'
)

_SEP = '\x1f'  # unit separator: safe delimiter for a commit-header line
# \x1e (record separator) LEADS each commit's header, so splitting on it cleanly isolates
# "previous commit's file list" from "next commit's header" -- a trailing separator would
# instead glue this commit's file list to the next commit's header in the same split token.
# Combined with `git log -z`, git NUL-terminates the format text itself (in place of the
# newline it would otherwise emit) before the --name-only listing, and NUL-terminates each
# filename too -- so a multi-line commit body (which freely contains '\n') can never be
# confused with a newline-joined file list. NUL cannot appear in a commit message or path.
_FORMAT = '\x1e' + _SEP.join(('%H', '%P', '%s', '%b'))


def is_test(path: str) -> bool:
    return bool(TEST_PATTERN.search(path))


def _is_vendor(path: str) -> bool:
    return bool(VENDOR_PATTERN.search(path))


def classify(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split into (source, test), dropping vendor paths from both."""
    source, test = [], []
    for path in paths:
        if _is_vendor(path):
            continue
        (test if is_test(path) else source).append(path)
    return source, test


def _run(repo, argv: list[str]) -> str:
    result = subprocess.run(['git', '-C', str(repo), *argv], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        return ''
    return result.stdout.decode(errors='replace')


def _parent_tree_tests(repo, parent: str, cache: dict[str, set[str]]) -> set[str]:
    """Test files present in the tree AT the parent commit (empty set if parent has none, or lookup fails)."""
    if parent in cache:
        return cache[parent]
    out = _run(repo, ['ls-tree', '-r', '--name-only', parent])
    lines = [line for line in out.split('\n') if line]
    tests = {line for line in lines if is_test(line) and not _is_vendor(line)}
    cache[parent] = tests
    return tests


def commits(repo, *, limit: int | None = None, max_files: int = 60) -> Iterator[dict]:
    """Yield one corpus record per usable commit, newest first."""
    log = _run(repo, ['log', '--no-merges', '--name-only', '-z', f'--format={_FORMAT}', 'HEAD'])
    if not log:
        return

    tree_cache: dict[str, set[str]] = {}
    yielded = 0
    for block in log.split('\x1e'):
        if not block:
            continue
        header, sep, filelist = block.partition('\x00')
        if not sep:
            continue
        parts = header.split(_SEP)
        if len(parts) < 3:
            continue
        sha, parents, subject = parts[0], parts[1], parts[2]
        body = (parts[3].rstrip('\n') if len(parts) > 3 else '')
        parent_list = parents.split()
        if len(parent_list) != 1:
            continue  # no parent (root commit) or already excluded by --no-merges; be defensive anyway
        parent = parent_list[0]

        changed = [f for f in filelist.lstrip('\n').split('\x00') if f]
        if not changed or len(changed) > max_files:
            continue

        source_files, test_files = classify(changed)
        if not source_files or not test_files:
            continue

        candidate_tests = _parent_tree_tests(repo, parent, tree_cache)
        if not candidate_tests:
            continue

        n_test_created = 0
        kept_tests = []
        for path in test_files:
            if path in candidate_tests:
                kept_tests.append(path)
            else:
                n_test_created += 1
        if not kept_tests:
            continue

        candidates = sorted(candidate_tests)
        record = dict(
            repo=str(repo),
            sha=sha,
            parent=parent,
            subject=subject,
            body=body,
            source_files=source_files,
            test_files=sorted(kept_tests),
            all_changed=changed,
            candidates=candidates,
            stats=dict(n_source=len(source_files), n_test=len(kept_tests),
                       n_candidates=len(candidates), n_test_created=n_test_created),
        )
        yield record
        yielded += 1
        if limit is not None and yielded >= limit:
            return


def stats(records: list[dict]) -> dict:
    """Aggregate counts for a dry-run summary."""
    n = len(records)
    total_source = sum(r['stats']['n_source'] for r in records)
    total_test = sum(r['stats']['n_test'] for r in records)
    total_candidates = sum(r['stats']['n_candidates'] for r in records)
    total_created = sum(r['stats']['n_test_created'] for r in records)
    return dict(
        n_commits=n,
        total_source_files=total_source,
        total_test_files=total_test,
        total_candidates=total_candidates,
        total_test_created=total_created,
        avg_candidates=(total_candidates / n) if n else 0.0,
        avg_source_per_commit=(total_source / n) if n else 0.0,
        avg_test_per_commit=(total_test / n) if n else 0.0,
    )
