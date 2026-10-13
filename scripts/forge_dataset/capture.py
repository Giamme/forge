"""Live capture: called from forge-runtime.py's begin()/finish() and from point events.

Every function here is an entry point a running pipeline calls, so every one of them
is fail-open: errors go to errors.log, nothing is printed (begin()'s stdout is the
attempt path the shell captures), and nothing is raised.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import enabled, location_refused, log_error, put_blob, put_file, record
from . import harvest as h


def _environment() -> dict:
    return dict(parent=os.environ.get('FORGE_DATASET_PARENT') or None,
                inv=os.environ.get('FORGE_DATASET_INVOCATION') or None,
                trigger=os.environ.get('FORGE_DATASET_TRIGGER') or None)


def on_begin(attempt: str, root: str, role: str) -> None:
    """Write the execution's provenance into its immutable attempt dir and a start receipt.

    The provenance travels with the attempt dir, so a plan split or combined later (which
    copies attempt dirs unchanged) still names the run the execution really belonged to.
    """
    if not enabled():
        return
    try:
        attempt_dir, root_dir = Path(attempt), Path(root)
        ctx = h.locate(root_dir, role)
        if location_refused(ctx['repo']):
            return
        uid = h.run_uid(ctx['run_dir'], create=True)
        env = _environment()
        if role == 'task' and not env['trigger']:
            env['trigger'] = 'initial'
        prov = dict(
            schema=1, run=uid, run_id=ctx['run_id'], mode=ctx['mode'], scope=ctx['scope'],
            plan=str(ctx['plan']) if ctx['plan'] else None, task=ctx['task'], repo=ctx['repo'],
            link=ctx['link'], role=role, root=str(root_dir), wall_started_ns=time.time_ns(), **env,
        )
        if role in h.PIPELINE_ROLES:
            prov['files'] = h.snapshot(root_dir)
        if role == 'task':
            prov['attempt_start'] = h.as_int(h.first_line(root_dir / 'attempt')) or 0
            prov['resume'] = h.first_line(root_dir / 'resume') or None
        h.write_json(attempt_dir / 'dataset.json', prov)
        record([dict(rid=attempt_dir.name + ':start', kind='start', ts=time.time(), run=uid,
                     run_id=ctx['run_id'], mode=ctx['mode'], task=ctx['task'], parent=env['parent'],
                     inv=env['inv'], data=dict(role=role, root=str(root_dir), scope=ctx['scope'],
                                               trigger=env['trigger'], link=ctx['link']))],
               repo=ctx['repo'], where='begin')
    except Exception as error:
        log_error('begin', error)


def provenance_for(attempt_dir: Path, root_dir: Path, role: str) -> dict:
    """The provenance begin() wrote, or one reconstructed for an attempt dir without it."""
    prov = h.read_json(attempt_dir / 'dataset.json')
    if prov is None:
        ctx = h.locate(root_dir, role)
        prov = dict(run=h.run_uid(ctx['run_dir'], create=False), run_id=ctx['run_id'], mode=ctx['mode'],
                    scope=ctx['scope'], plan=str(ctx['plan']) if ctx['plan'] else None, task=ctx['task'],
                    repo=ctx['repo'], link=ctx['link'], files=None)
    return prov


def events_for(attempt_dir: Path, root_dir: Path, role: str, harness: str, rc, prov: dict, src: str) -> list:
    if harness != 'pipeline':
        if prov.get('scope') == 'task':
            prov['attempt_now'] = h.as_int(h.first_line(root_dir / 'attempt'))
        return [h.dispatch_event(attempt_dir, root_dir, role, harness, rc, prov, src)]
    if role == 'task':
        return [h.task_event(attempt_dir, root_dir, rc, prov, src)]
    if role == 'solo':
        return [h.solo_event(attempt_dir, root_dir, rc, prov, src)]
    if role == 'verification':
        return h.verification_events(attempt_dir, root_dir, rc, prov, src)
    if role == 'parallel':
        return [h.run_end_event(attempt_dir, root_dir, rc, prov, src)]
    return []


def on_finish(attempt: str, root: str, role: str, harness: str, rc) -> None:
    if not enabled():
        return
    try:
        attempt_dir, root_dir = Path(attempt), Path(root)
        prov = provenance_for(attempt_dir, root_dir, role)
        if location_refused(prov.get('repo')):
            return
        prov['wall_ended'] = time.time()
        events = events_for(attempt_dir, root_dir, role, harness, rc, prov, 'live')
        record(events, repo=prov.get('repo'), where='finish')
    except Exception as error:
        log_error('finish', error)


def point(name: str, directory: str | None, task: str | None = None, values: dict | None = None,
          files: dict | None = None, kind: str = 'event', texts: dict | None = None,
          repo: str | None = None) -> None:
    """A fact that would be gone by the time any EXIT trap could read it.

    Merge conflicts are aborted, combine rolls back, integrate removes its candidate
    worktree; this records each one at the moment it is still on disk.
    """
    if not enabled():
        return
    try:
        env = _environment()
        ctx = h.locate(Path(directory)) if directory else dict(run_dir=None, run_id=None, mode=None, repo=None)
        if location_refused(repo or ctx.get('repo')):
            return
        uid = h.run_uid(ctx['run_dir'], create=False) if ctx.get('run_dir') else None
        data = dict(values or {}, name=name)
        refs = {}
        for label, path in (files or {}).items():
            ref = put_file(path, 'diff' if path.endswith('.diff') else 'text')
            if ref:
                refs[label] = ref
        for label, value in (texts or {}).items():
            if value:
                refs[label] = put_blob(value, 'log')
        task = task or ctx.get('task')
        data['blobs'] = refs
        rid = 'event:%s:%s' % (name, h.sha256(json.dumps([uid, task, time.time_ns(), data], sort_keys=True,
                                                         default=str).encode())[:24])
        record([dict(rid=rid, kind=kind, ts=time.time(), run=uid, run_id=ctx.get('run_id'),
                     mode=ctx.get('mode'), task=task, parent=env['parent'], inv=env['inv'], data=data)],
               repo=repo or ctx.get('repo'), where='event:' + name)
    except Exception as error:
        log_error('event:' + name, error)


def jev(*, site, key, canonical: bytes, ok, reason, latency, model, usage, questions, answers,
        run_dir, acting, shadow) -> None:
    """One Jev request: the full state and questions it judged, and what came back.

    The request blob's original sha equals jev.jsonl's `key`, so a run dir that still
    exists joins to it, and a judgment can later be replayed against any other model.
    """
    if not enabled():
        return
    try:
        env = _environment()
        ctx = h.locate(Path(run_dir)) if run_dir else dict(run_dir=None, run_id=None, mode=None, task=None, repo=None)
        if location_refused(ctx.get('repo')):
            return
        uid = h.run_uid(ctx['run_dir'], create=False) if ctx.get('run_dir') else None
        at = time.time()
        rid = 'jev:' + h.sha256(('%s\0%r\0%d\0%d' % (key, at, os.getpid(), time.perf_counter_ns())).encode())[:24]
        data = dict(site=site, key=key, ok=ok, reason=reason, latency_s=latency, model=model, usage=usage,
                    questions=questions, answers=answers, acting=acting, shadow=shadow,
                    run_dir=str(run_dir) if run_dir else None, blobs=dict(request=put_blob(canonical, 'request')))
        record([dict(rid=rid, kind='jev', ts=at, run=uid, run_id=ctx.get('run_id'), mode=ctx.get('mode'),
                     task=ctx.get('task'), parent=env['parent'], inv=env['inv'], data=data)],
               repo=ctx.get('repo'), where='jev:' + site)
    except Exception as error:
        log_error('jev:' + str(site), error)


def fractal_step(task_dir, node: dict, step_dir, *, phase: str, iteration, rc, cost, resolved) -> None:
    """One Fractal step, keyed by its own step uuid.

    node.json is mutable (a retry resets `iteration` to 0), so a step is recorded by
    the uuid of the dir it ran in rather than by (node, iteration), which can repeat.
    Called from worker threads: no signals, a connection per call.
    """
    if not enabled():
        return
    try:
        task_dir, step_dir = Path(task_dir), Path(step_dir)
        ctx = h.fractal_context(task_dir, create=True)
        if location_refused(ctx['repo']):
            return
        record([h.fractal_step_event(task_dir, node, step_dir, ctx, phase=phase, iteration=iteration, rc=rc,
                                     cost=cost, resolved=resolved)], repo=ctx['repo'], where='fractal_step')
    except Exception as error:
        log_error('fractal_step', error)


def fractal_outcome(task_dir) -> None:
    """Every node's final state and the task outcome, once per distinct state."""
    if not enabled():
        return
    try:
        task_dir = Path(task_dir)
        ctx = h.fractal_context(task_dir, create=True)
        if location_refused(ctx['repo']):
            return
        record(h.fractal_task_events(task_dir, ctx), repo=ctx['repo'], where='fractal_outcome')
    except Exception as error:
        log_error('fractal_outcome', error)


def plan_snapshot(plan: str) -> None:
    """Every routing, decomposition and scheduling decision made before the first dispatch."""
    if not enabled():
        return
    try:
        plan_dir = Path(plan)
        if not (plan_dir / 'tasks.tsv').exists():
            return
        ctx = h.locate(plan_dir)
        if location_refused(ctx['repo']):
            return
        prov = dict(run=h.run_uid(plan_dir, create=True), run_id=ctx['run_id'],
                    inv=_environment()['inv'])
        record([h.plan_event(plan_dir, prov)], repo=ctx['repo'], where='plan')
    except Exception as error:
        log_error('plan', error)


def invocation(command: str, directory: str | None, rc, started: float | None, argv: list,
               pid: str | None = None) -> None:
    """One forge-parallel.sh subcommand: what was asked, how it ended, and what it left.

    Artifacts a subcommand writes into a dir named after its own pid (final-review-*-PID,
    integrate-*-PID) are read here, after they exist and before anything deletes them.
    """
    if not enabled():
        return
    try:
        env = _environment()
        plan = Path(directory) if directory and Path(directory).is_dir() else None
        ctx = h.locate(plan) if plan else dict(run_dir=None, run_id=None, mode=None, repo=None)
        if location_refused(ctx.get('repo')):
            return
        # `plan` is what makes a plan dir a run, so it may mint the id; every other command
        # finds the one plan (or a dispatch's begin) already wrote.
        uid = h.run_uid(plan, create=command == 'plan') if plan and (plan / 'tasks.tsv').exists() else None
        data = dict(command=command, argv=argv, exit=h.as_int(rc), plan=str(plan) if plan else None,
                    duration_s=round(time.time() - float(started), 3) if started else None)
        refs = {}
        if plan and pid:
            produced = []
            for path in sorted(plan.glob('*-%s' % pid)):
                if not path.is_dir():
                    continue
                entry = dict(dir=path.name)
                for name in ('verdict', 'verification.status', 'verification.exit', 'verification.command'):
                    value = h.first_line(path / name)
                    if value is not None:
                        entry[name] = value
                inner = path / 'final-review'
                if inner.is_dir():
                    entry['final_review_verdict'] = h.first_line(inner / 'verdict')
                    entry['final_review_meta'] = h.kv(inner / 'review.meta') or None
                if (path / 'review.meta').exists():
                    entry['review_meta'] = h.kv(path / 'review.meta')
                    ref = put_file(path / 'qa.last', 'message')
                    if ref:
                        refs['final_review_message'] = ref
                    ref = put_file(path / 'combined.diff', 'diff')
                    if ref:
                        refs['final_review_diff'] = ref
                produced.append(entry)
            data['produced'] = produced
            data['final_review_verdict'] = h.first_line(plan / 'final-review.verdict')
        if command == 'accept' and plan and len(argv) > 0:
            task = next((a for a in argv if not a.startswith('-') and (plan / 'tasks' / a).is_dir()), None)
            if task:
                data['task'] = task
                data['accepted_by'] = h.text(plan / 'tasks' / task / 'accepted.by')
                data['status_after'] = h.first_line(plan / 'tasks' / task / 'status')
                ref = put_file(plan / 'tasks' / task / 'known_issues.md')
                if ref:
                    refs['known_issues'] = ref
        if command == 'retry' and plan and len(argv) > 0:
            data['task'] = next((a for a in argv if (plan / 'tasks' / a).is_dir()), None)
        data['blobs'] = refs
        record([dict(rid='invocation:%s' % (env['inv'] or h.sha256(repr((command, argv, started)).encode())[:24]),
                     kind='invocation', ts=time.time(), run=uid, run_id=ctx.get('run_id'), mode=ctx.get('mode'),
                     task=data.get('task'), inv=env['inv'], data=data)],
               repo=ctx.get('repo'), where='invocation:' + command)
        if command == 'plan' and plan:
            plan_snapshot(str(plan))
    except Exception as error:
        log_error('invocation:' + command, error)
