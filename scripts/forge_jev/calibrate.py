"""Joins shadow-mode routing judgments against actual task outcomes, and REFUSES to
emit a threshold for any tier whose joined sample is too small to say anything.

Two independent, fail-open sources feed the join:

  - <run_dir>/jev.jsonl -- written by client.py. A line with site == 'jev-score-plan'
    and ok == True is evidence that a real shadow-mode Jev call happened in that run
    dir. A run dir with no such line is skipped entirely: its jev-routing.tsv rows
    would be untethered from any real Jev call, and counting them would silently
    launder a run where routing was never actually scored.
  - <run_dir>/jev-routing.tsv -- a sibling file the caller writes alongside jev.jsonl,
    one row per (run_id, task) routing decision: run_id, task, tier, confidence,
    composite. This is the join key source, since jev.jsonl's lines carry no task
    identity of their own.
  - <repo>/.forge/ledger.tsv -- append-only, no header. Multiple rows per (run_id,
    task) collapse to one outcome: the qa role's verdict wins when any qa row exists
    (it is the authoritative judgment of the task), else UNKNOWN; duration comes from
    the dwarf role's rows (max duration_s seen, ignoring '-' and non-numeric values).

Every parse is fail-open: a corrupt line, a short row, a non-numeric field, or a
missing file is skipped and counted, never raised. This is a reporting tool bolted
onto a live pipeline -- it must never be the reason a run breaks.

The refusal is the point: a tier below --min-sample gets no threshold, on principle,
no matter how good its numbers look. Inventing a threshold from 4 samples would be
worse than saying nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import routing, threshold

TIERS = ('low', 'medium', 'high')
DEFAULT_MIN_SAMPLE = 30
# 0.50 .. 0.95 step 0.05 -- the confidence sweep a suggested threshold is drawn from.
CONFIDENCE_SWEEP = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))


def _run_dirs(repo: Path, extra: list[str]) -> list[Path]:
    """Run dirs to consider: every --run-dir given, plus anything under
    <repo>/.forge/runs/*/ if that glob root exists. Missing directories are not an
    error anywhere in this function -- callers filter with is_dir() as they go."""
    dirs = [Path(d) for d in extra]
    glob_root = repo / '.forge' / 'runs'
    if glob_root.is_dir():
        dirs += sorted(p for p in glob_root.iterdir() if p.is_dir())
    # Plus every plan dir that has scored this repo. score-plan records them, because
    # forge keeps plan dirs outside the repo and the glob above therefore matched
    # nothing -- `calibrate --repo X` reported "no data" for repos with plenty. The list
    # lives beside the Jev config, not in the repo: writing it into the working tree
    # made `integrate` refuse to run on a tree forge had dirtied itself.
    try:
        from .cli import runs_registry
        registry = runs_registry(repo)
        if registry.is_file():
            dirs += [Path(line.strip())
                     for line in registry.read_text(errors='replace').splitlines()
                     if line.strip() and not line.startswith('#')]
    except (OSError, ImportError):
        pass
    seen, unique = set(), []
    for path in dirs:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _has_shadow_score(path: Path) -> tuple[bool, int]:
    """True if this run dir's jev.jsonl shows at least one successful jev-score-plan
    judgment. Malformed lines (bad JSON, not an object) are skipped and counted, never
    raised; a missing file is simply "no evidence", not an error."""
    found = False
    skipped = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return found, skipped
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(entry, dict):
            skipped += 1
            continue
        if entry.get('site') == 'jev-score-plan' and entry.get('ok') is True:
            found = True
    return found, skipped


def _read_routing(path: Path) -> tuple[list[dict], int]:
    """Parse jev-routing.tsv: run_id, task, tier, confidence, composite (no header).
    A row with the wrong shape, an unknown tier, or a non-numeric confidence/composite
    is skipped and counted rather than raised."""
    rows: list[dict] = []
    skipped = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows, skipped
    for line in lines:
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) != 5:
            skipped += 1
            continue
        run_id, task, tier, confidence_s, composite_s = parts
        if tier not in TIERS:
            skipped += 1
            continue
        try:
            confidence = float(confidence_s)
            composite = float(composite_s)
        except ValueError:
            skipped += 1
            continue
        rows.append(dict(run_id=run_id, task=task, tier=tier,
                         confidence=confidence, composite=composite))
    return rows, skipped


def _read_ledger(path: Path) -> tuple[dict[tuple[str, str], dict], int]:
    """Collapse ledger.tsv's possibly-many rows per (run_id, task) into one outcome.
    A row with fewer than 10 tab-separated fields, or an unrecognized role, is skipped
    and counted; a non-numeric or '-' duration on a dwarf row is simply not counted
    toward that task's duration (the row itself is still valid)."""
    groups: dict[tuple[str, str], dict] = {}
    skipped = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return groups, skipped
    for line in lines:
        if not line.strip():
            continue
        parts = line.split('\t')
        # At least 10, not exactly 10. The ledger is append-only and has gained a column
        # since (the injectable flag), so an exact-width check would silently skip every
        # row written from now on and quietly report that no calibration data exists.
        if len(parts) < 10:
            skipped += 1
            continue
        ts, run_id, task, role, model, verdict, category, key, text, duration_s = parts[:10]
        if role not in ('dwarf', 'qa'):
            skipped += 1
            continue
        group = groups.setdefault((run_id, task), dict(qa_verdict=None, max_duration=None))
        if role == 'qa' and group['qa_verdict'] is None:
            group['qa_verdict'] = verdict if verdict in ('PASS', 'FAIL', 'UNKNOWN') else 'UNKNOWN'
        if role == 'dwarf' and duration_s != '-':
            try:
                duration = float(duration_s)
            except ValueError:
                duration = None
            if duration is not None and (group['max_duration'] is None or duration > group['max_duration']):
                group['max_duration'] = duration
    outcomes = {key: dict(verdict=g['qa_verdict'] or 'UNKNOWN', duration_s=g['max_duration'])
               for key, g in groups.items()}
    return outcomes, skipped


def _collect(repo: Path, dirs: list[Path]) -> tuple[list[dict], dict]:
    """Gather joined (routing row, outcome) samples across every candidate run dir."""
    routing_rows: list[dict] = []
    n_skipped_jsonl = 0
    n_skipped_routing = 0
    n_run_dirs_used = 0

    for run_dir in dirs:
        if not run_dir.is_dir():
            continue
        routing_path = run_dir / 'jev-routing.tsv'
        if not routing_path.exists():
            continue  # no routing file: nothing to join for this run dir
        has_score, jsonl_skipped = _has_shadow_score(run_dir / 'jev.jsonl')
        n_skipped_jsonl += jsonl_skipped
        if not has_score:
            continue  # routing rows with no corroborating shadow-mode Jev call
        rows, routing_skipped = _read_routing(routing_path)
        n_skipped_routing += routing_skipped
        routing_rows.extend(rows)
        n_run_dirs_used += 1

    outcomes, n_skipped_ledger = _read_ledger(repo / '.forge' / 'ledger.tsv')

    samples = []
    for row in routing_rows:
        outcome = outcomes.get((row['run_id'], row['task']))
        if outcome is None:
            continue  # a routing decision with no matching ledger outcome joins to nothing
        samples.append(dict(tier=row['tier'], confidence=row['confidence'], composite=row['composite'],
                            passed=outcome['verdict'] == 'PASS', duration_s=outcome['duration_s']))

    meta = dict(n_run_dirs_used=n_run_dirs_used, n_skipped_jsonl_lines=n_skipped_jsonl,
               n_skipped_routing_rows=n_skipped_routing, n_skipped_ledger_rows=n_skipped_ledger)
    return samples, meta


# 1.96 = 95% two-sided normal quantile. Hard-coded rather than imported so calibrate.py
# keeps the package's standard-library-only rule without pulling in statistics machinery.
_Z = 1.96


def _wilson_lower_bound(passed: int, total: int) -> float:
    """Lower end of the 95% Wilson score interval for a proportion.

    Wilson rather than the naive proportion because the naive one cannot tell 3/3 from
    30/30: both are 1.0. Wilson gives 0.44 and 0.89, which is exactly the distinction the
    sweep below needs in order not to be fooled by a small lucky subset.
    """
    if total <= 0:
        return 0.0
    phat = passed / total
    denominator = 1 + _Z ** 2 / total
    centre = phat + _Z ** 2 / (2 * total)
    margin = _Z * ((phat * (1 - phat) / total + _Z ** 2 / (4 * total ** 2)) ** 0.5)
    return max(0.0, (centre - margin) / denominator)


def _sweep_threshold(samples: list[dict], overall_pass_rate: float) -> float | None:
    """Lowest swept confidence whose subset BEATS the tier's pass rate with 95% confidence.

    The obvious version of this -- take the first cut whose observed subset pass rate
    meets the overall rate -- overfits badly, and did. Ten candidate cuts over thirty
    samples means the highest cuts retain a handful of points each, and a single passing
    sample at 0.95 scores a perfect 1.0 and wins. That produces a confident-looking
    threshold backed by one observation.

    Requiring the subset's Wilson LOWER bound to clear the overall rate fixes it without
    another magic minimum: the sample-size requirement falls out of the arithmetic, so a
    small subset simply cannot clear the bar however well it did.
    """
    for c in CONFIDENCE_SWEEP:
        subset = [s for s in samples if s['confidence'] >= c]
        if not subset:
            continue
        passed = sum(1 for s in subset if s['passed'])
        if _wilson_lower_bound(passed, len(subset)) >= overall_pass_rate:
            return c
    return None


def _tier_stats(samples: list[dict], min_sample: int) -> dict:
    n = len(samples)
    if n == 0:
        pass_rate = None
        mean_duration = None
        mean_confidence = None
    else:
        pass_rate = sum(1 for s in samples if s['passed']) / n
        durations = [s['duration_s'] for s in samples if s['duration_s'] is not None]
        mean_duration = (sum(durations) / len(durations)) if durations else None
        mean_confidence = sum(s['confidence'] for s in samples) / n

    result = dict(n=n, pass_rate=pass_rate, mean_duration_s=mean_duration,
                  mean_confidence=mean_confidence)

    # The question routing actually needs answered is absolute, not relative: if Jev is
    # allowed to route these tasks, does the work still pass? An earlier version asked
    # whether the confident subset beat the TIER's own pass rate, which is unanswerable
    # by construction -- the subset is contained in the tier, and when confidence
    # clusters tightly (measured: low-tier composites 0.89-0.93) the subset is most of
    # the sample, so it cannot out-perform the whole by a detectable margin. Simulated at
    # 3000 trials, that criterion accepted a genuinely good tier under 1% of the time.
    act_at = threshold('routing_act')
    floor = threshold('calibrate_floor')
    confident = [s for s in samples if s['confidence'] >= act_at]
    confident_passed = sum(1 for s in confident if s['passed'])
    bound = _wilson_lower_bound(confident_passed, len(confident)) if confident else 0.0
    result.update(n_confident=len(confident), confident_pass_rate=(
        confident_passed / len(confident)) if confident else None,
        confident_lower_bound=round(bound, 3), floor=floor, act_at=act_at)

    if n < min_sample:
        result['shortfall'] = min_sample - n
        result['threshold'] = None
        result['meets_floor'] = False
    else:
        result['shortfall'] = None
        result['meets_floor'] = bound >= floor
        # Reported for a human reading the table, never used to decide: searching ten
        # cuts for the best-looking one is how a threshold gets backed by three samples.
        result['threshold'] = _sweep_threshold(samples, pass_rate)
    return result


def run(repo, *, run_dirs: list[str] | None = None, min_sample: int = DEFAULT_MIN_SAMPLE) -> dict:
    repo = Path(repo)
    dirs = _run_dirs(repo, run_dirs or [])
    samples, meta = _collect(repo, dirs)

    tiers = {tier: _tier_stats([s for s in samples if s['tier'] == tier], min_sample) for tier in TIERS}
    any_meets_bar = any(t['n'] >= min_sample for t in tiers.values())

    return dict(repo=str(repo), min_sample=min_sample, n_run_dirs_considered=len(dirs),
               n_samples=len(samples), tiers=tiers, any_meets_bar=any_meets_bar, **meta)


def _fmt(value, digits=3) -> str:
    return 'n/a' if value is None else f'{value:.{digits}f}'


def report(summary: dict) -> str:
    lines = [f"Jev calibration -- repo={summary['repo']} min_sample={summary['min_sample']}"]
    lines.append(f"run dirs considered: {summary['n_run_dirs_considered']}  "
                f"run dirs used: {summary.get('n_run_dirs_used', 0)}  "
                f"joined samples: {summary['n_samples']}")
    skipped_total = (summary.get('n_skipped_jsonl_lines', 0) + summary.get('n_skipped_routing_rows', 0)
                     + summary.get('n_skipped_ledger_rows', 0))
    if skipped_total:
        lines.append(f"skipped {skipped_total} malformed row(s) across inputs "
                    f"(jsonl={summary.get('n_skipped_jsonl_lines', 0)}, "
                    f"routing={summary.get('n_skipped_routing_rows', 0)}, "
                    f"ledger={summary.get('n_skipped_ledger_rows', 0)})")
    lines.append('')

    for tier in TIERS:
        t = summary['tiers'][tier]
        if t['n'] < summary['min_sample']:
            lines.append(f"{tier}: {t['n']} of {summary['min_sample']} samples -- no threshold "
                        f"emitted (need {t['shortfall']} more)")
            continue
        lines.append(f"{tier}: n={t['n']} pass_rate={_fmt(t['pass_rate'])} "
                    f"mean_duration_s={_fmt(t['mean_duration_s'], 1)} "
                    f"mean_confidence={_fmt(t['mean_confidence'])}")
        lines.append(f"  at confidence >= {_fmt(t.get('act_at'), 2)}: "
                    f"n={t.get('n_confident')} "
                    f"pass_rate={_fmt(t.get('confident_pass_rate'))} "
                    f"95% lower bound={_fmt(t.get('confident_lower_bound'))} "
                    f"(needs >= {_fmt(t.get('floor'), 2)})")
        if t.get('meets_floor'):
            lines.append('  MEETS the floor -- `--write` would let routing act on this tier')
        else:
            # The lower bound rises with sample size, so "not yet" and "never" look the
            # same here. Say which one it is.
            lines.append('  below the floor -- routing stays advisory for this tier. More '
                        'samples raise the bound; a genuinely low pass rate will not.')
        if t['threshold'] is not None:
            lines.append(f"  (informational: swept threshold {t['threshold']:.2f} -- "
                        'not used to decide)')
    return '\n'.join(lines)


def write_calibration(repo, summary: dict) -> tuple[bool, str]:
    """Record which tiers earned the right to be acted on. Returns (written, reason).

    Only tiers that both met the sample bar AND produced a threshold are stored, so a
    repo can end up calibrated for `low` and advisory for `high` -- which is the honest
    outcome when most real tasks are hard and the cheap tier is rarely exercised.

    Writing is a separate, explicit step (`--write`) rather than a side effect of
    reporting, because this file is the only thing standing between `--jev-act` and Jev
    changing which model spends the user's quota.
    """
    tiers = {name: dict(n=stats['n'], n_confident=stats.get('n_confident'),
                        pass_rate=stats['pass_rate'],
                        lower_bound=stats.get('confident_lower_bound'),
                        act_at=stats.get('act_at'))
             for name, stats in summary.get('tiers', {}).items()
             if stats.get('meets_floor') and not stats.get('shortfall')}
    if not tiers:
        return False, ('no tier both reached the sample bar and held its pass rate above '
                       'the floor with 95% confidence')
    path = Path(repo) / '.forge' / routing.CALIBRATION_FILE
    payload = dict(calibrated=True, min_sample=summary.get('min_sample'),
                   n_samples=summary.get('n_samples'), tiers=tiers,
                   written_at=time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
        temporary.replace(path)
    except OSError as error:
        return False, f'could not write {path}: {error}'
    return True, str(path)
