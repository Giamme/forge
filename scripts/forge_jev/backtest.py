"""Replays git history against Jev to score test-selection and drift-prediction thresholds.

A commit's test changes are a LOWER BOUND on relevant tests: a human may touch fewer
tests than a change could break, so high recall against this label is necessary, not
sufficient -- it never proves full coverage. Co-change labels also encode one team's
testing habits, so numbers from one repo do not transfer to another without
re-measuring there. Both caveats are repeated in report() so they travel with the numbers.

Budget discipline: execute=False (the default) makes zero API calls -- it only estimates
how many requests a real run would cost. When execute=True, every request is appended to
a lock-protected ledger under out_dir *before* it is issued, and max_requests is a hard
cap; once hit, remaining records are marked incomplete rather than half-scored. A record
whose Jev calls fail is excluded from metrics entirely -- treating a failed chunk as "not
relevant" would manufacture artificial precision.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import time

from . import load_config, threshold, write_json
from .client import ask, noul
from .corpus import VENDOR_PATTERN, commits, is_test
from .questions import drift_prediction, test_relevance

MAX_QUESTIONS_PER_REQUEST = 100
DRIFT_CANDIDATE_CAP = 200  # capped universe of tracked non-test files considered for drift
DEFAULT_THRESHOLDS = tuple(round(0.05 * i, 2) for i in range(1, 20))  # 0.05 .. 0.95 step 0.05

LOWER_BOUND_NOTE = ('Test-touch labels are a LOWER BOUND on relevant tests: a human may touch '
                    'fewer tests than a change could break. High recall here is necessary, not '
                    'sufficient proof of coverage.')
NO_TRANSFER_NOTE = ('Co-change labels encode one team\'s testing habits and repo layout. Numbers '
                    'from this repo do not transfer to another repo without re-measuring there.')
# The single most optimistic thing about this whole method, so it travels with every number.
POST_HOC_NOTE = ('Commit messages are written AFTER the work and often name the very files or '
                 'behaviour that changed. A Forge task prompt is written before. These scores are '
                 'therefore an UPPER BOUND on live performance.')
DRIFT_CAP_NOTE = ('Drift candidates are the parent tree\'s tracked non-test files, sorted and '
                  'truncated to the cap -- a commit whose real files fall outside that slice is '
                  'scored against a universe that cannot contain them.')


def notes(capability: str) -> list[str]:
    """Caveats that apply to this capability. Drift's label (the commit's actual file set)
    is complete, so the test-selection lower-bound note would be false there."""
    if capability == 'drift':
        return [POST_HOC_NOTE, NO_TRANSFER_NOTE, DRIFT_CAP_NOTE]
    return [LOWER_BOUND_NOTE, POST_HOC_NOTE, NO_TRANSFER_NOTE]


def _index_candidates(candidates: list[str]) -> list[tuple[str, str]]:
    """Assign stable q<N> ids to candidate paths -- a path itself is not a safe question id."""
    return [(f'q{i}', path) for i, path in enumerate(candidates)]


def _chunk(pairs: list[tuple[str, str]], size: int = MAX_QUESTIONS_PER_REQUEST):
    for i in range(0, len(pairs), size):
        yield pairs[i:i + size]


def _ledger_append(out_dir: Path, entry: dict) -> None:
    path = out_dir / 'requests.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(entry) + '\n')
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _drift_candidates(repo, parent: str, cache: dict[str, list[str]], cap: int = DRIFT_CANDIDATE_CAP) -> list[str]:
    """Repo's tracked non-test files at `parent`, capped and sorted for determinism."""
    if parent in cache:
        return cache[parent]
    try:
        result = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', parent],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        cache[parent] = []
        return []
    paths = []
    if result.returncode == 0:
        for line in result.stdout.decode(errors='replace').split('\n'):
            if line and not is_test(line) and not VENDOR_PATTERN.search(line):
                paths.append(line)
    paths = sorted(paths)[:cap]
    cache[parent] = paths
    return paths


def _estimate_requests(n_candidates: int) -> int:
    if n_candidates <= 0:
        return 0
    return -(-n_candidates // MAX_QUESTIONS_PER_REQUEST)  # ceil division


def _score(sha: str, candidates: list[str], labels: list[str], answers: dict[str, float]) -> dict:
    """Package one record's raw probabilities. Threshold selection happens later in sweep()
    so re-tuning a threshold never costs another API call. `labels` is not part of the
    literal contract example but sweep() cannot compute recall without knowing which
    candidate paths are ground truth, so it travels alongside probabilities here."""
    return dict(sha=sha, n_candidates=len(candidates), n_labeled=len(labels),
                labels=sorted(set(labels)), probabilities=dict(answers), incomplete=False)


def score_tests(record: dict, answers: dict[str, float]) -> dict:
    """Score one test-selection record's answers against its candidates and test_files label."""
    return _score(record['sha'], record['candidates'], record.get('test_files', []), answers)


def _evaluate_record(record, *, state, candidates, question_fn, config, out_dir, run_dir,
                     site, max_requests, request_count) -> tuple[dict, bool]:
    """Ask Jev about every candidate for one record, chunked and budget-gated. Returns
    (path->probability, incomplete). A failed or budget-skipped chunk marks the whole
    record incomplete -- its answered paths are still returned for inspection but the
    record is excluded from metrics by the caller."""
    pairs = _index_candidates(candidates)
    probabilities: dict[str, float] = {}
    incomplete = False
    for chunk in _chunk(pairs):
        if request_count[0] >= max_requests:
            incomplete = True
            break
        questions = {qid: question_fn(path) for qid, path in chunk}
        request_count[0] += 1
        _ledger_append(out_dir, dict(at=time.time(), sha=record['sha'], site=site,
                                     n_questions=len(questions), request_no=request_count[0]))
        result = ask(state, questions, site=site, run_dir=run_dir, config=config)
        if result is None:
            incomplete = True
            break
        for qid, path in chunk:
            value = noul(result, qid)
            if value is not None:
                probabilities[path] = value
    return probabilities, incomplete


def run(repo, *, limit=None, execute=False, max_requests=200, out_dir=None,
       capability='tests', config=None) -> dict:
    if capability not in ('tests', 'drift'):
        raise ValueError("capability must be 'tests' or 'drift'")
    config = config or load_config()
    out_path = Path(out_dir) if out_dir is not None else Path(tempfile.mkdtemp(prefix='forge-jev-backtest-'))
    out_path.mkdir(parents=True, exist_ok=True)

    drift_cache: dict[str, list[str]] = {}
    question_fn = test_relevance if capability == 'tests' else drift_prediction
    site = 'jev-backtest:' + capability

    n_records = 0
    total_candidates = 0
    estimated_requests = 0
    results: list[dict] = []
    request_count = [0]
    budget_exhausted = False

    for record in commits(repo, limit=limit):
        if capability == 'tests':
            candidates = record['candidates']
            labels = record['test_files']
        else:
            candidates = _drift_candidates(repo, record['parent'], drift_cache)
            labels = sorted(set(record['all_changed']) & set(candidates))

        n_records += 1
        total_candidates += len(candidates)
        estimated_requests += _estimate_requests(len(candidates))

        if not execute:
            continue

        if budget_exhausted or request_count[0] >= max_requests:
            budget_exhausted = True
            results.append(_score(record['sha'], candidates, labels, {}) | dict(incomplete=True))
            continue

        # Label never enters the request state: only the task text and changed source
        # files are sent, never test_files/all_changed.
        state = dict(task=(record['subject'] + '\n' + record['body']).strip(),
                     changed_files=record['source_files'])
        probabilities, incomplete = _evaluate_record(
            record, state=state, candidates=candidates, question_fn=question_fn, config=config,
            out_dir=out_path, run_dir=out_path, site=site, max_requests=max_requests,
            request_count=request_count)
        if request_count[0] >= max_requests:
            budget_exhausted = True
        scored = _score(record['sha'], candidates, labels, probabilities)
        scored['incomplete'] = incomplete
        results.append(scored)

    summary = dict(
        capability=capability,
        repo=str(repo),
        execute=execute,
        n_records=n_records,
        estimated_requests=estimated_requests,
        avg_candidates=(total_candidates / n_records) if n_records else 0.0,
        max_requests=max_requests,
        max_questions_per_request=MAX_QUESTIONS_PER_REQUEST,
        out_dir=str(out_path),
        notes=notes(capability),
    )
    if capability == 'drift':
        summary['drift_candidate_cap'] = DRIFT_CANDIDATE_CAP
    if execute:
        summary['requests_used'] = request_count[0]
        summary['n_incomplete'] = sum(1 for r in results if r['incomplete'])
        summary['results'] = results
        summary['sweep'] = sweep(results)

    write_json(out_path / 'backtest.json', summary)
    return summary


def sweep(results: list[dict], thresholds=None) -> list[dict]:
    """Aggregate scored records across a sweep of thresholds. Incomplete records are
    dropped entirely -- an all-failed run must report zero scored records, never
    manufactured perfect (or perfectly bad) precision."""
    thresholds = list(thresholds) if thresholds is not None else list(DEFAULT_THRESHOLDS)
    usable = [r for r in results if not r.get('incomplete')]
    n_incomplete = len(results) - len(usable)

    rows = []
    for t in thresholds:
        total_labeled = 0
        total_labeled_selected = 0
        total_candidates = 0
        total_selected = 0
        per_record_recalls = []
        full_recall_records = 0
        selected_counts = []

        for r in usable:
            probs = r['probabilities']
            labels = set(r.get('labels', []))
            selected = {p for p, v in probs.items() if v is not None and v >= t}
            n_labeled = len(labels)
            n_labeled_selected = len(labels & selected)

            total_labeled += n_labeled
            total_labeled_selected += n_labeled_selected
            total_candidates += r['n_candidates']
            total_selected += len(selected)
            selected_counts.append(len(selected))
            if n_labeled:
                per_record_recalls.append(n_labeled_selected / n_labeled)
                if n_labeled_selected == n_labeled:
                    full_recall_records += 1

        n_recall_records = len(per_record_recalls)
        rows.append(dict(
            threshold=t,
            micro_recall=(total_labeled_selected / total_labeled) if total_labeled else None,
            # Precision is the headline for drift: a warning that fires on files nobody
            # edits trains people to ignore it. For test selection it is expected to be
            # low by design, since over-selecting only costs runtime.
            precision=(total_labeled_selected / total_selected) if total_selected else None,
            macro_recall=(sum(per_record_recalls) / n_recall_records) if n_recall_records else None,
            reduction=(1 - total_selected / total_candidates) if total_candidates else None,
            records_with_full_recall=(full_recall_records / n_recall_records) if n_recall_records else None,
            mean_selected=(sum(selected_counts) / len(selected_counts)) if selected_counts else None,
            n_records=len(usable),
            n_incomplete=n_incomplete,
        ))
    return rows


def _fmt(value, digits=3) -> str:
    return 'n/a' if value is None else f'{value:.{digits}f}'


def report(summary: dict) -> str:
    lines = [f"Jev backtest -- capability={summary.get('capability')} repo={summary.get('repo')}"]
    lines += summary.get('notes') or notes(summary.get('capability', 'tests'))
    lines.append('')

    if not summary.get('execute'):
        lines.append(
            f"DRY RUN -- no API calls made. {summary['n_records']} record(s), "
            f"avg {summary['avg_candidates']:.1f} candidates/record, "
            f"~{summary['estimated_requests']} request(s) estimated at "
            f"{summary['max_questions_per_request']} questions/request.")
        return '\n'.join(lines)

    sweep_rows = summary.get('sweep', [])
    lines.append(f"requests used: {summary.get('requests_used', 0)}  "
                f"incomplete records: {summary.get('n_incomplete', 0)}")
    lines.append(f"{'threshold':>9} | {'micro_recall':>12} | {'macro_recall':>12} | "
                f"{'precision':>9} | {'reduction':>9} | {'full_recall':>11} | {'mean_sel':>8}")
    best = None
    for row in sweep_rows:
        micro = row['micro_recall']
        lines.append(f"{row['threshold']:>9.2f} | {_fmt(micro):>12} | {_fmt(row['macro_recall']):>12} | "
                    f"{_fmt(row['precision']):>9} | "
                    f"{_fmt(row['reduction']):>9} | {_fmt(row['records_with_full_recall']):>11} | "
                    f"{_fmt(row['mean_selected'], 1):>8}")
        if micro is not None and micro >= 0.95:
            best = row['threshold']  # ascending order: last hit is the highest qualifying threshold

    # The two capabilities are tuned against opposite errors, so they get different
    # headlines: a missed test is a missed regression, while a drift warning on files
    # nobody edits is noise that trains people to ignore the warning.
    if summary.get('capability') == 'drift':
        warn = threshold('gate_warn')
        row = min(sweep_rows, key=lambda r: abs(r['threshold'] - warn), default=None)
        if row is not None:
            lines.append(f"\nDrift headline -- precision at the warn threshold "
                        f"{row['threshold']:.2f}: {_fmt(row['precision'])} "
                        f"(recall {_fmt(row['micro_recall'])}).")
    elif best is not None:
        lines.append(f"\nHighest threshold holding micro recall >= 0.95: {best:.2f}")
    else:
        lines.append('\nNo threshold in this sweep holds micro recall >= 0.95.')
    return '\n'.join(lines)
