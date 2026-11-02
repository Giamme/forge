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
      Prints the spec to use. Appends the decision, with the tier of its choice, to
      <plan>/tasks/<id>/route.jsonl (never rewritten) and marks a changed pick in tasks/<id>/route.explored for the table. A row
      with no neighbour is not explored and records nothing.

  forge-route.py escalation --plan P --task ID [--check] [--qa-only-next yes|no]
      The retry policy: after <plan>/escalate_after (default 3; 0 = off) failed attempts on
      one spec, the next attempt goes to the next tier up of the user's pools instead of
      repeating the same dispatch. Prints that spec when one is due. When none exists, writes
      tasks/<id>/escalation.stop and split.suggested and exits 3. When <plan>/escalate_effort
      contains `on` and no higher tier has another model, the same alias and harness is tried
      once more at the next effort its pairing accepts (weakest above the current one; a spec
      that names no effort counts as medium, a dwarf dispatch's default) before that stop.
      The streak then counts per effort, so the new effort starts again. At the pairing's
      highest effort the task still stops, and split.suggested says that effort was tried too.
      A lateral move recorded in tasks/<id>/lateral.tsv does not end the count (see failed_streak).
      With the file absent the streak and the stop are exactly as before. `--check` (run after
      every attempt) also records a due escalation in escalation.next, and clears the markers
      once the streak no longer holds. A pending tasks/<id>/escalation.pin (a human's one-shot
      choice, applied by prepare_retry) is what `--check` reports as the next model instead
      (escalation.next = its spec), whatever escalate_after says, unless the next retry is a
      QA-only resume or the plan is Fractal (pins never apply there). `--qa-only-next` is
      that fact as the caller measured it; without the option, a resume marker or an
      unreviewed commit is taken as QA-only. Nothing has applied the pin yet, so the policy's
      own stop and split markers stay as the policy decided them beneath it: they are what
      holds again if the pin is withdrawn. The marker writes take <plan>/control.lock and
      re-read escalate_after inside it: a policy decision made before a human switched the
      policy off writes nothing.

  forge-route.py fallback --plan P --task ID [--exclude harness/model,...]
      The lateral move of an infrastructure stop: prints the next free model of the pool of the
      tier the task runs in (its difficulty, the tier its exploration picked, or the target its last
      escalation recorded in running.tier), or exits 1 when there is none. Never a model of another tier.
      The move is recorded by `lateral` below, not as an escalation.

  forge-route.py qa-fallback --plan P --task ID [--exclude harness/model,...]
      The lateral move of a review stopped on infrastructure: prints the peer reviewer from
      plan/qa-routing.json (the --qa, --qa-low, --qa-medium and --qa-high pools `plan` was given): the
      next member of the task's difficulty pool and then of `any`, never the stopped reviewer's model nor
      the implementer's (a task is not reviewed by the model that wrote it), or exits 1 when there is none.
      A plan with no qa-routing.json, or a reviewer no pool lists, has no peer.

  forge-route.py record --plan P --task ID (--probe | --line TEXT)
      Internal. Probe or append tasks/<id>/escalations.tsv through forge-control.py, which
      walks from the plan one directory at a time and refuses a symlink at any component.
      An appended row also writes tasks/<id>/running.tier: its target tier and spec.
      `--probe` exits 0 when every component below the plan is a real directory and the file
      is absent or a regular file. `--line` appends one record under those same conditions.
      A missing parent exits 1 and writes nothing: the walk has no directory descriptor then,
      and opening the leaf would create it in the process's current directory.

  forge-route.py lateral --plan P --task ID (--probe | --line TEXT)
      Internal. The same probe and append, for tasks/<id>/lateral.tsv: the record of a dwarf
      lateral move (epoch, attempt, role, from, to, cause). Its appended row writes running.tier
      too: a move keeps the tier, and the spec it moved to is the one that tier is recorded for.

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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIERS = ('low', 'medium', 'high')
SCRIPTS = Path(__file__).resolve().parent
REGISTRY = SCRIPTS.parent / 'registry.tsv'
FAILED = ('FAIL', 'TIMEOUT', 'BOUNCED', 'INCOMPLETE')   # a judgment against the spec that ran
NEUTRAL = ('INFRA', 'UNKNOWN', 'ERROR', 'RUNNING', 'INTERRUPTED', 'NEEDS_INPUT')   # say nothing about it
DEFAULT_ESCALATE_AFTER = 3
DEFAULT_EFFORT = 'medium'                         # forge-dispatch.sh dwarf default
LOCK_WAIT = 5.0                                    # seconds to wait for P/control.lock
# forge-dispatch.sh rank_of. Only for "which accepted effort is above this one"; the set of
# efforts a pairing accepts comes from `forge-dispatch.sh efforts`, not from this table.
# ultra and ultracode share a rank: each is its own harness's top, under different names.
EFFORT_RANK = {'low': 0, 'medium': 1, 'high': 2, 'xhigh': 3, 'max': 4, 'ultra': 5, 'ultracode': 5}


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

def registry_models(path=None) -> dict:
    """{alias: {harness: model id}} from registry.tsv (or FORGE_REGISTRY, as dispatch and the scheduler read
    it); the first row of an alias is its default."""
    path = Path(path or os.environ.get('FORGE_REGISTRY') or REGISTRY)
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


def model_key(spec: str, registry: dict) -> str:
    """The model a spec runs, named so that one model reached through two harnesses compares equal
    (`gpt-6.1-sol` on codex and `github-copilot/gpt-6.1-sol` on opencode): the id it resolves to, without
    its provider prefix and with dots and dashes alike. Two ids are compared, never two aliases, since
    one alias may list different models on different harnesses (`gemini` is Flash on antigravity and
    Pro on opencode)."""
    return canonical(spec, registry).rsplit('/', 1)[-1].lower().replace('.', '-')


def same_model(a: str, b: str, registry: dict) -> bool:
    return model_key(a, registry) == model_key(b, registry)


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
    """The exploration alternative and its tier: the nearest cheaper tier for medium/high, the nearest
    stronger for low. (None, None) when there is none."""
    if tier not in TIERS:
        return None, None
    i = TIERS.index(tier)
    order = TIERS[i + 1:] if i == 0 else tuple(reversed(TIERS[:i]))
    for other in order:
        if pools.get(other):
            pick = _aligned(spec, pools.get(tier) or [spec], pools[other], lambda s: arm(s, registry))
            if pick:
                return pick, other
    return None, None


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


# --- capacity fallback ---------------------------------------------------------------

def pair_of(spec: str, registry: dict):
    """(harness, model id) a spec runs on, as forge-dispatch.sh's lookup resolves it; None when it cannot."""
    parts = str(spec).split(':')
    alias, harness = parts[0], ':'.join(parts[2:])
    rows = registry.get(alias)
    if rows:
        if not harness:
            return next(iter(rows.items()))
        return harness, rows.get(harness, alias)
    return (harness, alias) if harness else None


def parse_excluded(text: str) -> set:
    """`harness/model` items, comma-separated; `harness/*` excludes every model on that harness."""
    out = set()
    for item in (text or '').split(','):
        harness, slash, model = item.strip().partition('/')
        if harness and slash and model:
            out.add((harness, model))
    return out


def fallback_pick(current: str, pools: dict, registry: dict, excluded: set, tier: str):
    """The next member of the pool of `tier`, the tier the task runs in (running_tier), in pool
    order and wrapping around, that runs another model on a pair nobody paused. None when there is
    none, or when that pool does not run the current model (a model the user named by hand)."""
    pool = pools.get(tier) or []
    own = canonical(current, registry)
    keys = [canonical(s, registry) for s in pool]
    if own not in keys:
        return None
    start = keys.index(own)
    for step in range(1, len(pool) + 1):
        candidate = pool[(start + step) % len(pool)]
        pair = pair_of(candidate, registry)
        if canonical(candidate, registry) == own or pair is None:
            continue
        if pair in excluded or (pair[0], '*') in excluded:
            continue
        return candidate
    return None


def load_qa_pools(path) -> dict:
    """{'any'|'low'|'medium'|'high': [spec, ...]} from plan/qa-routing.json, the reviewer pools `plan` was
    given. Empty for a plan made before the file existed."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for key in ('any',) + TIERS:
        specs = data.get(key) if isinstance(data, dict) else None
        if isinstance(specs, list):
            specs = [str(s).strip() for s in specs if str(s).strip()]
            if specs:
                out[key] = specs
    return out


def qa_fallback_pick(current: str, implementer: str, pools: dict, registry: dict, excluded: set, difficulty: str):
    """The peer reviewer of a review stopped on infrastructure: the next member, in pool order and
    wrapping around, of the task's difficulty pool and then of `any`, that runs another model than the
    stopped reviewer and than the implementer, on a pair nobody paused. A reviewer that no such pool
    lists (named by hand) has no peer, since its strength is unknown. None when there is none."""
    order = [pools.get(key) or [] for key in (difficulty, 'any') if key in TIERS or key == 'any']
    own = canonical(current, registry)
    if not any(canonical(s, registry) == own for pool in order for s in pool):
        return None
    wrote = implementer if implementer not in ('', '-', 'UNASSIGNED') else None
    for pool in order:
        keys = [canonical(s, registry) for s in pool]
        start = keys.index(own) if own in keys else -1
        for step in range(1, len(pool) + 1):
            candidate = pool[(start + step) % len(pool)]
            model, pair = canonical(candidate, registry), pair_of(candidate, registry)
            if model == own or (wrote and same_model(candidate, wrote, registry)) or pair is None:
                continue
            if pair in excluded or (pair[0], '*') in excluded:
                continue
            return candidate
    return None


def escalation_target(source: str, frm: str, to: str, running: str, pools: dict, registry: dict) -> str:
    """The tier an escalations.tsv row moved the task to. The same model at another effort stays.
    The policy (stronger) went to the first pool with its target above the highest pool of `frm`;
    a person named a model, which is in the nearest pool to the tier the task was running in."""
    model = canonical(to, registry)
    if model == canonical(frm, registry):
        return running
    rank = rank_of(frm, pools, registry) if source == 'policy' else None
    first = rank + 1 if rank is not None else TIERS.index(running)
    for tier in TIERS[first:] + tuple(reversed(TIERS[:first])):
        if any(canonical(s, registry) == model for s in pools.get(tier, ())):
            return tier
    return running


def running_tier(plan: Path, task: str, current: str = '') -> str:
    """The tier a task runs in, from what forge recorded when it assigned the model, never from which
    pools list the model: one model can sit in several, and a re-plan can change them. While the task
    runs the spec of a record, the tier in tasks/<id>/running.tier (written with each escalations.tsv
    row), else the tier its exploration picked; with neither, as in an older plan, the difficulty.
    `current` is the spec the task runs, read from tasks.tsv when not given. '' when the difficulty
    is no tier."""
    tier = _row_cell(plan, task, 2)
    if tier not in TIERS:
        return ''
    tdir = plan / 'tasks' / task
    current = current or _dwarf_of(plan, task)
    with contextlib.suppress(OSError):
        recorded, _, spec = (tdir / 'running.tier').read_text().strip().partition('\t')
        if spec == current and recorded in TIERS:
            return recorded
    with contextlib.suppress(OSError, ValueError, AttributeError):       # no usable record: the difficulty
        lines = (tdir / 'route.jsonl').read_text().splitlines()
        decision = json.loads(lines[-1]) if lines else {}
        if decision.get('chosen') == current and decision.get('chosen_tier') in TIERS:
            return decision['chosen_tier']
    return tier


def record_running_tier(plan: str, task: str, row: str, control) -> None:
    """tasks/<id>/running.tier for an escalations.tsv row: `tier<TAB>spec`, the tier the row moved the
    task to, decided here with the pools of this moment. A fallback stays in its tier."""
    cells = row.rstrip('\n').split('\t')
    frm, to, source = cells[2], cells[3], cells[4] if len(cells) > 4 else 'policy'
    tier = running_tier(Path(plan), task, frm)
    if tier and source != 'fallback':
        tier = escalation_target(source, frm, to, tier, load_pools(Path(plan) / 'fractal-routing.json'),
                                 registry_models())
    if tier:
        control.write_atomic(plan, 'tasks/%s/running.tier' % task, '%s\t%s\n' % (tier, to))


def fallback(args) -> int:
    plan = Path(os.path.realpath(args.plan))
    current = _dwarf_of(plan, args.task)
    if not current or current in ('-', 'UNASSIGNED'):
        return 1
    pick = fallback_pick(current, load_pools(plan / 'fractal-routing.json'), registry_models(),
                         parse_excluded(args.exclude), running_tier(plan, args.task, current))
    if not pick:
        return 1
    print(pick)
    return 0


def qa_fallback(args) -> int:
    plan = Path(os.path.realpath(args.plan))
    current, implementer = _row_cell(plan, args.task, 5), _dwarf_of(plan, args.task)
    if not current or current in ('-', 'UNASSIGNED'):
        return 1
    pick = qa_fallback_pick(current, implementer, load_qa_pools(plan / 'qa-routing.json'), registry_models(),
                            parse_excluded(args.exclude), _row_cell(plan, args.task, 2))
    if not pick:
        return 1
    print(pick)
    return 0


# --- exploration -------------------------------------------------------------------

def explore(args) -> str:
    epsilon = float(args.epsilon)
    if not 0 < epsilon <= 1:
        return args.default
    alternative, alternative_tier = neighbour(args.default, args.tier, load_pools(args.pools), registry_models())
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
                    chosen_tier=alternative_tier if chosen != args.default else args.tier,
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

def _row_cell(plan: Path, task: str, index: int) -> str:
    for line in (plan / 'tasks.tsv').read_text().splitlines():
        cells = line.split('\t')
        if not line.startswith('#') and cells and cells[0] == task and len(cells) > index:
            return cells[index]
    return ''


def _dwarf_of(plan: Path, task: str) -> str:
    for line in (plan / 'tasks.tsv').read_text().splitlines():
        cells = line.split('\t')
        if not line.startswith('#') and cells and cells[0] == task and len(cells) > 4:
            return cells[4]
    return ''


def _spec_parts(spec: str):
    """(alias, effort, harness) of alias[:effort[:harness]]. Effort and harness are '' when unnamed."""
    alias, sep, rest = str(spec).partition(':')
    if not sep:
        return alias, '', ''
    effort, sep, harness = rest.partition(':')
    return alias, effort, harness if sep else ''


def _effort_of(spec: str) -> str:
    """The effort a spec names, or medium when it names none (what a dwarf dispatch runs)."""
    _alias, effort, _harness = _spec_parts(spec)
    return effort or DEFAULT_EFFORT


def _effort_step(plan: Path) -> bool:
    try:
        return (plan / 'escalate_effort').read_text().strip() == 'on'
    except OSError:
        return False


def _accepted_efforts(spec: str) -> list:
    """Efforts `spec`'s pairing accepts, weakest first. Empty when dispatch cannot say."""
    try:
        done = subprocess.run(
            ['bash', str(SCRIPTS / 'forge-dispatch.sh'), 'efforts', '--spec', spec],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _effective_effort(requested: str, accepted: list):
    """The effort a dwarf dispatch runs when `requested` is asked of a pairing accepting
    `accepted`: as asked when accepted, else the strongest accepted at or below it, else the
    weakest accepted (forge-dispatch.sh's clamp)."""
    if requested in accepted:
        return requested
    want = EFFORT_RANK.get(requested)
    below = [e for e in accepted if want is not None and EFFORT_RANK.get(e, 99) <= want]
    return below[-1] if below else (accepted[0] if accepted else None)


def _next_effort_spec(spec: str):
    """The same alias and harness at the next accepted effort above the one a dispatch of
    `spec` actually runs, or None."""
    alias, _effort, harness = _spec_parts(spec)
    accepted = _accepted_efforts(spec)
    rank = EFFORT_RANK.get(_effective_effort(_effort_of(spec), accepted) or '')
    if not alias or rank is None:
        return None
    for effort in accepted:
        other = EFFORT_RANK.get(effort)
        if other is not None and other > rank:
            return '%s:%s:%s' % (alias, effort, harness) if harness else '%s:%s' % (alias, effort)
    return None


def failed_streak(rows: list, current: str, registry: dict, by_effort: bool = False, laterals=()) -> int:
    """Distinct trailing attempt numbers that failed on `current`'s model.

    rows: (attempt, status, spec-or-None) oldest first. A row without a spec predates the
    column and is taken as the current spec. Infra stops, missing verdicts and forge errors
    judge nothing and are skipped; a pass, a merge or another model ends the streak.
    laterals: (attempt, from, to) of each dwarf move in lateral.tsv. A model change between
    two counted attempts does not end the streak when dwarf moves recorded between them take
    the earlier attempt's spec to the later one's, through any chain of moves made in order
    (see _moved_between): a lateral move is an infrastructure stop, not a judgment that the
    model was too weak.
    With by_effort, another effort of the same model ends it too (a bump starts a new
    streak). A spec that names no effort counts as medium. With by_effort off the count
    is by model only, as before.
    """
    effort = _effort_of(current) if by_effort else None
    seen = set()
    expect, later = current, None        # the spec the last counted attempt ran (current to start), and its number
    for attempt, status, spec in reversed(rows):
        if status in NEUTRAL:
            continue
        if spec and canonical(spec, registry) != canonical(expect, registry):
            if later is None or not _moved_between(laterals, spec, expect, attempt, later):
                break
        if effort is not None and _effort_of(spec or current) != effort:
            break
        if status not in FAILED:
            break
        seen.add(attempt)
        expect, later = spec or expect, attempt
    return len(seen)


def _moved_between(laterals, frm: str, to: str, earlier: int, later: int) -> bool:
    """Whether dwarf moves took `frm` to `to` between two attempts: after attempt `earlier` was spent and
    before attempt `later` began. A move records the attempts spent at that moment (an infrastructure stop
    refunds its own), so a move straight after `earlier` is recorded as `earlier`. One stop can make several
    moves (sol -> astra -> sonnet) with no attempt on the middle model, so the moves in the window must chain
    in the order they were made: each one starts where the last ended. A move that starts elsewhere means
    the model changed in between by something no lateral row records (an explicit retry), and the window
    does not carry across it."""
    model = frm
    for n, f, t in laterals:
        if earlier <= n < later:
            if f != model:
                return False
            model = t
    return model == to


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


def lateral_rows(tdir: Path) -> list:
    """(attempt, from, to) of each dwarf move in tasks/<id>/lateral.tsv, oldest first."""
    rows = []
    try:
        lines = (tdir / 'lateral.tsv').read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        cells = line.split('\t')
        if len(cells) >= 5 and cells[1].isdigit() and cells[2] == 'dwarf':
            rows.append((int(cells[1]), cells[3], cells[4]))
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
    """Fallback when `--qa-only-next` is absent: a resume marker, or an unreviewed commit.

    This does not look at the worktree. Callers that measured it (qa_only_next) pass
    `--qa-only-next` instead, so a marker the worktree no longer matches is a full retry.
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
    step = _effort_step(plan)
    streak = failed_streak(attempt_rows(tdir), current, registry, by_effort=step, laterals=lateral_rows(tdir))
    if streak < after:
        return MARKERS, (), '', 0
    target, why = stronger(current, load_pools(plan / 'fractal-routing.json'), registry)
    # Only once no higher tier has a model. A tier-up is still the stronger move.
    if target is None and step:
        target = _next_effort_spec(current)
    if target:
        writes = (('escalation.next', target + '\n'),) if args.check else ()
        return ('escalation.stop', 'split.suggested'), writes, target, 0
    if step:
        why = '%s. The highest effort this pairing accepts was tried as well' % why
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
    # policy is still decided underneath it and its markers kept current. The policy's own
    # idle test above is separate and stays on the raw resume marker.
    qa_only = _qa_next(tdir) if args.qa_only_next is None else args.qa_only_next == 'yes'
    pin = _pin(tdir) if args.check and not fractal and not qa_only else None
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


def _load_control():
    """forge-control.py, by path: probe and append_line refuse a symlink at any component."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'forge_control', Path(__file__).resolve().with_name('forge-control.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules['forge_control'] = module
    spec.loader.exec_module(module)
    return module


def _record(args, name: str, after=None) -> int:
    """Probe or append tasks/<id>/<name>. 0 when that is safe; 1 when it is not.

    A missing parent is a refusal on both paths. probe() reports that as the file being
    absent, so the check has to look at the walk itself. append_line refuses the same case
    before it opens the leaf: with no directory descriptor that open would use the process
    cwd, including when the task directory disappears between this check and the append.
    `after(plan, task, line, control)` runs once the line is appended; its failure is printed,
    not returned, because the row stands.
    """
    if not args.task or args.task in ('.', '..') or '/' in args.task or '\0' in args.task:
        return 1
    try:
        control = _load_control()
        plan = os.path.realpath(args.plan)
        rel = 'tasks/%s/%s' % (args.task, name)
        parent, _leaf = control._open_parent(plan, rel)
        if parent is None:
            return 1
        os.close(parent)
        if args.probe:
            control.probe(plan, rel)
        else:
            line = args.line if args.line.endswith('\n') else args.line + '\n'
            control.append_line(plan, rel, line)
            if after:
                try:
                    after(plan, args.task, line, control)
                except Exception as error:      # the row stands; without this record the task reads as its difficulty
                    print('%s: could not record the tier it runs in: %s' % (args.task, error), file=sys.stderr)
    except Exception:
        return 1
    return 0


def record_escalation(args) -> int:
    """Probe or append tasks/<id>/escalations.tsv, and the tier the row moved the task to."""
    return _record(args, 'escalations.tsv', record_running_tier)


def record_lateral(args) -> int:
    """Probe or append tasks/<id>/lateral.tsv, and the tier the move keeps (record_lateral_tier)."""
    return _record(args, 'lateral.tsv', record_lateral_tier)


def record_lateral_tier(plan: str, task: str, row: str, control) -> None:
    """tasks/<id>/running.tier after a lateral move: the tier the task ran in, now recorded for the spec
    it moved to. Without this a task moved sideways from a recorded spec would read as its difficulty again."""
    cells = row.rstrip('\n').split('\t')
    if cells[2] != 'dwarf':                  # a reviewer's move says nothing about the implementer's tier
        return
    frm, to = cells[3], cells[4]
    tier = running_tier(Path(plan), task, frm)
    if tier:
        control.write_atomic(plan, 'tasks/%s/running.tier' % task, '%s\t%s\n' % (tier, to))


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
    p.add_argument('--qa-only-next', choices=('yes', 'no'), default=None,
                   help='whether the next entry only re-runs QA, as the caller measured it')
    p = sub.add_parser('fallback', help='the same-tier model a capacity stop moves to')
    p.add_argument('--plan', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--exclude', default='', help='harness/model pairs to skip, comma-separated (harness/* for all)')
    p = sub.add_parser('qa-fallback', help='the peer reviewer a review stopped on infrastructure moves to')
    p.add_argument('--plan', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--exclude', default='', help='harness/model pairs to skip, comma-separated (harness/* for all)')
    for name in ('record', 'lateral'):
        p = sub.add_parser(name, help=argparse.SUPPRESS)
        p.add_argument('--plan', required=True)
        p.add_argument('--task', required=True)
        mode = p.add_mutually_exclusive_group(required=True)
        mode.add_argument('--probe', action='store_true')
        mode.add_argument('--line')
    args = ap.parse_args(argv)
    if args.command == 'explore':
        try:
            print(explore(args))
        except Exception:
            print(args.default)
        return 0
    if args.command == 'record':
        return record_escalation(args)
    if args.command == 'lateral':
        return record_lateral(args)
    if args.command in ('fallback', 'qa-fallback'):
        try:
            return (fallback if args.command == 'fallback' else qa_fallback)(args)
        except Exception:
            return 1
    try:
        return escalation(args)
    except Exception:
        return 0


if __name__ == '__main__':
    sys.exit(main())
