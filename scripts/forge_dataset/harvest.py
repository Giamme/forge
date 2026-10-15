"""Readers that turn forge's run artifacts into dataset events.

Live capture (capture.py) and the importer share these, so a run captured live and the
same run imported later produce the same rids and the same shapes, and INSERT OR IGNORE
keeps whichever arrived first.

Every reader takes what it needs from IMMUTABLE places whenever one exists: the
per-execution `attempts/<role>-<uuid>/` dir that forge-runtime.py fills at the end of
every dispatch. Task-level files (changes.diff, qa.last, status) are mutable and outlive
the execution that wrote them -- a dwarf that fails returns before changes.diff is
refreshed, so a naive read would credit the previous attempt's diff to the failed one.
begin() therefore snapshots each task file's (inode, mtime, size), and finish() only
attributes a file to this execution when that signature changed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import SCRIPTS, put_blob, put_file, sha256

DISPATCH_ROLES = ('dwarf', 'qa', 'planner')
PIPELINE_ROLES = ('task', 'solo', 'verification', 'parallel')
TASKS_COLS = ('id', 'deps', 'difficulty', 'files', 'dwarf', 'qa', 'title')
STRUCTURED = ('codex', 'claude')
# Plan files that change what a plan decides, added to its rid only when present (see plan_event).
PLAN_RID_LATER = ('decompose_level', 'shadow_qa', 'shadow_rate')

# Files a task or solo execution may write. begin() snapshots their signatures so
# finish() can tell this execution's outputs from what an earlier one left behind.
WATCHED = (
    'status', 'attempt', 'resume', 'noretry', 'changes.diff', 'changes.prev.diff', 'committed.diff',
    'drift.txt', 'guard.txt', 'qa.last', 'dwarf.last', 'qa.gate', 'known_issues.md',
    'retry_findings.md', 'reviewed.commit', 'base_ref', 'infra.txt', 'dwarf.memory', 'qa.memory',
    'baseline.capsule', 'capsule.md', 'subset.jev.txt', 'qa.effort.jev', 'qa.jev.json', 'jev.json',
    'verdict', 'existing.diff', 'start.tree', 'reviewed.tree', 'context.md',
    'verification.status', 'verification.exit', 'verification.command', 'verification.log',
    'verification.retry.log', 'verification.flaky', 'verification.coverage.txt',
    'verification.jev.json', 'verification.runs.tsv', 'verification.commit', 'verification.fingerprint',
    'results.tsv',
)

_VERDICT = re.compile(r'^FORGE_VERDICT: (PASS|FAIL)([ \t]+(—|–|-)( .*)?)?[ \t]*$')
_LEARNING = re.compile(r'^\s*FORGE_LEARNING:\s*(.+)$')

_contract = None


def contract():
    """forge-contract.py, loaded by path: its file name is not importable."""
    global _contract
    if _contract is None:
        spec = importlib.util.spec_from_file_location('forge_contract', SCRIPTS / 'forge-contract.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _contract = module
    return _contract


# --- small readers -------------------------------------------------------------------

def text(path: Path, limit: int | None = None) -> str | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if limit is not None:
        data = data[:limit]
    return data.decode('utf-8', 'replace')


def first_line(path: Path) -> str | None:
    value = text(path, 4096)
    if value is None:
        return None
    value = value.strip().splitlines()
    return value[0].strip() if value else ''


def kv(path: Path) -> dict:
    """key=value lines (`.resolved`, `.infra`, `infra.txt`); a repeated key keeps the last."""
    result = {}
    for line in (text(path) or '').splitlines():
        if '=' in line:
            key, _, value = line.partition('=')
            result[key.strip()] = value.strip()
    return result


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name('.%s.%d' % (path.name, os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def signature(path: Path) -> list | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_ino, st.st_mtime_ns, st.st_size]


def snapshot(root: Path) -> dict:
    return {name: signature(root / name) for name in WATCHED if (root / name).exists()}


def produced(root: Path, before: dict | None, name: str) -> bool:
    """Whether this execution wrote `name`. Unknown history (no snapshot) means no."""
    now = signature(root / name)
    if now is None or before is None:
        return False
    return before.get(name) != now


def verdict_of(message: str | None) -> str:
    """forge_verdict (forge-artifact.sh), in Python: only a standalone last line counts."""
    lines = [line for line in (message or '').splitlines() if line.strip()]
    if not lines:
        return 'UNKNOWN'
    match = _VERDICT.match(lines[-1].rstrip('\r'))
    return match.group(1) if match else 'UNKNOWN'


def learnings(message: str | None) -> list:
    out = []
    for line in (message or '').splitlines():
        match = _LEARNING.match(line)
        if match:
            out.append(match.group(1).strip()[:2000])
    return out


def findings(message: str | None) -> list:
    return parse_review(message)[0]


def parse_review(message: str | None) -> tuple[list, bool]:
    """(findings, parsed): parsed is False when the contract's parser raised."""
    if not message:
        return [], True
    try:
        parsed = contract().parse_findings(message)
    except Exception:
        return [], False
    return [dict(status=f.status, severity=f.severity, located=f.located, line=f.line,
                 head=f.text.splitlines()[0][:300] if f.text else '') for f in parsed], True


def review_completeness(verdict: str | None, found: list, parsed: bool = True) -> str | None:
    """How fully a review's findings can be read, so a graded label is never a guess.

    clean        a PASS with no finding blocks: worst severity is none
    graded       every CONFIRMED finding carries P0-P3 and none is UNSTATED
    ungraded     a CONFIRMED finding without a severity, an UNSTATED one, or a FAIL whose
                 reasons are not CONFIRMED findings
    unparseable  no verdict line, or the parser failed: an empty list here means nothing
    """
    if verdict not in ('PASS', 'FAIL') or not parsed:
        return 'unparseable'
    if not found:
        return 'clean' if verdict == 'PASS' else 'ungraded'
    confirmed = [f for f in found if f.get('status') == 'CONFIRMED']
    if any(f.get('status') == 'UNSTATED' for f in found) or any(f.get('severity') is None for f in confirmed):
        return 'ungraded'
    if verdict == 'FAIL' and not confirmed:
        return 'ungraded'
    return 'graded'


def tasks_rows(path: Path) -> dict:
    rows = {}
    for line in (text(path) or '').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        row = {name: (cells[i] if i < len(cells) else '') for i, name in enumerate(TASKS_COLS)}
        if row['id']:
            rows[row['id']] = row
    return rows


def split_list(value: str | None) -> list:
    if not value or value == '-':
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def diff_summary(diff: str | None) -> dict:
    """Changed files, per-file +/- counts, and a key that survives rebasing.

    The patch key hashes each file's added and removed lines (sorted, stripped), not the
    hunks' positions, so the same change landing on a moved base still matches.
    """
    files, current, binary = {}, None, set()
    for line in (diff or '').splitlines():
        if line.startswith('diff --git '):
            match = re.match(r'diff --git a/(.*) b/(.*)$', line)
            current = match.group(2) if match else line[11:]
            files.setdefault(current, [0, 0, []])
        elif current is None:
            continue
        elif line.startswith('Binary files') or line.startswith('GIT binary patch'):
            binary.add(current)
        elif line.startswith('+') and not line.startswith('+++'):
            files[current][0] += 1
            files[current][2].append('+' + line[1:].strip())
        elif line.startswith('-') and not line.startswith('---'):
            files[current][1] += 1
            files[current][2].append('-' + line[1:].strip())
    key = hashlib.sha256()
    for name in sorted(files):
        key.update(name.encode() + b'\0')
        for changed in sorted(files[name][2]):
            key.update(changed.encode('utf-8', 'replace') + b'\n')
    return dict(changed_files=sorted(files),
                numstat={name: [v[0], v[1]] for name, v in sorted(files.items())},
                binary=sorted(binary), patch_key=key.hexdigest()[:24] if files else None)


def blobs(**refs) -> dict:
    return {name: ref for name, ref in refs.items() if ref}


# --- where an execution belongs ------------------------------------------------------

def locate(root: str | Path, role: str | None = None) -> dict:
    """Which run, mode and task a dispatch or pipeline root belongs to, from the path alone.

    Env vars cannot carry this reliably (the orchestrator dispatches the planner by hand,
    Fractal steps run under tmux, and the importer has no env at all), so live capture
    and import both derive it from where forge put the files.
    """
    p = Path(root).resolve()
    parts = p.parts
    ctx = dict(mode='adhoc', scope='run', run_dir=p, plan=None, task=None, repo=None,
               run_id=p.name, link=None)
    if 'steps' in parts and 'nodes' in parts:
        steps = len(parts) - 1 - parts[::-1].index('steps')
        nodes = len(parts) - 1 - parts[::-1].index('nodes')
        task_dir = Path(*parts[:nodes])
        run_dir = task_dir.parent.parent if task_dir.parent.name == 'tasks' else task_dir
        request = read_json(task_dir / 'request.json') or {}
        ctx.update(mode='fractal', scope='step', run_dir=run_dir, run_id=run_dir.name,
                   task='%s/%s' % (task_dir.name, parts[nodes + 1]) if nodes + 1 < steps else task_dir.name,
                   repo=request.get('repo') or request.get('source'), link=request.get('output'))
        return ctx
    plan = None
    if p.parent.parent.name == 'shadow' and (p.parent.parent.parent / 'tasks.tsv').exists():
        # <plan>/shadow/<task>/<attempt>-<k>: a shadow review (forge-shadow.py) of that task.
        plan, ctx['task'], ctx['scope'], ctx['shadow'] = p.parent.parent.parent, p.parent.name, 'task', True
    elif p.parent.name == 'tasks' and (p.parent.parent / 'tasks.tsv').exists():
        plan, ctx['task'], ctx['scope'] = p.parent.parent, p.name, 'task'
    elif p.name.startswith('final-review-') and (p.parent / 'tasks.tsv').exists():
        plan, ctx['scope'] = p.parent, 'final_review'
    elif p.name == 'final-review' and p.parent.name.startswith('integrate-'):
        plan, ctx['scope'] = p.parent.parent, 'final_review'
    elif p.name.startswith('integrate-') and (p.parent / 'tasks.tsv').exists():
        plan, ctx['scope'] = p.parent, 'integrate'
    elif (p / 'tasks.tsv').exists() or (p / 'run_id').exists():
        plan, ctx['scope'] = p, 'plan'
    if plan is not None:
        ctx.update(mode='parallel', run_dir=plan, plan=plan,
                   repo=first_line(plan / 'repo'), run_id=first_line(plan / 'run_id') or plan.name)
        return ctx
    if (p / 'prompt.md').exists() and role != 'planner':
        ctx.update(mode='solo', repo=first_line(p / 'repo'))
    elif role == 'planner':
        ctx['mode'] = 'planning'
    if not ctx['repo']:
        for name in ('dwarf.resolved', 'planner.resolved'):
            repo = kv(p / name).get('repo')
            if repo:
                ctx['repo'] = repo
                break
    return ctx


def legacy_uid(run_dir: Path) -> str:
    return legacy_uid_for(run_dir, run_dir)


def legacy_uid_for(run_dir: Path, original: Path) -> str:
    """A stable id for a run captured before dataset.uid existed.

    `original` is where the run lived when it ran: an archive extracted elsewhere must
    get the same id as the dir it was made from. The earliest start is part of the key
    because /tmp plan paths get reused by later, unrelated runs.
    """
    starts = []
    for metrics in run_dir.glob('**/attempts/*/metrics.json'):
        if 'review-' in str(metrics.parent.parent.name):
            continue
        value = (read_json(metrics) or {}).get('wall_started')
        if isinstance(value, (int, float)):
            starts.append(value)
    earliest = '%.0f' % min(starts) if starts else ''
    return 'legacy-' + sha256(('%s\0%s\0%s' % (original, first_line(run_dir / 'run_id') or run_dir.name,
                                               earliest)).encode())[:24]


def run_uid(run_dir: Path, create: bool) -> str:
    path = Path(run_dir) / 'dataset.uid'
    value = first_line(path)
    if value:
        return value
    if create:
        from . import create_once
        import uuid
        return create_once(path, uuid.uuid4().hex)
    return legacy_uid(Path(run_dir))


# --- dispatch: one model call ----------------------------------------------------------

def dispatch_event(attempt: Path, root: Path, role: str, harness: str, rc, prov: dict, src: str = 'live') -> dict:
    metrics = read_json(attempt / 'metrics.json') or {}
    resolved = kv(attempt / (role + '.resolved'))
    infra = kv(attempt / (role + '.infra'))
    harness = harness or metrics.get('harness') or resolved.get('harness')
    message = text(attempt / (role + '.last'))
    final_source = 'structured' if harness in STRUCTURED else 'log_copy'
    delivery = resolved.get('delivery') or 'custom'
    orphans = text(attempt / (role + '.orphans'))
    effort = resolved.get('effort')
    verdict = verdict_of(message) if message is not None else None
    found, parsed = parse_review(message) if role == 'qa' else ([], True)
    shadow = prov.get('shadow')
    data = dict(
        # A shadow review keeps the qa files and contract but is never a task's review, so it
        # gets its own role: nothing that reads qa dispatches mistakes it for the one that decided.
        role='qa_shadow' if shadow and role == 'qa' else role, alias=resolved.get('alias'), harness=harness, model=resolved.get('model'),
        effort=None if not effort or effort.startswith('<') else effort,
        yolo=resolved.get('yolo') == 'ON', passthrough=resolved.get('passthrough') == 'yes',
        clamped=resolved.get('clamped'), timeout=as_int(resolved.get('timeout')),
        timeout_source=resolved.get('timeout_source'), delivery=delivery,
        duration_s=as_int(resolved.get('duration_s')), exit=as_int(rc), exit_code=metrics.get('exit_code'),
        elapsed_s=metrics.get('elapsed_s'), wall_started=metrics.get('wall_started'),
        wall_ended=prov.get('wall_ended'), timed_out=(attempt / (role + '.timedout')).exists(),
        infra=infra or None, orphans=len((orphans or '').splitlines()) or None,
        tokens=dict(input=metrics.get('input_tokens'), cached_input=metrics.get('cached_input_tokens'),
                    output=metrics.get('output_tokens')),
        prompt_bytes=metrics.get('prompt_bytes'), final_source=final_source,
        verdict=verdict, findings=found, learnings=learnings(message),
        findings_parse=review_completeness(verdict, found, parsed) if role == 'qa' else None,
        root=str(root), scope=prov.get('scope'), trigger=prov.get('trigger'),
        **({'shadow': shadow} if shadow else {}),
    )
    prompt = None if delivery == 'native_review' else put_file(attempt / (role + '.prompt'), 'prompt')
    last = put_file(attempt / (role + '.last'), 'message' if final_source == 'structured' else 'log')
    return dict(rid=attempt.name, kind='dispatch', ts=prov.get('wall_ended') or time.time(), src=src,
                run=prov.get('run'), run_id=prov.get('run_id'), mode=prov.get('mode'),
                task=prov.get('task'), attempt=prov.get('attempt_now'), parent=prov.get('parent'),
                inv=prov.get('inv'), data=dict(data, blobs=blobs(prompt=prompt, message=last)))


# --- pipeline: a task attempt, a solo run, a verification, a whole run ----------------

def attempt_number(prov: dict, end: int | None, status: str | None, infra: dict) -> tuple[int | None, dict]:
    """Which attempt this execution was, and how it was spent.

    The counter is bumped after begin(), refunded on a dwarf-stage infra stop, and left
    alone by a QA-only resume, so the number alone is ambiguous.
    """
    start = prov.get('attempt_start')
    flags = dict(resumed=False, refunded=False)
    if start is None or end is None:
        return end, flags
    if end > start:
        return end, flags
    if status == 'INFRA' and infra.get('stage') == 'dwarf':
        flags['refunded'] = True
        return start + 1, flags
    flags['resumed'] = bool(prov.get('resume'))
    return start, flags


def children(root: Path, parent: str) -> list:
    found = []
    for path in sorted(Path(root).glob('attempts/*/dataset.json')):
        if (read_json(path) or {}).get('parent') == parent:
            found.append(path.parent.name)
    return found


def task_event(attempt: Path, tdir: Path, rc, prov: dict, src: str = 'live') -> dict:
    before = prov.get('files')
    made = lambda name: produced(tdir, before, name)
    status = first_line(tdir / 'status')
    infra = kv(tdir / 'infra.txt') if made('infra.txt') else {}
    end = as_int(first_line(tdir / 'attempt'))
    number, flags = attempt_number(prov, end, status, infra)
    plan = tdir.parent.parent
    row = tasks_rows(plan / 'tasks.tsv').get(tdir.name, {})
    diff = text(tdir / 'changes.diff') if made('changes.diff') else None
    gate = first_line(tdir / 'qa.gate') if made('qa.gate') else None
    data = dict(
        status=status, exit=as_int(rc), attempt_start=prov.get('attempt_start'), attempt_end=end,
        trigger=prov.get('trigger'), **flags,
        dwarf=row.get('dwarf'), qa=row.get('qa'), difficulty=row.get('difficulty'),
        title=row.get('title'), deps=split_list(row.get('deps')), declared_files=split_list(row.get('files')),
        base_ref=first_line(tdir / 'base_ref'),
        reviewed_commit=first_line(tdir / 'reviewed.commit') if made('reviewed.commit') else None,
        drift=(text(tdir / 'drift.txt') or '').split() if made('drift.txt') else [],
        guard=(text(tdir / 'guard.txt') or '').splitlines() if made('guard.txt') else [],
        noretry=made('noretry'), gate=gate, infra=infra or None,
        identical_retry=bool(diff) and made('changes.prev.diff') and diff == text(tdir / 'changes.prev.diff'),
        produced=sorted(n for n in WATCHED if made(n)),
        carried=sorted(n for n in WATCHED if (tdir / n).exists() and not made(n)),
        children=children(tdir, attempt.name),
        **(diff_summary(diff) if diff is not None else dict(changed_files=[], numstat={}, binary=[], patch_key=None)),
    )
    refs = blobs(
        diff=put_blob(diff, 'diff') if diff is not None else None,
        retry_findings=put_file(tdir / 'retry_findings.md', 'message'),
        forge_note=put_file(tdir / 'qa.last', 'message') if made('qa.last') and not any(
            c.startswith('qa-') for c in data['children']) else None,
        known_issues=put_file(tdir / 'known_issues.md') if made('known_issues.md') else None,
        dwarf_memory=put_file(tdir / 'dwarf.memory') if made('dwarf.memory') else None,
        qa_memory=put_file(tdir / 'qa.memory') if made('qa.memory') else None,
        capsule=put_file(tdir / 'baseline.capsule') if made('baseline.capsule') else None,
        subset=put_file(tdir / 'subset.jev.txt') if made('subset.jev.txt') else None,
        qa_effort_jev=put_file(tdir / 'qa.effort.jev') if made('qa.effort.jev') else None,
        review_triage_jev=put_file(tdir / 'qa.jev.json') if made('qa.jev.json') else None,
        routing_jev=put_file(tdir / 'jev.json'),
        prompt=put_file(tdir / 'prompt.md', 'prompt'), approach=put_file(tdir / 'approach.md', 'prompt'),
    )
    data['blobs'] = refs
    return dict(rid=attempt.name, kind='attempt', ts=prov.get('wall_ended') or time.time(), src=src,
                run=prov.get('run'), run_id=prov.get('run_id'), mode=prov.get('mode'), task=tdir.name,
                attempt=number, parent=prov.get('parent'), inv=prov.get('inv'), data=data)


def solo_event(attempt: Path, run: Path, rc, prov: dict, src: str = 'live') -> dict:
    before = prov.get('files')
    made = lambda name: produced(run, before, name)
    diff = text(run / 'changes.diff') if made('changes.diff') else None
    starts = sorted(((read_json(m) or {}).get('wall_started') or 0, m.parent.name)
                    for m in run.glob('attempts/solo-*/metrics.json'))
    number = next((i + 1 for i, (_, name) in enumerate(starts) if name == attempt.name), None)
    data = dict(
        verdict=first_line(run / 'verdict') if made('verdict') else None, exit=as_int(rc),
        goal=first_line(run / 'goal.txt'), trigger=prov.get('trigger'),
        guard=(text(run / 'guard.txt') or '').splitlines() if made('guard.txt') else [],
        start_tree=first_line(run / 'start.tree') if made('start.tree') else None,
        reviewed_tree=first_line(run / 'reviewed.tree') if made('reviewed.tree') else None,
        produced=sorted(n for n in WATCHED if made(n)), children=children(run, attempt.name),
        **(diff_summary(diff) if diff is not None else dict(changed_files=[], numstat={}, binary=[], patch_key=None)),
    )
    data['blobs'] = blobs(
        diff=put_blob(diff, 'diff') if diff is not None else None,
        existing_diff=put_file(run / 'existing.diff', 'diff') if made('existing.diff') else None,
        prompt=put_file(run / 'prompt.md', 'prompt'), goal=put_file(run / 'goal.txt'),
        request=put_file(run / 'request.md', 'prompt'),
        dwarf_memory=put_file(run / 'dwarf.memory') if made('dwarf.memory') else None,
        qa_memory=put_file(run / 'qa.memory') if made('qa.memory') else None,
    )
    return dict(rid=attempt.name, kind='attempt', ts=prov.get('wall_ended') or time.time(), src=src,
                run=prov.get('run'), run_id=prov.get('run_id'), mode='solo', task=None, attempt=number,
                parent=prov.get('parent'), inv=prov.get('inv'), data=data)


def verification_events(attempt: Path, out: Path, rc, prov: dict, src: str = 'live') -> list:
    before = prov.get('files')
    made = lambda name: produced(out, before, name) if before is not None else (out / name).exists()
    plan = out.parent if out.name.startswith('integrate-') else out
    repo = first_line(plan / 'repo')
    command = first_line(out / 'verification.command') if made('verification.command') else None
    jev = read_json(out / 'verification.jev.json') if made('verification.jev.json') else None
    if not command:
        source = None
    elif (plan / 'verify_cmd').exists():
        source = 'flag'
    elif jev and jev.get('discovered'):
        source = 'jev'
    elif repo and (Path(repo) / '.forge' / 'verify').exists():
        source = 'repo'
    else:
        source = 'unknown'
    runs = []
    if made('verification.runs.tsv'):
        for line in (text(out / 'verification.runs.tsv') or '').splitlines():
            cells = line.split('\t')
            if len(cells) >= 5 and cells[0].isdigit():
                runs.append(dict(n=int(cells[0]), kind=cells[1], exit=as_int(cells[2]),
                                 tree_changed=cells[3] == '1', duration_s=as_int(cells[4]),
                                 log=cells[5] if len(cells) > 5 else None))
    elif command and made('verification.log'):
        runs.append(dict(n=1, kind='initial', exit=as_int(first_line(out / 'verification.exit')),
                         tree_changed=None, duration_s=None, log='verification.log'))
        if made('verification.retry.log'):
            runs.append(dict(n=2, kind='rerun', exit=None, tree_changed=None, duration_s=None,
                             log='verification.retry.log'))
    status = first_line(out / 'verification.status') if made('verification.status') else None
    data = dict(
        scope='integrate' if out != plan else 'run', status=status, exit=as_int(rc), command=command,
        commit=first_line(out / 'verification.commit') if made('verification.commit') else None,
        command_source=source, retries_allowed=as_int(first_line(plan / 'verify_retries')),
        flaky=kv(out / 'verification.flaky') if made('verification.flaky') else None,
        jev=jev, runs=[{k: v for k, v in r.items() if k != 'log'} for r in runs],
        blobs=blobs(coverage=put_file(out / 'verification.coverage.txt') if made('verification.coverage.txt') else None),
    )
    base = dict(src=src, run=prov.get('run'), run_id=prov.get('run_id'), mode=prov.get('mode'),
                parent=prov.get('parent'), inv=prov.get('inv'))
    events = [dict(base, rid=attempt.name, kind='verification', ts=prov.get('wall_ended') or time.time(), data=data)]
    for r in runs:
        log = put_file(out / r['log'], 'log') if r.get('log') else None
        events.append(dict(base, rid='%s:run:%d' % (attempt.name, r['n']), kind='verify_run',
                           ts=prov.get('wall_ended') or time.time(), parent=attempt.name,
                           data=dict({k: v for k, v in r.items() if k != 'log'}, command=command,
                                     blobs=blobs(log=log))))
    return events


def run_end_event(attempt: Path, plan: Path, rc, prov: dict, src: str = 'live') -> dict:
    results = {}
    for line in (text(plan / 'results.tsv') or '').splitlines():
        cells = line.split('\t')
        if len(cells) >= 2:
            results[cells[0]] = cells[1]
    repo = first_line(plan / 'repo')
    data = dict(exit=as_int(rc), results=results,
                accepted_integration=first_line(plan / 'accepted.integration'),
                blobs=blobs(memory=put_file(Path(repo) / '.forge' / 'memory.md') if repo else None))
    return dict(rid=attempt.name, kind='run_end', ts=prov.get('wall_ended') or time.time(), src=src,
                run=prov.get('run'), run_id=prov.get('run_id'), mode='parallel', parent=prov.get('parent'),
                inv=prov.get('inv'), data=data)


# --- the plan: every decision made before the first dispatch ----------------------------

def _git(repo: str, *args: str, limit: int = 1 << 20) -> str | None:
    """Read-only object-store queries; never touches the working tree or index."""
    try:
        result = subprocess.run(['git', '-C', repo, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout[:limit].decode('utf-8', 'replace')


def decision_snapshot(repo: str, rows: dict, head: str | None) -> dict | None:
    """What the declared files looked like when the plan was made.

    Routing, drift and coupling questions are about code as it was at decision time,
    and by the time anyone trains on this the repo has moved on.
    """
    if not repo or not head:
        return None
    excerpts, total = {}, 0
    for row in rows.values():
        for name in split_list(row.get('files')):
            if name in excerpts or len(excerpts) >= 40 or total >= 256 << 10:
                continue
            if name.endswith('/'):
                continue
            body = _git(repo, 'show', '%s:%s' % (head, name), limit=4096)
            if body is not None:
                excerpts[name] = body
                total += len(body)
    return excerpts


def plan_event(plan: Path, prov: dict, src: str = 'live', inventory: bool = True, head: str | None = None) -> dict:
    rows = tasks_rows(plan / 'tasks.tsv')
    declared_path = plan / 'tasks.declared.tsv'
    declared = tasks_rows(declared_path) if declared_path.exists() else None
    repo = first_line(plan / 'repo')
    # Live, the plan is made against HEAD. An import is not: HEAD has moved on, so the caller
    # passes the commit the tasks branched from.
    if head is None and src == 'live' and repo:
        head = (_git(repo, 'rev-parse', 'HEAD') or '').strip() or None
    per_task = {}
    for tid, row in rows.items():
        tdir = plan / 'tasks' / tid
        routing = read_json(tdir / 'jev.json')
        declared_tier = (declared or {}).get(tid, {}).get('difficulty') or (routing or {}).get('declared')
        if declared_tier and declared_tier != row.get('difficulty') and routing and routing.get('may_act'):
            source = 'jev-act'
        elif (plan / 'planner').exists() and list(plan.glob('attempts/planner-*')):
            source = 'planner'
        else:
            source = 'orchestrator'
        per_task[tid] = dict(row, declared_difficulty=declared_tier, difficulty_source=source,
                             routing=routing, prompt=put_file(tdir / 'prompt.md', 'prompt'),
                             approach=put_file(tdir / 'approach.md', 'prompt'))
    waves = {}
    for line in (text(plan / 'waves.tsv') or '').splitlines():
        cells = line.split('\t')
        if len(cells) >= 2:
            waves[cells[1]] = as_int(cells[0])
    # Inputs that joined the plan after its rid scheme was fixed are hashed only when present,
    # so every plan already in a store keeps its rid and re-importing an archive adds nothing.
    # Options older than that (timeout, qa_threshold, ...) stay out for the same reason.
    later = {name: first_line(plan / name) for name in PLAN_RID_LATER if (plan / name).exists()}
    inputs = json.dumps([text(plan / 'goal.txt'), text(plan / 'tasks.tsv'), text(declared_path),
                         {t: [r['prompt'], r['approach']] for t, r in per_task.items()}] + ([later] if later else []),
                        sort_keys=True, default=str)
    snapshot_excerpts = decision_snapshot(repo, rows, head) if repo else None
    listing = _git(repo, 'ls-tree', '-r', '--name-only', head) if inventory and repo and head else None
    data = dict(
        goal=first_line(plan / 'goal.txt'), decompose_level=first_line(plan / 'decompose_level'),
        planner=first_line(plan / 'planner'), author='planner' if list(plan.glob('attempts/planner-*')) else 'orchestrator',
        planner_dispatches=sorted(p.name for p in plan.glob('attempts/planner-*')),
        head=head, tasks=per_task, waves=waves, deferrals=(text(plan / 'deferrals.txt') or '').splitlines(),
        pools=read_json(plan / 'fractal-routing.json'), jev_selection=read_json(plan / 'jev-selection.json'),
        fractal_selection=read_json(plan / 'fractal-selection.json'),
        jev_coupling=read_json(plan / 'jev-coupling.json'),
        options={name: first_line(plan / name) for name in ('qa_threshold', 'verify_retries', 'timeout', 'final_qa',
                                                            'shadow_qa', 'shadow_rate')
                 if (plan / name).exists()},
        flags=sorted(name for name in ('no_memory', 'no_ripwire', 'yolo_dwarf', 'yolo_qa') if (plan / name).exists()),
        blobs=blobs(goal=put_file(plan / 'goal.txt'), request=put_file(plan / 'request.md', 'prompt'),
                    tasks=put_file(plan / 'tasks.tsv'), tasks_declared=put_file(declared_path),
                    jev_routing=put_file(plan / 'jev-routing.tsv'),
                    snapshot=put_blob(json.dumps(snapshot_excerpts, sort_keys=True), 'request') if snapshot_excerpts else None,
                    inventory=put_blob(listing, 'request') if listing else None,
                    memory=put_file(Path(repo) / '.forge' / 'memory.md') if repo else None),
    )
    return dict(rid='plan:%s:%s' % (prov.get('run'), sha256(inputs.encode())[:16]), kind='plan',
                ts=time.time(), src=src, run=prov.get('run'), run_id=prov.get('run_id'), mode='parallel',
                inv=prov.get('inv'), data=data)


# --- Fractal ------------------------------------------------------------------------------

def fractal_context(task_dir: Path, create: bool) -> dict:
    request = read_json(task_dir / 'request.json') or {}
    run_dir = task_dir.parent.parent if task_dir.parent.name == 'tasks' else task_dir
    return dict(run=run_uid(run_dir, create=create), run_id=run_dir.name, repo=request.get('repo'),
                link=request.get('output'))


def fractal_step_event(task_dir: Path, node: dict, step_dir: Path, ctx: dict, *, phase: str | None = None,
                       iteration=None, rc=None, cost=None, resolved=None, src: str = 'live') -> dict:
    request = read_json(step_dir / 'request.json') or {}
    role = request.get('role')
    phase = phase or ('planning' if role == 'planner' else 'implementation')
    decomposition = node.get('decomposition') or {}
    dispatches = sorted(p.name for p in step_dir.glob('attempts/*-*'))
    if rc is None and dispatches:
        rc = (read_json(step_dir / 'attempts' / dispatches[-1] / 'metrics.json') or {}).get('exit_code')
    data = dict(node=node.get('id'), parent_node=node.get('parent'), phase=phase, iteration=iteration,
                exit=as_int(rc), cost=cost, resolved_model=resolved, model=node.get('model'),
                difficulty=node.get('difficulty'), status=node.get('status'), goal=node.get('goal'),
                decision=decomposition.get('decision'), decomposition_attempts=decomposition.get('attempts'),
                link=ctx['link'], dispatches=dispatches)
    return dict(rid='fractal_step:' + step_dir.name, kind='fractal_step', ts=time.time(), src=src, run=ctx['run'],
                run_id=ctx['run_id'], mode='fractal', task='%s/%s' % (task_dir.name, node.get('id')),
                attempt=as_int(iteration), data=data)


def fractal_task_events(task_dir: Path, ctx: dict, src: str = 'live') -> list:
    """Every node's state and the task outcome, keyed by content: once per distinct state."""
    events = []
    for path in sorted(task_dir.glob('nodes/*/node.json')):
        raw = text(path) or ''
        node = read_json(path) or {}
        events.append(dict(rid='fractal_node:%s/%s:%s' % (ctx['run'], node.get('id'), sha256(raw.encode())[:16]),
                           kind='fractal_node', ts=time.time(), src=src, run=ctx['run'], run_id=ctx['run_id'],
                           mode='fractal', task='%s/%s' % (task_dir.name, node.get('id')),
                           attempt=as_int(node.get('iteration')),
                           data=dict(node, blobs=blobs(candidate=put_file(path.parent / 'candidate.diff', 'diff')))))
    outcome = read_json(task_dir / 'outcome.json')
    if outcome is not None:
        raw = json.dumps(outcome, sort_keys=True)
        events.append(dict(rid='fractal_outcome:%s/%s:%s' % (ctx['run'], task_dir.name, sha256(raw.encode())[:16]),
                           kind='event', ts=time.time(), src=src, run=ctx['run'], run_id=ctx['run_id'],
                           mode='fractal', task=task_dir.name,
                           data=dict(outcome, name='fractal_outcome', link=ctx['link'],
                                     blobs=blobs(diff=put_file(task_dir / 'changes.diff', 'diff'),
                                                 qa=put_file(task_dir / 'qa.last', 'message')))))
    return events
