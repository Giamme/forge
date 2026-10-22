#!/usr/bin/env python3
"""Routing helpers for `plan` and retries: exploratory picks with their odds, and escalation.

Every task in the run history ran on the model the router chose for it, so the data says
how that model did and nothing about how another would have done on the same task. A
small, recorded share of random assignments is what lets that be estimated later without
bias (inverse-propensity weighting needs the probability each choice had).

  forge-route.py explore --plan P --task ID --tier T --epsilon E --default SPEC --pools FILE
      Called by `plan` for a row the user left to forge, after the pool's own turn picked
      SPEC (the pool counters advance exactly as without exploration). The candidates are
      exactly two: SPEC and its neighbour, the model one tier away in the user's own pools
      (the next cheaper tier for medium and high, the next stronger for low). With
      probability 1-E it keeps SPEC; with probability E it picks uniformly between the two.
      Prints the spec to use. Appends the decision to <plan>/tasks/<id>/route.jsonl (never
      rewritten) and marks a changed pick in tasks/<id>/route.explored for the table. A row
      with no neighbour is not explored and records nothing.

  forge-route.py escalation --plan P --task ID [--check]
      The retry policy: after <plan>/escalate_after (default 3; 0 = off) failed attempts on
      one spec, the next attempt goes to the next tier up of the user's pools instead of
      repeating the same dispatch. Prints that spec when one is due. When none exists, writes
      tasks/<id>/escalation.stop and split.suggested and exits 3. `--check` (run after every
      attempt) also records a due escalation in escalation.next, and clears the markers once
      the streak no longer holds. A pending tasks/<id>/escalation.pin (a human's one-shot
      choice, applied by prepare_retry) is what `--check` reports as the next model instead
      (escalation.next = its spec), whatever escalate_after says, unless the next retry is a
      QA-only resume or the plan is Fractal (pins never apply there). Nothing has applied the
      pin yet, so the policy's own stop and split markers stay as the policy decided them
      beneath it: they are what holds again if the pin is withdrawn.
      The marker writes take <plan>/control.lock and re-read escalate_after inside it: a
      policy decision made before a human switched the policy off writes nothing.

The propensity of the chosen spec is (1-E)*[chosen == SPEC] + E/2. Forge never picks a
model the user did not name: both helpers read only the pools `plan` stored in
fractal-routing.json, and "stronger" means only "in a higher tier of those pools" --
registry.tsv has no cost or capability column to say more. FORGE_ROUTE_EXPLORE_SEED makes
the draws reproducible (tests only). Any failure prints SPEC (explore) or nothing
(escalation): neither ever changes a run by breaking.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import random
import secrets
import stat
import sys
import tempfile
import time
from pathlib import Path

TIERS = ('low', 'medium', 'high')
SCRIPTS = Path(__file__).resolve().parent
REGISTRY = SCRIPTS.parent / 'registry.tsv'
FAILED = ('FAIL', 'TIMEOUT')                       # a judgment against the spec that ran
NEUTRAL = ('INFRA', 'UNKNOWN', 'ERROR', 'RUNNING', 'INTERRUPTED')   # say nothing about it
DEFAULT_ESCALATE_AFTER = 3
LOCK_WAIT = 5.0                                    # seconds to wait for P/control.lock


def rng_for(task: str):
    seed = os.environ.get('FORGE_ROUTE_EXPLORE_SEED')
    return random.Random('%s:%s' % (seed, task)) if seed else secrets.SystemRandom()


def row_inputs(plan: Path, task: str) -> str:
    """The sha of what the task is, so a decision can be told apart from one about a rewritten task."""
    row = ''
    try:
        for line in (plan / 'tasks.tsv').read_text().splitlines():
            cells = line.split('\t')
            if cells and cells[0] == task:
                row = '\t'.join(cells[i] for i in (1, 3, 6) if i < len(cells))   # deps, files, title
                break
    except OSError:
        pass
    try:
        prompt = (plan / 'tasks' / task / 'prompt.md').read_text()
    except OSError:
        prompt = ''
    return hashlib.sha256(('%s\0%s' % (row, prompt)).encode()).hexdigest()[:16]


# --- the user's ladder -------------------------------------------------------------

def registry_models(path: Path = REGISTRY) -> dict:
    """{alias: {harness: model id}} from registry.tsv; the first row of an alias is its default."""
    out = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) >= 3:
            out.setdefault(cells[0], {}).setdefault(cells[1], cells[2])
    return out


def canonical(spec: str, registry: dict) -> str:
    """The model a spec runs, so `sol` and `sol:xhigh` count as one model and `gpt-6.1-sol` too."""
    parts = str(spec).split(':')
    alias = parts[0]
    harness = parts[2] if len(parts) > 2 else None
    rows = registry.get(alias)
    if not rows:
        return alias                       # a literal model id is its own identity
    if harness and harness in rows:
        return rows[harness]
    return next(iter(rows.values()))


def arm(spec: str, registry: dict) -> tuple:
    """What exploration compares on: the model and the effort as written.

    `sol` and `sol:xhigh` are one model but two arms, so pools that differ only in effort
    still explore and the routing table measures whether the effort was needed. A bare
    spec and one naming the effort its harness defaults to count as two arms: a wasted
    comparison at worst, never a wrong one.
    """
    parts = str(spec).split(':')
    return canonical(spec, registry), parts[1] if len(parts) > 1 else ''


def load_pools(path) -> dict:
    """{tier: [spec, ...]} for low/medium/high. `any` (plain --dwarf) has no rank, so it is left out."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for tier in TIERS:
        specs = data.get(tier) if isinstance(data, dict) else None
        if isinstance(specs, list):
            specs = [str(s).strip() for s in specs if str(s).strip()]
            if specs:
                out[tier] = specs
    return out


def rank_of(spec: str, pools: dict, registry: dict):
    """The highest tier whose pool runs this spec's model, or None when no pool does."""
    model = canonical(spec, registry)
    ranks = [i for i, tier in enumerate(TIERS) for s in pools.get(tier, ()) if canonical(s, registry) == model]
    return max(ranks) if ranks else None


def _aligned(spec: str, pool_of_spec: list, other: list, key):
    """The spec in `other` at this spec's position in its own pool, else the first distinct one.

    Users list one family per position (`--dwarf-low luna,haiku --dwarf-medium sol,sonnet`),
    so position keeps a comparison within a family without forge guessing what a family is.
    `key` says what distinct means: the arm for exploration, the model for escalation.
    """
    own = key(spec)
    distinct = [s for s in other if key(s) != own]
    if not distinct:
        return None
    try:
        i = [key(s) for s in pool_of_spec].index(own)
    except ValueError:
        return distinct[0]
    pick = other[i % len(other)]
    return pick if key(pick) != own else distinct[0]


def neighbour(spec: str, tier: str, pools: dict, registry: dict):
    """The exploration alternative: the nearest cheaper tier for medium/high, the nearest stronger for low."""
    if tier not in TIERS:
        return None
    i = TIERS.index(tier)
    order = TIERS[i + 1:] if i == 0 else tuple(reversed(TIERS[:i]))
    for other in order:
        if pools.get(other):
            pick = _aligned(spec, pools.get(tier) or [spec], pools[other], lambda s: arm(s, registry))
            if pick:
                return pick
    return None


def stronger(spec: str, pools: dict, registry: dict):
    """The escalation target: a distinct model in the next tier up, or (None, why)."""
    rank = rank_of(spec, pools, registry)
    if rank is None:
        return None, 'its model is in none of this plan\'s tier pools, so there is no configured escalation'
    own = pools.get(TIERS[rank]) or [spec]
    for tier in TIERS[rank + 1:]:
        pick = _aligned(spec, own, pools.get(tier) or [], lambda s: canonical(s, registry))
        if pick:
            return pick, None
    return None, 'it is already in the highest tier of this plan\'s pools (%s)' % TIERS[rank]


# --- exploration -------------------------------------------------------------------

def explore(args) -> str:
    epsilon = float(args.epsilon)
    if not 0 < epsilon <= 1:
        return args.default
    alternative = neighbour(args.default, args.tier, load_pools(args.pools), registry_models())
    if not alternative:
        return args.default
    candidates = [args.default, alternative]
    rng = rng_for(args.task)
    draw = rng.random()
    chosen = rng.choice(candidates) if draw < epsilon else args.default
    propensity = (1 - epsilon) * (chosen == args.default) + epsilon / len(candidates)
    plan = Path(args.plan)
    tdir = plan / 'tasks' / args.task
    tdir.mkdir(parents=True, exist_ok=True)
    decision = dict(at=time.time(), task=args.task, tier=args.tier, epsilon=epsilon, candidates=candidates,
                    default=args.default, chosen=chosen, explored=chosen != args.default,
                    propensity=round(propensity, 6), inputs=row_inputs(plan, args.task))
    decision['decision'] = hashlib.sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()[:16]
    with open(tdir / 'route.jsonl', 'a') as log:      # one line per resolution, never rewritten
        log.write(json.dumps(decision, sort_keys=True) + '\n')
    marker = tdir / 'route.explored'
    if decision['explored']:
        marker.write_text(chosen + '\n')
    elif marker.exists():
        marker.unlink()
    return chosen


# --- escalation --------------------------------------------------------------------

def _dwarf_of(plan: Path, task: str) -> str:
    for line in (plan / 'tasks.tsv').read_text().splitlines():
        cells = line.split('\t')
        if not line.startswith('#') and cells and cells[0] == task and len(cells) > 4:
            return cells[4]
    return ''


def failed_streak(rows: list, current: str, registry: dict) -> int:
    """Distinct trailing attempt numbers that failed on `current`'s model.

    rows: (attempt, status, spec-or-None) oldest first. A row without a spec predates the
    column and is taken as the current spec. Infra stops, missing verdicts and forge errors
    judge nothing and are skipped; a pass, a merge or another model ends the streak.
    """
    model = canonical(current, registry)
    seen = set()
    for attempt, status, spec in reversed(rows):
        if status in NEUTRAL:
            continue
        if spec and canonical(spec, registry) != model:
            break
        if status not in FAILED:
            break
        seen.add(attempt)
    return len(seen)


def attempt_rows(tdir: Path) -> list:
    rows = []
    try:
        lines = (tdir / 'attempts.tsv').read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        cells = line.split('\t')
        if len(cells) >= 3 and cells[1].isdigit():
            rows.append((int(cells[1]), cells[2], cells[4] if len(cells) > 4 and cells[4] else None))
    return rows


def _setting(plan: Path) -> int:
    try:
        value = (plan / 'escalate_after').read_text().strip()
        return int(value) if value else DEFAULT_ESCALATE_AFTER
    except (OSError, ValueError):
        return DEFAULT_ESCALATE_AFTER


def _fractal(plan: Path) -> bool:
    try:
        return bool(json.loads((plan / 'fractal-selection.json').read_text()).get('enabled'))
    except (OSError, ValueError, AttributeError):
        return False


# --- live-control files ------------------------------------------------------------
# Markers are shared with `forge control` and the scheduler, so they are written the way
# every writer of the plan's control files writes: a task id the scheduler would accept, a
# real directory chain under the canonical plan dir, a regular file as the target, a temp
# file plus rename, and P/control.lock held around the check-and-write.

class Busy(Exception):
    """P/control.lock stayed held for LOCK_WAIT seconds."""


def _task_dir(plan: Path, task: str):
    """tasks/<task> under the canonical plan dir, or None when it is not a real directory."""
    if not task or task in ('.', '..') or '/' in task or '\0' in task:
        return None
    tdir = plan / 'tasks' / task
    try:
        if not stat.S_ISDIR(os.lstat(tdir).st_mode) or os.path.realpath(tdir) != str(tdir):
            return None
    except OSError:
        return None
    return tdir


@contextlib.contextmanager
def _control_lock(plan: Path):
    fd = os.open(str(plan / 'control.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'control.lock is not a regular file')
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise Busy('control.lock is held') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


MARKERS = ('escalation.stop', 'split.suggested', 'escalation.next')


def _clear(tdir: Path, *names) -> None:
    for name in names:
        if _regular(tdir / name):
            try:
                (tdir / name).unlink()
            except OSError:
                pass


def _write_marker(tdir: Path, name: str, text: str) -> None:
    target = tdir / name
    if os.path.lexists(target) and not _regular(target):
        raise OSError(errno.EINVAL, '%s is not a regular file' % name)
    fd, tmp = tempfile.mkstemp(dir=str(tdir), prefix='.%s.' % name)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pin(tdir: Path):
    """The pending pin's spec, or None. One line, one token: it goes into a TSV column."""
    path = tdir / 'escalation.pin'
    if not _regular(path):
        return None
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    spec = lines[0].strip() if lines else ''
    if not spec or spec in ('-', 'UNASSIGNED') or any(c.isspace() or ord(c) < 32 for c in spec):
        return None
    return spec


def _qa_next(tdir: Path) -> bool:
    """True when the task's next retry only re-runs QA, so it cannot apply a pin.

    That is a `resume` marker (a QA-stage infrastructure stop), or a verdict-less review of
    committed work: prepare_retry turns that into the marker, but it has not run yet when
    `--check` does, right after the attempt.
    """
    if (tdir / 'resume').exists():
        return True
    try:
        return (tdir / 'status').read_text().strip() == 'UNKNOWN' and (tdir / 'reviewed.commit').stat().st_size > 0
    except OSError:
        return False


def _apply(plan: Path, tdir: Path, clear=(), writes=(), policy=True, pin=None):
    """Clear and write markers under the control lock. Returns (policy stood, pin reported).

    The policy re-reads escalate_after inside the lock (a human may have switched it off since
    the decision was made): it then writes nothing and its markers go, as `--escalate-after 0`
    clears them. A pin is reported as escalation.next only while it is still pending; the stop
    and split markers beneath it stay the policy's, because the pin has not been applied.
    """
    with _control_lock(plan):
        stood = policy and _setting(plan) > 0
        if policy and not stood:
            clear, writes = MARKERS, ()
        reported = pin is not None and _pin(tdir) == pin
        _clear(tdir, *clear)
        for name, text in writes:
            _write_marker(tdir, name, text)
        if reported:
            _write_marker(tdir, 'escalation.next', pin + '\n')
    return stood, reported


def _decide(plan: Path, tdir: Path, args, after: int):
    """What the policy wants done to the markers: (clear, writes, spec to print, exit code)."""
    current = _dwarf_of(plan, args.task)
    if not current or current in ('-', 'UNASSIGNED'):
        return (), (), '', 0
    registry = registry_models()
    streak = failed_streak(attempt_rows(tdir), current, registry)
    if streak < after:
        return MARKERS, (), '', 0
    target, why = stronger(current, load_pools(plan / 'fractal-routing.json'), registry)
    if target:
        writes = (('escalation.next', target + '\n'),) if args.check else ()
        return ('escalation.stop', 'split.suggested'), writes, target, 0
    split = ('%s failed %d attempts on %s, and %s. Another attempt on the same model rarely passes, '
             'so split the task into smaller ones, or retry it with an explicit --dwarf <spec>.\n'
             % (args.task, streak, current, why))
    return ('escalation.next',), (('escalation.stop', '%d\t%s\n' % (streak, current)), ('split.suggested', split)), '', 3


def escalation(args) -> int:
    plan = Path(os.path.realpath(args.plan))
    tdir = _task_dir(plan, args.task)
    if tdir is None:
        return 0
    after = _setting(plan)
    fractal = _fractal(plan)
    # Fractal plans freeze their eligible set at selection; a QA-only resume spends no dwarf
    # attempt, so neither is a moment to change the implementer.
    idle = after <= 0 or fractal or (tdir / 'resume').exists()
    # A pin is a human's choice and outranks the policy, so `--check` reports it as the next
    # model whatever escalate_after says; prepare_retry applies it and claims the file. The
    # policy is still decided underneath it and its markers kept current.
    pin = _pin(tdir) if args.check and not fractal and not _qa_next(tdir) else None
    clear, writes, shown, rc = (MARKERS if after <= 0 else ()), (), '', 0
    if not idle:
        clear, writes, shown, rc = _decide(plan, tdir, args, after)
    if not (writes or shown or pin):
        _clear(tdir, *clear)
        return 0
    stood, reported = _apply(plan, tdir, clear, writes, policy=not idle, pin=pin)
    if reported:
        print(pin)
        return 0
    if not stood:
        return 0
    if shown:
        print(shown)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-route.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('explore')
    for name in ('plan', 'task', 'tier', 'epsilon', 'default', 'pools'):
        p.add_argument('--' + name, required=True)
    p = sub.add_parser('escalation')
    p.add_argument('--plan', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--check', action='store_true')
    args = ap.parse_args(argv)
    if args.command == 'explore':
        try:
            print(explore(args))
        except Exception:
            print(args.default)
        return 0
    try:
        return escalation(args)
    except Exception:
        return 0


if __name__ == '__main__':
    sys.exit(main())
