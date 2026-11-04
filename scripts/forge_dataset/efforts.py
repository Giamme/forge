"""A note when a spec is measurably slower than a lower effort of the same model.

`slow_note` is advisory. It never changes the spec it was given, and the callers that
print it (doctor --spec, and the plan table) do not change an exit code because of it.

The dataset decides when it is on and both this effort and a lower one of the same model
have at least five first attempts. Each lower effort is checked, nearest first. The note
names the nearest one this effort is at least twice as slow as, with a first-try pass
rate no better. A nearer pair that has enough data but fails that rule does not hide a
farther pair that meets it. When at least one pair has enough data and none meet the
rule, there is no note: measured data overrides the built-in table. Otherwise one
built-in row applies, `haiku:max` against `haiku:xhigh` on the claude harness's model
(#86, 2026-10-28).

    python3 -m forge_dataset.efforts SPEC

prints the note, or nothing. Run it from `scripts/`. `plan_notes` gives the plan table's
lines (`forge-dataset.py slow-notes PLAN`, printed by `forge-parallel.sh plan`), kept out
of forge-plan-check.py: that file builds the planner's send-back prompt, and nothing that
builds a prompt reads the dataset.
"""
from __future__ import annotations

import math
import statistics
import subprocess
import sys
from pathlib import Path

from . import SKILL, connect, dataset_dir, enabled
from .export import Runs, Store
from .forecast import registry_models, resolve
from .routing import tasks as routing_tasks

MIN_TASKS = 5
# No measured pair had five first attempts on both sides, so the built-in table may speak.
_SKIP = object()

# alias, effort, the lower effort, and the harness whose model id the row belongs to.
# openclaude's haiku is that same model; opencode's is not, and does not match.
_TABLE = (
    dict(alias='haiku', effort='max', lower='xhigh', harness='claude',
         passes=(7, 7), lower_passes=(4, 4), minutes=7.8, lower_minutes=1.9,
         source='#86, 2026-10-28'),
)


def _parts(spec):
    """(alias, effort or None, harness or None) of alias[:effort[:harness]]."""
    parts = str(spec or '').split(':')
    alias = parts[0]
    effort = parts[1] if len(parts) > 1 and parts[1] else None
    harness = parts[2] if len(parts) > 2 and parts[2] else None
    return alias, effort, harness


def _efforts(spec) -> list:
    """Efforts the pairing accepts, weakest first. Empty when dispatch cannot say."""
    try:
        done = subprocess.run(
            ['bash', str(SKILL / 'scripts' / 'forge-dispatch.sh'), 'efforts', '--spec', spec],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _runs(store):
    """A Runs over `store` (a Runs, a Store, or a connection), or over the dataset.

    Reading the default store does not create one: a machine with no dataset yet is
    simply a machine with no measured rows.
    """
    if isinstance(store, Runs):
        return store
    if isinstance(store, Store):
        return Runs(store)
    if store is not None:
        return Runs(Store(store))
    if not (dataset_dir() / 'dataset.sqlite').is_file():
        return None
    db = connect()
    try:
        return Runs(Store(db))
    finally:
        db.close()


def _arm(records, model_id, effort) -> dict:
    """n (first attempts with an outcome), passes, and the median of runtime_s/attempts."""
    group = [r for r in records if r.get('model_id') == model_id and r.get('effort') == effort
             and r.get('first_pass') is not None]
    samples = []
    for record in group:
        attempts = record.get('attempts') or 0
        runtime = record.get('runtime_s')
        if isinstance(runtime, bool) or isinstance(attempts, bool):
            continue
        if isinstance(runtime, (int, float)) and isinstance(attempts, (int, float)) and attempts > 0:
            if math.isfinite(runtime) and math.isfinite(attempts):
                samples.append(runtime / attempts)
    return dict(n=len(group), k=sum(1 for r in group if r['first_pass']),
                median=statistics.median(samples) if samples else None)


def _line(alias, effort, lower, passes, lower_passes, minutes, lower_minutes, source) -> str:
    k, n = passes
    k2, n2 = lower_passes
    ratio = int(math.floor(minutes / lower_minutes + 0.5))
    return ('%s:%s: no measured gain over %s:%s (first try %d/%d vs %d/%d) at about %dx the time '
            '(%.1f vs %.1f min; %s). The spec is unchanged.'
            % (alias, effort, alias, lower, k, n, k2, n2, ratio, minutes, lower_minutes, source))


def _from_dataset(records, alias, effort, model_id, lowers):
    """The dataset's note, None when measured pairs do not qualify, or _SKIP.

    `lowers` is weakest first, so the walk starts at the nearest. The first lower
    effort that meets the rule is the one named. A pair with enough data that fails
    the rule does not stop the walk: a farther effort may still qualify. If any pair
    had enough data and none met the rule, return None so the built-in table stays quiet.
    """
    hi = _arm(records, model_id, effort)
    measured = False
    for lower in reversed(lowers):
        lo = _arm(records, model_id, lower)
        if hi['n'] < MIN_TASKS or lo['n'] < MIN_TASKS:
            continue
        measured = True
        slower = (hi['median'] is not None and lo['median'] is not None and lo['median'] > 0
                  and hi['median'] >= 2 * lo['median'])
        no_better = hi['k'] * lo['n'] <= lo['k'] * hi['n']
        if slower and no_better:
            return _line(alias, effort, lower, (hi['k'], hi['n']), (lo['k'], lo['n']),
                         hi['median'] / 60.0, lo['median'] / 60.0,
                         'forge dataset, %d runs' % (hi['n'] + lo['n']))
    return None if measured else _SKIP


def _from_table(alias, effort, model_id, lowers, registry):
    for row in _TABLE:
        if (alias, effort) != (row['alias'], row['effort']) or row['lower'] not in lowers:
            continue
        same = (registry.get(alias) or {}).get(row['harness'])
        if not same or model_id != same:
            continue
        return _line(alias, effort, row['lower'], row['passes'], row['lower_passes'],
                     row['minutes'], row['lower_minutes'], row['source'])
    return None


def slow_note(spec, *, store=None):
    """One note when `spec` is a measured slowdown, else None. Never raises."""
    try:
        alias, effort, _harness = _parts(spec)
        if not alias or not effort:
            return None
        registry = registry_models()
        model_id, effort = resolve(spec, registry)
        if not model_id or not effort:
            return None
        ladder = _efforts(spec)
        if effort not in ladder:
            return None
        lowers = ladder[:ladder.index(effort)]
        if not lowers:
            return None
        # The flag gates the dataset, an injected store included. No file yet is not an error:
        # there is nothing measured, so the built-in table still applies.
        if enabled():
            runs = _runs(store)
            if runs is not None:
                found = _from_dataset(routing_tasks(runs), alias, effort, model_id, lowers)
                if found is not _SKIP:
                    return found
        return _from_table(alias, effort, model_id, lowers, registry)
    except Exception:
        return None


def plan_notes(plan) -> list:
    """`note:` lines for the distinct slow specs in a plan's dwarf and qa columns.

    After routing, tasks.tsv holds concrete specs. The same spec twice is one line. A
    missing table prints nothing.
    """
    from .harvest import tasks_rows
    seen, out = set(), []
    for row in tasks_rows(Path(plan) / 'tasks.tsv').values():
        for spec in (row.get('dwarf'), row.get('qa')):
            spec = (spec or '').strip()
            if not spec or spec == '-' or spec in seen:
                continue
            seen.add(spec)
            text = slow_note(spec)
            if text:
                out.append('note: ' + text)
    return out


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print('usage: python3 -m forge_dataset.efforts SPEC', file=sys.stderr)
        return 2
    text = slow_note(args[0])
    if text:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
