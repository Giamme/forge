"""Labeled decision cases derived from the run dataset, in the System One wire format.

The store keeps raw events. This folds them into (state, questions, expected) cases for
the decisions Jev makes in forge, each labeled by what actually happened afterwards:

  routing   will the first attempt at this task pass review?  plus the tier the
            orchestrator or planner declared (agreement, not outcome)
  retry     will this retry pass, given the findings it was handed?
  review    will the independent reviewer pass this diff (state names the reviewer)?
  drift     which files the task did not declare did it really edit?
  citation  does this review describe this diff, or another task's from the same repo?
  memory    is this recorded fact about a file the task went on to edit?

States are built the way forge_jev builds them for the same site, so a model scored here
is scored on the inputs forge would send it. Where a site's answer cannot be labeled by
an outcome (QA effort sizing, the five routing rubrics, failure triage) it is left out
rather than labeled by opinion. Hosted Jev's own answers, where forge recorded them, go to
meta.jsonl as baselines; they are never part of a case.

The output holds prompts, diffs and review text from the repos forge ran in. It is written
0600 into a 0700 dir and must stay out of any public repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from . import connect, data_home, read_blob

SLICES = ('routing', 'retry', 'review', 'drift', 'citation', 'memory')

# The caps forge_jev applies to the same fields (routing.py, review.py, cli.py memory-slice).
FILE_EXCERPT_CHARS = 600
MAX_EXCERPT_FILES = 6
MEMORY_CHARS = 4000
MAX_SIBLINGS = 40
SIBLING_TITLE_CHARS = 120
DIFF_CHARS = 24000
REVIEW_CHARS = 12000
SLICE_TASK_CHARS = 4000

DRIFT_QUESTIONS = 6
MEMORY_QUESTIONS = 6

_DIFF_BLOCK = re.compile(r'```diff\n(.*?)\n```', re.S)
_ANCHOR = re.compile(r'\[([^\[\]]+)\]\s*$')


def _sha(*parts) -> str:
    return hashlib.sha256('\0'.join(str(p) for p in parts).encode()).hexdigest()


def _words(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [w for w in str(value or '').replace(',', ' ').split() if w and w != '-']


class Store:
    def __init__(self, db):
        self.events = []
        for kind, src, repo, run, run_id, mode, task, attempt, ts, data in db.execute(
                'SELECT kind, src, repo, run, run_id, mode, task, attempt, ts, data FROM events'):
            self.events.append(dict(kind=kind, src=src, repo=repo, run=run, run_id=run_id, mode=mode,
                                    task=task, attempt=attempt, ts=ts or 0, d=json.loads(data)))
        self.repo_paths = dict(db.execute('SELECT repo, path FROM repos'))
        self._blobs = {}

    def text(self, ref) -> str:
        if not isinstance(ref, dict) or not ref.get('sha'):
            return ''
        sha = ref['sha']
        if sha not in self._blobs:
            raw = read_blob(sha)
            self._blobs[sha] = raw.decode('utf-8', 'replace') if raw is not None else ''
        return self._blobs[sha]

    def of(self, kind):
        return [e for e in self.events if e['kind'] == kind]


def _question_builders():
    from forge_jev import questions as q
    return q


# --- new rubrics: the outcome versions of questions forge asks without a label --------

def first_pass() -> dict:
    return dict(type='noul', instructions=(
        'This Forge task is dispatched to the model in state.dwarf, at the effort shown, in its '
        'own worktree. An independent model (state.reviewer) then reviews the resulting diff '
        'against the task. Will that FIRST attempt pass review, with no retry needed?'),
        criteria={
            'true': 'The task is within reach of this model at this effort on the first try: the '
                    'requirements are clear, the change is contained, and nothing in it is likely '
                    'to be missed or subtly wrong.',
            'false': 'The first attempt is likely to fail review: the task has edge cases, '
                     'cross-cutting effects, unclear requirements or subtle state that a first '
                     'attempt by this model at this effort typically gets wrong, so a retry would '
                     'be needed.'})


def declared_tier() -> dict:
    # decompose.md's difficulty table, verbatim.
    return dict(type='choice', instructions=(
        'Given the task and its context (in state), which difficulty tier does it belong to? '
        'Rate the judgment the work requires, not how much code it produces: a five-hundred-line '
        'mechanical rename is low, a ten-line concurrency fix is high.'),
        criteria={
            'low': 'mechanical and local: rename, move, docs, a test for behaviour that already '
                   'exists. Little design judgment; a mistake is obvious.',
            'medium': 'self-contained implementation against a clear spec; some design choices, '
                      'bounded blast radius.',
            'high': 'needs design judgment, touches cross-cutting behaviour, has subtle edge cases '
                    'or state/concurrency, or the right approach is not obvious from the goal.'})


def review_passes() -> dict:
    return dict(type='noul', instructions=(
        'A diff is about to be reviewed for correctness by an independent model that did not '
        'write it (the diff and the task it implements are in state). Will that reviewer PASS '
        'the diff?'),
        criteria={
            'true': 'The diff does what the task asks, completely, with nothing a careful reviewer '
                    'would block on: requirements met, edge cases handled, nothing else broken.',
            'false': 'A careful reviewer would FAIL it: a requirement is missing or wrong, an edge '
                     'case breaks, a needed test is missing, or the change breaks behaviour '
                     'elsewhere.'})


def retry_pass() -> dict:
    return dict(type='noul', instructions=(
        'An earlier attempt at this Forge task failed review. The task is now dispatched again, to '
        'the model in state.dwarf at the effort shown, with that review\'s findings '
        '(state.previous_review) to fix; state.attempt is which try this is, and the model in '
        'state.reviewer reviews the result. Will this attempt pass review?'),
        criteria={
            'true': 'The findings are specific and fixable, and this model at this effort can address '
                    'them without breaking something else: the retry is likely to pass.',
            'false': 'The findings point at a design problem, an unclear requirement or a recurring '
                     'mistake that another attempt by this model at this effort is likely to repeat: '
                     'the retry is likely to fail again and should be escalated or rethought.'})


# --- indexes ---------------------------------------------------------------------------

class Runs:
    """Plans, attempts and dispatches of every run, joined by (run, task)."""

    def __init__(self, store: Store):
        self.store = store
        self.plans = defaultdict(list)
        for e in store.of('plan'):
            self.plans[e['run']].append(e)
        for revisions in self.plans.values():
            revisions.sort(key=lambda e: e['ts'])
        self.attempts = defaultdict(list)
        for e in store.of('attempt'):
            if e['mode'] == 'parallel' and e['task'] and e['d'].get('status'):
                self.attempts[(e['run'], e['task'])].append(e)
        for rows in self.attempts.values():
            rows.sort(key=lambda e: (e['attempt'] or 0, e['d'].get('wall_started') or e['ts']))
        self.dispatches = defaultdict(list)
        for e in store.of('dispatch'):
            if e['task']:
                self.dispatches[(e['run'], e['task'], e['d'].get('role'))].append(e)

    def plan_for(self, run, task, at):
        """The plan revision that held this task when its attempt started."""
        holding = [p for p in self.plans.get(run, []) if task in (p['d'].get('tasks') or {})]
        if not holding:
            return None
        before = [p for p in holding if p['ts'] <= at + 1]
        return before[-1] if before else holding[0]

    def prompt(self, run, task, plan=None) -> str:
        for e in self.attempts.get((run, task), []):
            text = self.store.text((e['d'].get('blobs') or {}).get('prompt'))
            if text.strip():
                return text
        if plan:
            return self.store.text(((plan['d'].get('tasks') or {}).get(task) or {}).get('prompt'))
        return ''

    def resolved(self, run, task, role, attempt) -> dict:
        rows = [e for e in self.dispatches.get((run, task, role), []) if e['attempt'] == attempt]
        rows = rows or self.dispatches.get((run, task, role), [])
        for e in rows:
            d = e['d']
            if d.get('model'):
                return dict(model=d.get('model'), effort=d.get('effort'), harness=d.get('harness'))
        return {}


def _spec(spec) -> dict:
    parts = str(spec or '').split(':')
    out = dict(model=parts[0] or None)
    if len(parts) > 1:
        out['effort'] = parts[1]
    if len(parts) > 2:
        out['harness'] = parts[2]
    return out


def _file_excerpts(store: Store, plan, files) -> dict:
    try:
        snapshot = json.loads(store.text((plan['d'].get('blobs') or {}).get('snapshot')) or '{}')
    except ValueError:
        snapshot = {}
    out = {}
    for rel in _words(files)[:MAX_EXCERPT_FILES]:
        if rel in snapshot and snapshot[rel] is not None:
            out[rel] = str(snapshot[rel])[:FILE_EXCERPT_CHARS]
        elif snapshot:
            out[rel] = '(does not exist yet)'
    return out


def _siblings(plan, task_id) -> list:
    out = []
    for tid, row in (plan['d'].get('tasks') or {}).items():
        if tid == task_id:
            continue
        out.append(dict(id=tid, title=str(row.get('title', ''))[:SIBLING_TITLE_CHARS],
                        files=row.get('files', ''), deps=row.get('deps', '')))
        if len(out) >= MAX_SIBLINGS:
            break
    return out


def _first_real(rows):
    """The first attempt that ran to a verdict: refunded and infra attempts do not count."""
    for e in rows:
        d = e['d']
        if d.get('refunded') or d.get('infra') or d.get('status') not in ('PASS', 'FAIL'):
            continue
        return e
    return None


# --- slices ----------------------------------------------------------------------------

def _task_state(runs: Runs, run, task, attempt):
    """forge_jev.routing's state for this task as planned when `attempt` started, plus who ran and reviewed it.

    Returns (state, plan row) or (None, None) when the plan or the task's requirements are gone.
    """
    store = runs.store
    plan = runs.plan_for(run, task, attempt['d'].get('wall_started') or attempt['ts'])
    if plan is None:
        return None, None
    row = plan['d']['tasks'][task]
    prompt = store.text(row.get('prompt')) or runs.prompt(run, task)
    if not prompt.strip():
        return None, None
    dwarf = runs.resolved(run, task, 'dwarf', attempt['attempt']) or _spec(attempt['d'].get('dwarf'))
    reviewer = runs.resolved(run, task, 'qa', attempt['attempt']) or _spec(attempt['d'].get('qa'))
    state = dict(goal=store.text((plan['d'].get('blobs') or {}).get('goal')) or plan['d'].get('goal') or '',
                 task=dict(id=task, title=row.get('title', ''), files=row.get('files', ''),
                           deps=row.get('deps', '')),
                 prompt=prompt, approach=store.text(row.get('approach')),
                 plan_tasks=_siblings(plan, task),
                 file_excerpts=_file_excerpts(store, plan, row.get('files')),
                 memory=store.text((plan['d'].get('blobs') or {}).get('memory'))[:MEMORY_CHARS],
                 dwarf=dwarf, reviewer=reviewer)
    return state, row


def routing_cases(runs: Runs):
    for (run, task), rows in runs.attempts.items():
        first = _first_real(rows)
        if first is None:
            continue
        state, row = _task_state(runs, run, task, first)
        if state is None:
            continue
        dwarf = state['dwarf']
        questions = dict(first_pass=first_pass())
        expected = dict(first_pass=first['d']['status'] == 'PASS')
        declared = row.get('declared_difficulty')
        if row.get('difficulty_source') in ('orchestrator', 'planner') and declared in ('low', 'medium', 'high'):
            questions['tier'] = declared_tier()
            expected['tier'] = declared
        routing = row.get('routing') or {}
        baseline = {}
        if routing.get('composite') not in (None, 'None'):
            # Hosted Jev's composite difficulty: higher means harder, so it ranks failures.
            baseline['first_pass'] = dict(source='hosted-jev composite', risk=float(routing['composite']))
        if routing.get('tier') in ('low', 'medium', 'high'):
            baseline['tier'] = dict(source='hosted-jev tier', predicted=routing['tier'],
                                    confidence=_float(routing.get('confidence')))
        if declared in ('low', 'medium', 'high'):
            baseline.setdefault('first_pass_declared', dict(source='declared difficulty', risk=
                                dict(low=0.0, medium=0.5, high=1.0)[declared]))
        yield dict(slice='routing', key=(run, task), run=run, repo=first['repo'], task=task,
                   state=state, questions=questions, expected=expected, baseline=baseline,
                   label_source=dict(first_pass='first non-infra attempt status',
                                     tier='declared_difficulty (%s)' % row.get('difficulty_source')),
                   dwarf=dwarf.get('model'))


def retry_cases(runs: Runs):
    """Every attempt after the first that ran to a verdict: retry with the findings it was given, or escalate?"""
    store = runs.store
    for (run, task), rows in runs.attempts.items():
        first = _first_real(rows)
        if first is None:
            continue
        for e in rows:
            d = e['d']
            if (e is first or (e['attempt'] or 0) <= (first['attempt'] or 0) or d.get('refunded') or d.get('infra')
                    or d.get('status') not in ('PASS', 'FAIL')):
                continue
            findings = store.text((d.get('blobs') or {}).get('retry_findings'))
            if not findings.strip():
                continue
            state, _row = _task_state(runs, run, task, e)
            if state is None:
                continue
            state.update(attempt=e['attempt'], previous_review=findings[:REVIEW_CHARS],
                         previous_review_truncated=len(findings) > REVIEW_CHARS)
            yield dict(slice='retry', key=(run, task, e['attempt']), run=run, repo=e['repo'], task=task,
                       state=state, questions=dict(retry_pass=retry_pass()),
                       expected=dict(retry_pass=d['status'] == 'PASS'), baseline={},
                       label_source=dict(retry_pass='status of this retry attempt'),
                       dwarf=state['dwarf'].get('model'))


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qa_rows(runs: Runs):
    """Every QA dispatch with a verdict, with the diff it was shown and its task text."""
    store = runs.store
    effort = {}
    for (run, task), rows in runs.attempts.items():
        for e in rows:
            text = store.text((e['d'].get('blobs') or {}).get('qa_effort_jev'))
            if text.strip():
                word, _, conf = text.strip().partition('\t')
                effort[(run, task, e['attempt'])] = (word, _float(conf))
    for (run, task, role), rows in runs.dispatches.items():
        if role != 'qa':
            continue
        for e in rows:
            d = e['d']
            if d.get('verdict') not in ('PASS', 'FAIL'):
                continue
            prompt = store.text((d.get('blobs') or {}).get('prompt'))
            blocks = _DIFF_BLOCK.findall(prompt)
            if not blocks or not blocks[-1].strip():
                continue
            yield dict(run=run, task=task, attempt=e['attempt'], repo=e['repo'], verdict=d['verdict'],
                       diff=blocks[-1], review=store.text((d.get('blobs') or {}).get('message')),
                       effort=effort.get((run, task, e['attempt'])), model=d.get('model'),
                       reviewer=dict(model=d.get('model'), effort=d.get('effort'), harness=d.get('harness')))


EFFORT_RANK = dict(low=0, medium=1, high=2, xhigh=3, max=4)


def review_cases(runs: Runs):
    seen = {}
    for r in _qa_rows(runs):
        task = runs.prompt(r['run'], r['task'])[:REVIEW_CHARS]
        key = _sha(task, r['diff'])
        seen.setdefault(key, []).append((r, task))
    for key, items in seen.items():
        verdicts = {r['verdict'] for r, _ in items}
        if len(verdicts) != 1:
            # Two reviews of the same diff disagreed: the label is noise, not an outcome.
            continue
        r, task = items[0]
        diff = r['diff']
        # Who reviews decides the outcome as much as the diff: the same diff meets a 0.93 pass rate
        # from one reviewer and 0.39 from another, so the reviewer is part of the state.
        state = dict(diff=diff[:DIFF_CHARS], task=task, truncated=len(diff) > DIFF_CHARS, reviewer=r['reviewer'])
        baseline = {}
        if r['effort'] and r['effort'][0] in EFFORT_RANK:
            baseline['passes'] = dict(source='hosted-jev qa effort', risk=EFFORT_RANK[r['effort'][0]] / 4.0)
        yield dict(slice='review', key=key, run=r['run'], repo=r['repo'], task=r['task'], state=state,
                   questions=dict(passes=review_passes()), expected=dict(passes=r['verdict'] == 'PASS'),
                   baseline=baseline, label_source=dict(passes='QA verdict'), reviewer=r['model'])


_SKIP_PREFIXES = ('.git/', '.forge/', 'node_modules/', 'vendor/', 'target/', 'dist/',
                  'build/', '.venv/', 'venv/', '__pycache__/')
_SKIP_SUFFIXES = ('.lock', '.min.js', '.map', '.png', '.jpg', '.jpeg', '.gif', '.svg',
                  '.ico', '.pdf', '.zip', '.woff', '.woff2', '.ttf')
_SKIP_NAMES = ('package-lock.json', 'npm-shrinkwrap.json', 'composer.lock',
               'Gemfile.lock', 'go.sum', 'pnpm-lock.yaml', 'yarn.lock')


def _candidate(name: str) -> bool:
    return not (name.startswith(_SKIP_PREFIXES) or name.endswith(_SKIP_SUFFIXES)
                or name.rsplit('/', 1)[-1] in _SKIP_NAMES)


def _tracked(store: Store, plan, repo, base) -> list[str]:
    listing = store.text((plan['d'].get('blobs') or {}).get('inventory')) if plan else ''
    if listing.strip():
        return [line for line in listing.splitlines() if line.strip()]
    path = store.repo_paths.get(repo)
    if not path or not base or not Path(path).is_dir():
        return []
    try:
        out = subprocess.run(['git', '-C', path, 'ls-tree', '-r', '--name-only', base],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.splitlines() if out.returncode == 0 else []


def _final_pass(rows):
    passed = [e for e in rows if e['d'].get('status') == 'PASS']
    return passed[-1] if passed else None


def _drift_baseline(store: Store):
    """Hosted Jev's jev-drift answers, keyed by (repo, task id, file)."""
    out = {}
    for e in store.of('jev'):
        if e['d'].get('site') != 'jev-drift':
            continue
        try:
            request = json.loads(store.text((e['d'].get('blobs') or {}).get('request')) or '{}')
        except ValueError:
            continue
        task = ((request.get('state') or {}).get('task') or {}).get('id')
        answers = e['d'].get('answers') or {}
        for qid, question in (request.get('questions') or {}).items():
            found = re.search(r"the file '([^']+)'", question.get('instructions', ''))
            value = (answers.get(qid) or {}).get('noul') if isinstance(answers.get(qid), dict) else answers.get(qid)
            if found and isinstance(value, (int, float)):
                out[(e['repo'], task, found.group(1))] = float(value)
    return out


def drift_cases(runs: Runs, rng_seed: int):
    q = _question_builders()
    store = runs.store
    jev = _drift_baseline(store)
    for (run, task), rows in runs.attempts.items():
        final = _final_pass(rows)
        if final is None:
            continue
        d = final['d']
        changed = [f for f in _words(d.get('changed_files')) if _candidate(f)]
        declared = set(_words(d.get('declared_files')))
        if not changed:
            continue
        plan = runs.plan_for(run, task, d.get('wall_started') or final['ts'])
        tracked = [f for f in _tracked(store, plan, final['repo'], d.get('base_ref')) if _candidate(f)]
        if len(tracked) < 20:
            continue
        rng = random.Random(_sha(rng_seed, run, task))
        positives = [f for f in changed if f not in declared]
        rng.shuffle(positives)
        positives = positives[:3]
        if not positives and rng.random() < 0.5:
            continue
        near_dirs = {f.rsplit('/', 1)[0] for f in list(declared) + changed if '/' in f}
        pool = [f for f in tracked if f not in declared and f not in changed]
        near = [f for f in pool if f.rsplit('/', 1)[0] in near_dirs]
        far = [f for f in pool if f not in set(near)]
        rng.shuffle(near)
        rng.shuffle(far)
        want = DRIFT_QUESTIONS - len(positives) if positives else 4
        negatives = near[:(want + 1) // 2]
        negatives += far[:want - len(negatives)]
        files = positives + negatives
        rng.shuffle(files)
        row = ((plan['d'].get('tasks') or {}).get(task) or {}) if plan else {}
        prompt = store.text(row.get('prompt')) or runs.prompt(run, task)
        if not prompt.strip():
            continue
        state = dict(goal=(store.text((plan['d'].get('blobs') or {}).get('goal')) if plan else '') or '',
                     task=dict(id=task, title=row.get('title') or d.get('title', ''),
                               declared_files=','.join(sorted(declared))),
                     prompt=prompt, approach=store.text(row.get('approach')))
        questions, expected, baseline = {}, {}, {}
        for i, name in enumerate(files):
            questions['f%d' % i] = q.drift_prediction(name)
            expected['f%d' % i] = name in positives
            if (final['repo'], task, name) in jev:
                baseline['f%d' % i] = dict(source='hosted-jev drift', p=jev[(final['repo'], task, name)])
        yield dict(slice='drift', key=(run, task), run=run, repo=final['repo'], task=task, state=state,
                   questions=questions, expected=expected, baseline=baseline,
                   label_source=dict(all='file in the passing attempt\'s changed files'))


def citation_cases(runs: Runs, rng_seed: int):
    q = _question_builders()
    fails = [r for r in _qa_rows(runs) if r['verdict'] == 'FAIL' and r['review'].strip()]
    by_repo = defaultdict(list)
    for r in fails:
        by_repo[r['repo']].append(r)
    seen = set()
    for r in fails:
        key = _sha(r['review'])
        if key in seen:
            continue
        seen.add(key)
        rng = random.Random(_sha(rng_seed, key))
        own = rng.random() < 0.5
        diff = r['diff']
        if not own:
            mine = set(re.findall(r'^\+\+\+ b/(\S+)', diff, re.M))
            others = [o for o in by_repo[r['repo']] if o['task'] != r['task']
                      and not mine & set(re.findall(r'^\+\+\+ b/(\S+)', o['diff'], re.M))]
            if not others:
                continue
            diff = rng.choice(others)['diff']
        state = dict(review=r['review'][:REVIEW_CHARS], diff=diff[:DIFF_CHARS],
                     truncated=dict(review=len(r['review']) > REVIEW_CHARS, diff=len(diff) > DIFF_CHARS))
        yield dict(slice='citation', key=key, run=r['run'], repo=r['repo'], task=r['task'], state=state,
                   questions=dict(citation=q.findings_cite_the_diff()), expected=dict(citation=own),
                   baseline={}, label_source=dict(citation='review paired with its own diff (true) or '
                                                           'another task\'s diff from the same repo (false)'))


def _area(path: str) -> str:
    parts = path.split('/')
    return '/'.join(parts[:2]) if len(parts) > 2 else parts[0]


def memory_cases(runs: Runs, rng_seed: int):
    q = _question_builders()
    store = runs.store
    for (run, task), rows in runs.attempts.items():
        final = _final_pass(rows)
        if final is None:
            continue
        d = final['d']
        changed = set(_words(d.get('changed_files')))
        touched = changed | set(_words(d.get('declared_files')))
        if not changed:
            continue
        plan = runs.plan_for(run, task, d.get('wall_started') or final['ts'])
        memory = store.text((plan['d'].get('blobs') or {}).get('memory')) if plan else ''
        facts = [line for line in memory.splitlines() if line.startswith('- ')]
        positives, negatives = [], []
        areas = {_area(f) for f in touched}
        for line in facts:
            found = _ANCHOR.search(line)
            if not found:
                continue
            anchors = _words(found.group(1))
            if any(a in changed for a in anchors):
                positives.append(line)
            elif not any(_area(a) in areas for a in anchors):
                negatives.append(line)
        if not positives or not negatives:
            continue
        rng = random.Random(_sha(rng_seed, run, task))
        rng.shuffle(positives)
        rng.shuffle(negatives)
        positives = positives[:2]
        lines = positives + negatives[:MEMORY_QUESTIONS - len(positives)]
        rng.shuffle(lines)
        task_text = runs.prompt(run, task, plan)[:SLICE_TASK_CHARS]
        if not task_text.strip():
            continue
        questions = {'l%d' % i: q.memory_relevant(line, task_text) for i, line in enumerate(lines)}
        expected = {'l%d' % i: line in positives for i, line in enumerate(lines)}
        yield dict(slice='memory', key=(run, task), run=run, repo=final['repo'], task=task,
                   state=dict(task=task_text, facts=lines), questions=questions, expected=expected,
                   baseline={}, label_source=dict(all='fact anchored in a file the task edited (true) '
                                                      'or in an area it never touched (false)'))


# --- sampling and output ---------------------------------------------------------------

def _sample(cases: list, limit: int | None, seed: int) -> list:
    """Spread a capped slice across runs, then across labels within a run."""
    if not limit or len(cases) <= limit:
        return cases
    rng = random.Random(seed)
    by_run = defaultdict(list)
    for case in cases:
        by_run[case['run']].append(case)
    for rows in by_run.values():
        rng.shuffle(rows)
    picked = []
    while len(picked) < limit and any(by_run.values()):
        for run in sorted(by_run, key=lambda r: _sha(seed, r)):
            if by_run[run] and len(picked) < limit:
                picked.append(by_run[run].pop())
    return picked


def build(db, *, slices=SLICES, limit: int | None = None, seed: int = 7) -> list:
    store = Store(db)
    runs = Runs(store)
    builders = dict(routing=lambda: routing_cases(runs), retry=lambda: retry_cases(runs),
                    review=lambda: review_cases(runs),
                    drift=lambda: drift_cases(runs, seed), citation=lambda: citation_cases(runs, seed),
                    memory=lambda: memory_cases(runs, seed))
    out = []
    for name in slices:
        cases = list(builders[name]())
        for case in cases:
            case['id'] = '%s-%s' % (name, _sha(seed, *([case['key']] if isinstance(case['key'], str)
                                                        else case['key']))[:12])
        out.extend(sorted(_sample(cases, limit, seed), key=lambda c: c['id']))
    return out


def _private_write(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(path, 0o600)


def write(cases: list, out: Path, *, seed: int, limit) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    lines, meta = [], []
    for c in cases:
        lines.append(json.dumps(dict(id=c['id'], state=c['state'], questions=c['questions'],
                                     expected=c['expected']), ensure_ascii=False, sort_keys=True))
        meta.append(json.dumps(dict(id=c['id'], slice=c['slice'], run=c['run'], repo=c['repo'],
                                    task=c['task'], baseline=c['baseline'], label_source=c['label_source'],
                                    dwarf=c.get('dwarf'), reviewer=c.get('reviewer'),
                                    state_chars=len(json.dumps(c['state'], ensure_ascii=False)),
                                    question_chars=len(json.dumps(c['questions'], ensure_ascii=False))),
                               ensure_ascii=False, sort_keys=True))
    body = '\n'.join(lines) + '\n'
    _private_write(out / 'dataset.jsonl', body)
    _private_write(out / 'meta.jsonl', '\n'.join(meta) + '\n')
    counts = defaultdict(lambda: defaultdict(int))
    for c in cases:
        for qid, label in c['expected'].items():
            counts[c['slice']][str(label)] += 1
    manifest = dict(created=time.strftime('%Y-%m-%dT%H:%M:%S'), seed=seed, limit_per_slice=limit,
                    cases=len(cases), sha256=hashlib.sha256(body.encode()).hexdigest(),
                    slices={s: dict(cases=sum(1 for c in cases if c['slice'] == s), labels=dict(v))
                            for s, v in counts.items()})
    _private_write(out / 'manifest.json', json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return manifest


def main(args) -> int:
    db = connect()
    try:
        cases = build(db, slices=args.slices or SLICES, limit=args.limit, seed=args.seed)
    finally:
        db.close()
    if args.out:
        out = Path(args.out)
    else:
        (data_home() / 'exports').mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data_home() / 'exports', 0o700)
        out = data_home() / 'exports' / 'forge-decisions'
    manifest = write(cases, out, seed=args.seed, limit=args.limit)
    print('wrote %d cases to %s' % (manifest['cases'], out / 'dataset.jsonl'))
    for name, info in sorted(manifest['slices'].items()):
        print('  %-9s %4d cases  labels %s' % (name, info['cases'],
                                               ', '.join('%s %d' % kv for kv in sorted(info['labels'].items()))))
    return 0
