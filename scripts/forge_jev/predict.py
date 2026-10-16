"""Silent outcome predictions on Clef: will a task's first attempt pass, will this retry pass.

Recorded and never read by a run. `forge-jev.py first-pass` runs once per plan, after the
table rewrite that assigns every row its dwarf and reviewer; `forge-jev.py retry-pass` runs
when a dwarf attempt above 1 begins. Each writes tasks/<id>/clef.first_pass or
clef.retry_pass.<n> and, through client.ask, a `jev` event in the run dataset. Nothing
here is put into a prompt, a verdict, tasks.tsv, a retry's spec or its findings.

The state is forge_jev.outcome's, the one the export rebuilds for the same rubrics, so a
live prediction and the measurement it is judged by see the same shape of evidence.
Bounded: one deadline for the whole command, at most 4 requests in flight, model
resolution cached per (role, spec).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import load_config, site_ready, write_json
from . import outcome, questions
from .client import ask, noul
from .features import task_size
from .routing import _excerpt, _memory

SCRIPTS = Path(__file__).resolve().parents[1]
DISPATCH = SCRIPTS / 'forge-dispatch.sh'
CONCURRENCY = 4


def _text(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def rows(plan: Path) -> list[dict]:
    out = []
    for line in _text(plan / 'tasks.tsv').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        out.append(dict(id=parts[0], deps=parts[1], difficulty=parts[2], files=parts[3], dwarf=parts[4],
                        qa=parts[5], title=parts[6]))
    return out


class Resolver:
    """{model, effort, harness} of a role and spec, by dispatch's own dry run, once per pair."""

    def __init__(self, deadline_at: float):
        self.deadline_at = deadline_at
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, role: str, spec: str) -> dict:
        key = (role, spec)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        found = {}
        remaining = self.deadline_at - time.time()
        if remaining > 0.5:
            with tempfile.TemporaryDirectory(prefix='forge-clef-resolve-') as tmp:
                prompt = Path(tmp) / 'input'
                prompt.write_text('Offline resolution')
                try:
                    out = subprocess.run(['/bin/bash', str(DISPATCH), role, spec, '--run-dir', tmp,
                                          '--prompt-file', str(prompt), '--dry-run'],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                         timeout=min(10.0, remaining))
                    fields = dict(line.split('=', 1) for line in out.stdout.splitlines() if '=' in line)
                    if fields.get('model'):
                        found = dict(model=fields.get('model'), effort=fields.get('effort'),
                                     harness=fields.get('harness'))
                except (OSError, subprocess.SubprocessError, ValueError):
                    found = {}
        with self.lock:
            self.cache[key] = found
        return found


def task_state(plan: Path, repo: Path, row: dict, table: list, resolve: Resolver) -> dict | None:
    prompt = _text(plan / 'tasks' / row['id'] / 'prompt.md')
    if not prompt.strip():
        return None
    dwarf, reviewer = resolve('dwarf', row['dwarf']), resolve('qa', row['qa'])
    if not dwarf or not reviewer:
        return None
    level = _text(plan / 'decompose_level').strip() or None
    return outcome.assemble(
        goal=_text(plan / 'goal.txt'), task_id=row['id'], title=row['title'], files=row['files'], deps=row['deps'],
        prompt=prompt, approach=_text(plan / 'tasks' / row['id'] / 'approach.md'),
        plan_tasks=outcome.siblings(table, row['id']), file_excerpts=_excerpt(repo, row['files']),
        memory=_memory(repo), dwarf=dwarf, reviewer=reviewer,
        size=task_size(prompt=prompt, declared_files=row['files'], plan_tasks=len(table), deps=row['deps'],
                       decompose_level=level))


def _mint_run_uid(plan: Path) -> None:
    """The plan's dataset uid, before any request: otherwise each prediction's run is a
    legacy id that never joins the attempts the plan goes on to run."""
    try:
        from forge_dataset.capture import mint_run_uid
        mint_run_uid(plan)
    except Exception:
        pass


def _assigned(row: dict) -> bool:
    return row['dwarf'] not in ('', '-', 'UNASSIGNED') and row['qa'] not in ('', '-')


def first_pass(plan: Path, repo: Path, *, config: dict | None = None, budget_s: float | None = None) -> int:
    """One prediction per assigned row; returns how many were recorded."""
    config = config or load_config()
    if not site_ready('clef-first-pass', config):
        return 0
    deadline_at = time.time() + (budget_s if budget_s is not None else 2 * config.get('deadline_s', 5.0))
    table = rows(plan)
    todo = [r for r in table if _assigned(r)]
    if not todo:
        return 0
    _mint_run_uid(plan)
    revision = hashlib.sha256(_text(plan / 'tasks.tsv').encode()).hexdigest()
    resolve = Resolver(deadline_at)
    gate = threading.BoundedSemaphore(CONCURRENCY)
    recorded = []

    def one(row):
        with gate:
            if time.time() >= deadline_at:
                return
            state = task_state(plan, repo, row, table, resolve)
            if state is None or time.time() >= deadline_at:
                return
            result = ask(state, dict(first_pass=questions.first_pass()), site='clef-first-pass', run_dir=str(plan),
                         config=config, deadline_s=max(0.1, deadline_at - time.time()),
                         context=dict(task=row['id'], stage='plan', intended_attempt=1, plan_table=revision,
                                      assignment=dict(dwarf=row['dwarf'], qa=row['qa'])))
            p = noul(result, 'first_pass')
            if p is None:
                return
            try:
                write_json(plan / 'tasks' / row['id'] / 'clef.first_pass',
                           dict(p=p, model=result.get('model'), at=time.time(), plan_table=revision,
                                assignment=dict(dwarf=row['dwarf'], qa=row['qa'])))
                recorded.append(row['id'])
            except OSError:
                pass

    threads = [threading.Thread(target=one, args=(r,), daemon=True) for r in todo]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline_at - time.time()))
    return len(recorded)


def retry_pass(plan: Path, repo: Path, task: str, attempt: int, *, config: dict | None = None,
               budget_s: float | None = None) -> bool:
    """The prediction for a dwarf attempt above 1, with the findings it was handed."""
    config = config or load_config()
    out = plan / 'tasks' / task / ('clef.retry_pass.%d' % attempt)
    findings = _text(plan / 'tasks' / task / 'retry_findings.md')
    if attempt < 2 or not findings.strip() or out.exists() or not site_ready('clef-retry-pass', config):
        return False
    deadline_at = time.time() + (budget_s if budget_s is not None else 2 * config.get('deadline_s', 5.0))
    table = rows(plan)
    row = next((r for r in table if r['id'] == task), None)
    if row is None or not _assigned(row):
        return False
    state = task_state(plan, repo, row, table, Resolver(deadline_at))
    if state is None:
        return False
    state = outcome.retry(state, attempt=attempt, previous_review=findings)
    result = ask(state, dict(retry_pass=questions.retry_pass()), site='clef-retry-pass',
                 run_dir=str(plan), config=config, deadline_s=max(0.1, deadline_at - time.time()),
                 context=dict(task=task, stage='retry', attempt=attempt,
                              assignment=dict(dwarf=row['dwarf'], qa=row['qa'])))
    p = noul(result, 'retry_pass')
    if p is None:
        return False
    try:
        write_json(out, dict(p=p, model=result.get('model'), at=time.time(), attempt=attempt,
                             assignment=dict(dwarf=row['dwarf'], qa=row['qa'])))
    except OSError:
        return False
    return True


def main_first_pass(args) -> int:
    return 0 if first_pass(Path(args.plan), Path(args.repo)) else 3


def main_retry_pass(args) -> int:
    return 0 if retry_pass(Path(args.plan), Path(args.repo), args.task, args.attempt) else 3
