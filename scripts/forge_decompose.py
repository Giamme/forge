#!/usr/bin/env python3
"""Recursive decomposition (#153): one node model for every level, the limits a plan freezes,
and the split of a task into a sub-plan.

A task of a plan may be split into subtasks. The subtasks are an ordinary plan, the task's
sub-plan, kept under the task: `<plan>/tasks/<id>/sub/`. It runs on forge's own scheduler,
worktrees, routing and QA, merges into the task's branch, and the task is then reviewed on its
cumulative diff. A subtask can split again, down to the depth the plan allows.

Every plan directory, a sub-plan included, keeps the decisions about its own tasks in
`decomposition.json`. One record per task (the node):

    id, parent, goal, files, deps, difficulty   what the node is
    decision                                    atomic | split | bounded
    reason, by, at                              why, who decided (planner, user, auto, default), when
    limits_used                                 the node's level, the subtasks in the tree and the
                                                children proposed when it was decided

`atomic` runs as one task. `split` has a sub-plan. `bounded` would have split but a limit said
no, and it runs as one task. The limits (depth, children per split, subtasks in the whole tree)
live in the root plan's file: `plan` sets them, the table shows them, and the first `run`
freezes them.

Command line, for the shell runners:
    forge_decompose.py limits <plan> [--depth N] [--children N] [--nodes N]   set (root plan, not frozen) and print
    forge_decompose.py freeze <plan>                 freeze the limits (the first run)
    forge_decompose.py split <plan> <task> --proposal FILE --reason TEXT [--by B] [--start keep|fresh]
                                                     0 the sub-plan was created | 3 refused | 4 bounded | 5 kept whole
    forge_decompose.py record <plan> <task> --decision D --reason TEXT [--by B]
    forge_decompose.py settle <plan> [--by B]        apply the plan's stored proposals, record the rest atomic
    forge_decompose.py tree <plan> [--json]          the nodes of the plan, through every level
    forge_decompose.py state <plan> <task>           none | open | merged
    forge_decompose.py waiting <plan> <task>         one line per subtask that has not merged
    forge_decompose.py root <plan>                   the root plan of a sub-plan (itself for a root)
    forge_decompose.py subs <plan>                   every sub-plan directory under the plan, parents first
    forge_decompose.py later <plan> [--json]         the stages that follow this plan (goal -> plans)
    forge_decompose.py next <plan> [--dir NEW]       make the directory of the next stage's plan: its request,
                                                     its goal and its place in the chain; prints the directory
    forge_decompose.py slot --root DIR --cap N --while-pid PID --ready FILE   hold one slot of the tree
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_legacy  # noqa: E402

FILE = 'decomposition.json'
SUB = 'sub'                 # <plan>/tasks/<id>/sub: the task's sub-plan
PARENT = 'parent.json'      # in a sub-plan: the task it belongs to, and how the split started
MERGED = 'sub.merged'       # <plan>/tasks/<id>/sub.merged: the sub-plan's work is on the task's branch
WAITING = 'sub.waiting'     # why the task waits for its subtasks, for the run summary
PROPOSAL = 'split.proposal.json'   # <plan>/tasks/<id>/: subtasks a planner proposed, applied by settle
REASON = 'split.reason'            # <plan>/tasks/<id>/: why a planner kept the task whole, one line
SLOTS = 'tree.slots'        # in the root plan: one lock file per dispatch the tree may run at once
LATER = 'later.json'        # the stages a planner proposed after this plan: {"later": [{title, goal}, ...]}
CHAIN = 'chain.json'        # in a plan `next` made: {first, stage, previous, remaining}
VERSION = 1

DEFAULT_LIMITS = {'depth': 2, 'children': 6, 'nodes': 24}
# What a limit may be set to: 0 depth or 0 nodes turns splitting off; a split has at least two children.
LIMIT_RANGE = {'depth': (0, 6), 'children': (2, 20), 'nodes': (0, 500)}
LIMIT_WORDS = {'depth': 'levels of subtasks', 'children': 'subtasks per split', 'nodes': 'subtasks in the whole run'}
DECISIONS = ('atomic', 'split', 'bounded')
DECIDERS = ('planner', 'user', 'auto', 'default')
STARTS = ('keep', 'fresh')
DIFFICULTIES = ('low', 'medium', 'high')
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
TSV_HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle'
MAX_RUN_ID = 128            # a nested run id names branches and directories (forge runs rm takes no longer one)
# A task in one of these has nothing left to split, or is being worked on right now.
NOT_SPLITTABLE = ('MERGED', 'RUNNING', 'PASS', 'IMPLEMENTED')
# Plan files a sub-plan takes from its parent: the cost-bearing choices made for the plan apply to
# its subtasks too. Not verification (the parent's review and the root's verification judge the
# result), not groups, not the brief or the summaries, not `after`.
OPTION_FILES = ('no_ripwire', 'no_memory', 'yolo_dwarf', 'yolo_qa', 'reviewer_session', 'implementer_session',
                'fast', 'pre_review_tests', 'pre_review_parallel', 'fail_first', 'qa_threshold', 'timeout',
                'setup_cmd', 'escalate_after', 'retry_per_model', 'max_attempts', 'converge_window',
                'infra_fallback', 'infra_backoff', forge_legacy.POOLS, 'qa-routing.json', 'jev-selection.json',
                'profile.json', 'repo.identity', 'planner', 'split_planner', 'auto_split')


class Invalid(Exception):
    """A split that cannot be made. Nothing was written."""


class Bounded(Exception):
    """A split a limit refuses. `limit` names it."""

    def __init__(self, limit: str, message: str):
        super().__init__(message)
        self.limit = limit


class Atomic(Exception):
    """A proposal of fewer than two subtasks: the node stays whole."""


# --- files ---------------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('%s.tmp-%d' % (path.name, os.getpid()))
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _first_line(path: Path) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.readline().strip()
    except OSError:
        return ''


def load(plan) -> dict:
    """The plan's decomposition.json, always a well-formed dict (an unreadable file reads as empty)."""
    data = _read_json(Path(plan) / FILE)
    if not isinstance(data, dict):
        data = {}
    nodes = data.get('nodes')
    data['nodes'] = {k: v for k, v in nodes.items() if isinstance(v, dict)} if isinstance(nodes, dict) else {}
    data['version'] = VERSION
    return data


def save(plan, data: dict) -> None:
    _write_atomic(Path(plan) / FILE, json.dumps(data, indent=1, sort_keys=True) + '\n')


def read_rows(plan) -> list:
    """tasks.tsv as dicts, in file order. A missing or unreadable table is no rows."""
    rows = []
    try:
        lines = (Path(plan) / 'tasks.tsv').read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return rows
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = (line.split('\t') + [''] * 7)[:7]
        rows.append(dict(id=cells[0], deps=[d for d in cells[1].split(',') if d and d != '-'], difficulty=cells[2],
                         files=[f for f in cells[3].split(',') if f], dwarf=cells[4], qa=cells[5], title=cells[6]))
    return rows


def row_of(plan, task: str):
    for row in read_rows(plan):
        if row['id'] == task:
            return row
    return None


def status_of(plan, task: str) -> str:
    base = Path(plan) / 'tasks' / task
    if (base / 'merged').exists():
        return 'MERGED'
    return _first_line(base / 'status') or 'PENDING'


def run_id_of(plan) -> str:
    plan = Path(plan)
    text = _first_line(plan / 'run_id')
    if text:
        return text
    name = plan.resolve().name
    return name[len('forge-'):] if name.startswith('forge-') else name


# --- the tree ------------------------------------------------------------------------------

def sub_dir(plan, task: str) -> Path:
    return Path(plan) / 'tasks' / task / SUB


def is_split(plan, task: str) -> bool:
    """The task has a sub-plan."""
    return (sub_dir(plan, task) / 'tasks.tsv').is_file()


def is_open(plan, task: str) -> bool:
    """The task has a sub-plan whose work is not on the task's branch yet: the next entry runs the
    sub-plan, not an implementer."""
    return is_split(plan, task) and not (Path(plan) / 'tasks' / task / MERGED).exists()


def parent_link(plan):
    """(parent plan, task) of a sub-plan, None for a root plan. The place says it: a sub-plan is
    <parent>/tasks/<task>/sub and holds parent.json, so a moved plan directory keeps its tree."""
    path = Path(plan).resolve()
    if path.name != SUB or path.parent.parent.name != 'tasks' or not (path / PARENT).is_file():
        return None
    return path.parent.parent.parent, path.parent.name


def ancestors(plan) -> list:
    """[(plan, task), ...] from the nearest parent up to the root plan. Empty for a root plan."""
    found = []
    link = parent_link(plan)
    while link is not None and len(found) < 64:
        found.append(link)
        link = parent_link(link[0])
    return found


def root_of(plan) -> Path:
    chain = ancestors(plan)
    return chain[-1][0] if chain else Path(plan).resolve()


def level_of(plan) -> int:
    """How many splits lie above this plan's tasks: 0 for the tasks of a root plan."""
    return len(ancestors(plan))


def node_path(plan, task: str) -> str:
    """The node's id in the tree: the task ids from the root plan down, joined by '/'."""
    return '/'.join([t for _plan, t in reversed(ancestors(plan))] + [task])


def count_subnodes(root) -> int:
    """Subtasks in the whole tree under a root plan: every row of every sub-plan."""
    total = 0
    stack = [Path(root)]
    while stack:
        plan = stack.pop()
        for row in read_rows(plan):
            sub = sub_dir(plan, row['id'])
            if (sub / 'tasks.tsv').is_file():
                total += len(read_rows(sub))
                stack.append(sub)
    return total


# --- limits --------------------------------------------------------------------------------

def limits(plan) -> dict:
    """The limits of the tree this plan is in: the root plan's, each default where it has none."""
    stored = load(root_of(plan)).get('limits')
    out = dict(DEFAULT_LIMITS)
    if isinstance(stored, dict):
        for key in DEFAULT_LIMITS:
            value = stored.get(key)
            low, high = LIMIT_RANGE[key]
            if type(value) is int and low <= value <= high:
                out[key] = value
    return out


def is_frozen(plan) -> bool:
    return load(root_of(plan)).get('frozen') is True


def set_limits(plan, **values) -> dict:
    """Set limits on a root plan before its first run. A value equal to the current one is always
    accepted, so a re-plan with the same flags is not an error."""
    plan = Path(plan)
    changes = {k: v for k, v in values.items() if v is not None}
    if parent_link(plan) is not None:
        if changes:
            raise Invalid('the limits belong to the root plan %s; a sub-plan has none of its own' % root_of(plan))
        return limits(plan)
    current = limits(plan)
    for key, value in changes.items():
        low, high = LIMIT_RANGE[key]
        if type(value) is not int or not low <= value <= high:
            raise Invalid('--split-%s takes a whole number from %d to %d' % (key, low, high))
    wanted = {k: v for k, v in changes.items() if current[k] != v}
    data = load(plan)
    if wanted and data.get('frozen') is True:
        raise Invalid('the decomposition limits were frozen when this plan first ran (%s); they do not change '
                      'mid-run. A new plan can have other limits' % describe(current))
    current.update(wanted)
    if wanted or not isinstance(data.get('limits'), dict):
        data['limits'] = current
        save(plan, data)
    return current


def freeze(plan) -> dict:
    """The first run freezes the limits. Idempotent; a sub-plan freezes nothing (its root did)."""
    plan = Path(plan)
    if parent_link(plan) is not None:
        return limits(plan)
    data = load(plan)
    current = limits(plan)
    if data.get('frozen') is not True or data.get('limits') != current:
        data['limits'] = current
        data['frozen'] = True
        data.setdefault('frozen_at', time.time())
        save(plan, data)
    return current


def describe(values: dict) -> str:
    if values['depth'] == 0 or values['nodes'] == 0:
        return 'no task is split into subtasks (depth %d, %d subtasks in all)' % (values['depth'], values['nodes'])
    return ('depth %d, at most %d subtasks per split, %d subtasks in the whole run'
            % (values['depth'], values['children'], values['nodes']))


# --- decisions -----------------------------------------------------------------------------

def record(plan, task: str, decision: str, reason: str, by: str = 'default', **extra) -> dict:
    """Write the node's decision. A `default` record (nobody decided) never replaces a decision
    somebody made, and never replaces `split` or `bounded`."""
    if decision not in DECISIONS:
        raise Invalid('decision %r is not atomic, split or bounded' % decision)
    if by not in DECIDERS:
        raise Invalid('by %r is not planner, user, auto or default' % by)
    plan = Path(plan)
    data = load(plan)
    old = data['nodes'].get(task)
    if old and by == 'default' and (old.get('by') != 'default' or old.get('decision') != 'atomic'):
        return old
    row = row_of(plan, task) or {}
    link = parent_link(plan)
    node = dict(id=node_path(plan, task), task=task,
                parent=node_path(link[0], link[1]) if link else None,
                goal=row.get('title', ''), files=row.get('files', []), deps=row.get('deps', []),
                difficulty=row.get('difficulty', ''), decision=decision,
                reason=' '.join(str(reason).split())[:2000], by=by, at=time.time(),
                limits_used=dict(depth=level_of(plan), nodes=count_subnodes(root_of(plan)),
                                 children=extra.pop('children', None), limit=extra.pop('limit', None)))
    node.update(extra)
    data['nodes'][task] = node
    save(plan, data)
    return node


def node_of(plan, task: str, data: dict = None) -> dict:
    """The node as recorded, or what an undecided task is: atomic, by default."""
    data = load(plan) if data is None else data
    node = data['nodes'].get(task)
    if node:
        return node
    return dict(decision='split' if is_split(plan, task) else 'atomic', by='default',
                reason='no split was proposed' if not is_split(plan, task) else '')


# --- validation ----------------------------------------------------------------------------

def _norm(path: str) -> str:
    path = path.replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    return path.rstrip('/') or '.'


def inside(path: str, owned: list) -> bool:
    """Whether a declared path lies inside what the parent declares: the same path, a path under a
    declared directory, or anything when the parent declares `.` (the rule check_drift uses)."""
    path = _norm(path)
    for entry in owned:
        entry = _norm(entry)
        if entry == '.' or path == entry or path.startswith(entry + '/'):
            return True
    return False


def _check_children(children, parent_files: list) -> list:
    if not isinstance(children, list) or not all(isinstance(c, dict) for c in children):
        raise Invalid('the subtasks are not a list of task objects')
    if len(children) < 2:
        raise Atomic('a split needs at least two subtasks')
    seen = set()
    for child in children:
        cid = child.get('id')
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise Invalid('subtask id %r does not match ^[a-z0-9][a-z0-9-]{0,39}$' % (cid,))
        if cid in seen:
            raise Invalid('subtask %s: duplicate id' % cid)
        seen.add(cid)
        if child.get('difficulty') not in DIFFICULTIES:
            raise Invalid('subtask %s: difficulty %r is not low, medium or high' % (cid, child.get('difficulty')))
        files = child.get('files')
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            raise Invalid('subtask %s: files is not a non-empty list of paths' % cid)
        for path in files:
            if ',' in path or re.search(r'[\x00-\x1f\x7f]', path):
                raise Invalid('subtask %s: file %r holds a comma, tab or control character' % (cid, path))
            if path.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', path) or '..' in re.split(r'[\\/]', path):
                raise Invalid('subtask %s: file %r is absolute or has a .. component' % (cid, path))
            if not inside(path, parent_files):
                raise Invalid('subtask %s: file %s is outside the files its parent declares (%s); a subtask '
                              'owns paths inside its parent\'s' % (cid, path, ', '.join(parent_files) or 'none'))
        title = child.get('title')
        if not isinstance(title, str) or not title.strip() or re.search(r'[\x00-\x1f\x7f]', title):
            raise Invalid('subtask %s: title is not one non-empty line' % cid)
        prompt = child.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            raise Invalid('subtask %s: prompt is empty' % cid)
        deps = child.get('deps', [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            raise Invalid('subtask %s: deps is not a list of distinct ids' % cid)
    graph = {c['id']: list(c.get('deps', [])) for c in children}
    for cid, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                raise Invalid('subtask %s: unknown dep %r (a subtask depends only on its siblings)' % (cid, dep))
    state = {}

    def visit(node, trail):
        state[node] = 1
        for nxt in graph[node]:
            if state.get(nxt) == 1:
                raise Invalid('dependency cycle among the subtasks: ' + ' -> '.join(trail + [nxt]))
            if nxt not in state:
                visit(nxt, trail + [nxt])
        state[node] = 2

    for cid in graph:
        if cid not in state:
            visit(cid, [cid])
    return children


def check_split(plan, task: str, children) -> None:
    """Raise Invalid (the split is wrong), Atomic (fewer than two subtasks) or Bounded (a limit
    refuses it). Returns when the split may be made. Validity is judged before the limits, so a
    wrong proposal is never recorded as bounded."""
    plan = Path(plan)
    row = row_of(plan, task)
    if row is None:
        raise Invalid('no task %r in %s' % (task, plan))
    if row['dwarf'] == 'script':
        raise Invalid('task %s is a script task: its command is one step, and it is not split' % task)
    state = status_of(plan, task)
    if state in NOT_SPLITTABLE:
        raise Invalid('task %s is %s: only a task that has not passed and is not running can be split' % (task, state))
    if is_split(plan, task):
        raise Invalid('task %s is already split into subtasks (%s); its subtasks are kept, never made twice'
                      % (task, sub_dir(plan, task)))
    _check_children(children, row['files'])
    run_id = '%s--%s' % (run_id_of(plan), task)
    if len(run_id) > MAX_RUN_ID:
        raise Invalid('the run id of the sub-plan would be longer than %d characters' % MAX_RUN_ID)
    lim = limits(plan)
    level = level_of(plan)
    if level >= lim['depth']:
        raise Bounded('depth', 'task %s is at depth %d and the limit is %d: it is not split further'
                      % (node_path(plan, task), level, lim['depth']))
    if len(children) > lim['children']:
        raise Bounded('children', '%d subtasks were proposed for %s and the limit is %d per split'
                      % (len(children), node_path(plan, task), lim['children']))
    used = count_subnodes(root_of(plan))
    if used + len(children) > lim['nodes']:
        raise Bounded('nodes', 'the run already has %d subtasks, %d more were proposed for %s, and the limit is %d'
                      % (used, len(children), node_path(plan, task), lim['nodes']))


# --- the sub-plan --------------------------------------------------------------------------

def _pools(path: Path) -> dict:
    data = _read_json(path)
    out = {}
    if isinstance(data, dict):
        for key in ('any', 'low', 'medium', 'high'):
            specs = data.get(key)
            if isinstance(specs, list):
                specs = [s for s in specs if isinstance(s, str) and s and s != 'script']
                if specs:
                    out[key] = specs
    return out


def _route(children: list, pools: dict, fallback: str) -> list:
    """One spec per child from the plan's tier pools, each pool taken in turn, as `plan` does:
    the child's tier, then the plain pool, then what the parent task has."""
    turn = {}
    picked = []
    for child in children:
        tier = child['difficulty'] if child['difficulty'] in pools else 'any'
        if tier in pools:
            index = turn.get(tier, 0)
            turn[tier] = index + 1
            picked.append(pools[tier][index % len(pools[tier])])
        else:
            picked.append(fallback)
    return picked


def create_sub(plan, task: str, children: list, reason: str, by: str = 'user', start: str = 'keep',
               context: str = '') -> Path:
    """Make the task's sub-plan from validated children and record the split. The sub-plan appears
    whole (written beside its place and renamed), so an interrupted split leaves none."""
    if start not in STARTS:
        raise Invalid('a split starts with keep or fresh, not %r' % start)
    plan = Path(plan)
    check_split(plan, task, children)
    row = row_of(plan, task)
    tdir = plan / 'tasks' / task
    final = tdir / SUB
    work = tdir / ('.%s.tmp-%d' % (SUB, os.getpid()))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        dwarfs = _route(children, _pools(forge_legacy.pools_path(plan)), row['dwarf'])
        reviewers = _route(children, _pools(plan / 'qa-routing.json'), row['qa'])
        declared, routed = [TSV_HEADER], [TSV_HEADER]
        try:
            parent_prompt = (tdir / 'prompt.md').read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            parent_prompt = row['title'] + '\n'
        for child, dwarf, qa in zip(children, dwarfs, reviewers):
            cells = [child['id'], ','.join(child.get('deps', [])) or '-', child['difficulty'],
                     ','.join(child['files']), '-', '-', ' '.join(child['title'].split())]
            declared.append('\t'.join(cells))
            routed.append('\t'.join(cells[:4] + [dwarf or '-', qa or '-'] + cells[6:]))
            cdir = work / 'tasks' / child['id']
            cdir.mkdir(parents=True)
            text = child['prompt'].rstrip('\n') + '\n\n## The task this is a part of\n\n'
            text += ('This is one of %d subtasks of the task "%s". The other subtasks own the rest of it; '
                     'do only the part above.\n\n' % (len(children), row['title']))
            text += parent_prompt if parent_prompt.endswith('\n') else parent_prompt + '\n'
            if context.strip():
                text += '\n' + context.rstrip('\n') + '\n'
            (cdir / 'prompt.md').write_text(text, encoding='utf-8')
            approach = child.get('approach')
            if isinstance(approach, str) and approach.strip():
                (cdir / 'approach.md').write_text(approach, encoding='utf-8')
            if child.get('risk') in DIFFICULTIES:
                (cdir / 'risk').write_text(child['risk'] + '\n', encoding='utf-8')
            nested = child.get('split')
            if isinstance(nested, dict) and isinstance(nested.get('tasks'), list):
                (cdir / PROPOSAL).write_text(json.dumps(nested), encoding='utf-8')
        (work / 'tasks.declared.tsv').write_text('\n'.join(declared) + '\n', encoding='utf-8')
        (work / 'tasks.tsv').write_text('\n'.join(routed) + '\n', encoding='utf-8')
        (work / 'run_id').write_text('%s--%s\n' % (run_id_of(plan), task), encoding='utf-8')
        (work / 'request.md').write_text(parent_prompt, encoding='utf-8')
        (work / 'goal.txt').write_text(row['title'] + '\n', encoding='utf-8')
        (work / 'orchestrator').write_text('split\n', encoding='utf-8')
        for name in ('repo',) + OPTION_FILES:
            source = plan / name
            if source.is_file():
                shutil.copy2(source, work / name)
        (work / PARENT).write_text(json.dumps(dict(
            task=task, run_id=run_id_of(plan), start=start, by=by, at=time.time(),
            reason=' '.join(str(reason).split())[:2000]), indent=1) + '\n', encoding='utf-8')
        if not (work / 'repo').is_file():
            raise Invalid('%s has no repo file: plan it first (forge-parallel.sh plan)' % plan)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        (tdir / MERGED).unlink()
    except OSError:
        pass
    record(plan, task, 'split', reason, by, children=len(children), start=start,
           subtasks=[c['id'] for c in children])
    return final


def split(plan, task: str, proposal: dict, reason: str = '', by: str = 'user', start: str = 'keep',
          context: str = '') -> Path:
    """create_sub, with the outcome that is not a split recorded: a proposal of one task (or none)
    is `atomic` with its reason, and a split a limit refuses is `bounded`. Both raise again, so
    the caller says what happened; Invalid records nothing."""
    children = proposal.get('tasks') if isinstance(proposal, dict) else None
    why = reason or (proposal.get('reason') if isinstance(proposal, dict) else '') or ''
    try:
        made = create_sub(plan, task, children, why, by=by, start=start, context=context)
        settle(made)                # its subtasks are nodes too, and one may carry a proposal of its own
        return made
    except Atomic:
        # The reason to keep a task whole is the one who answered gave, when there is one.
        said = proposal.get('reason') if isinstance(proposal, dict) else ''
        record(plan, task, 'atomic', (said if isinstance(said, str) and said.strip() else why)
               or 'kept as one task: no split was proposed', by,
               children=len(children) if isinstance(children, list) else 0)
        raise
    except Bounded as limit:
        record(plan, task, 'bounded', '%s (%s)' % (limit, why) if why else str(limit), by,
               children=len(children), limit=limit.limit)
        raise


def settle(plan, by: str = 'default') -> list:
    """After `plan` routed the table: turn every stored proposal (tasks/<id>/split.proposal.json,
    written when a planner proposed subtasks) into a sub-plan, through every level, and record
    every task nobody decided about as atomic. Returns one line per thing done, for the table.
    A proposal is applied once: it is removed whatever its outcome."""
    lines = []
    stack = [Path(plan)]
    while stack:
        current = stack.pop()
        for row in read_rows(current):
            task = row['id']
            stored = current / 'tasks' / task / PROPOSAL
            if stored.is_file():
                proposal = _read_json(stored)
                try:
                    stored.unlink()
                except OSError:
                    pass
                who = by if by != 'default' else 'planner'
                try:
                    sub = split(current, task, proposal if isinstance(proposal, dict) else {}, by=who)
                    lines.append('%s: split into %d subtasks' % (node_path(current, task), len(read_rows(sub))))
                except Atomic:
                    lines.append('%s: kept whole' % node_path(current, task))
                except Bounded as limit:
                    lines.append('%s: not split, at a limit (%s)' % (node_path(current, task), limit))
                except Invalid as error:
                    lines.append('%s: the proposed split was refused (%s)' % (node_path(current, task), error))
            if is_split(current, task):
                stack.append(sub_dir(current, task))
            else:
                old = load(current)['nodes'].get(task)
                if old and old.get('decision') == 'bounded':
                    continue                     # a limit said no: that stays the record
                note = _first_line(current / 'tasks' / task / REASON)
                record(current, task, 'atomic', note or 'kept as one task: no split was proposed',
                       'planner' if note else 'default')
    return lines


# --- reading the tree ----------------------------------------------------------------------

def tree(plan, _parent: str = None, _level: int = None) -> list:
    """Every node at and below this plan, in tree order (a task, then its subtasks). Each is a
    dict: id (the path from this plan's root), task, parent, level, plan (the directory the task
    is a row of), run, title, status, difficulty, files, deps, model, qa, decision, reason, by,
    children (how many subtasks it has)."""
    plan = Path(plan)
    out = []
    data = load(plan)
    level = level_of(plan) if _level is None else _level
    run = run_id_of(plan)
    for row in read_rows(plan):
        task = row['id']
        node = node_of(plan, task, data)
        path = node_path(plan, task)
        sub = sub_dir(plan, task)
        kids = read_rows(sub) if is_split(plan, task) else []
        out.append(dict(id=path, task=task, parent=_parent if _parent is not None else (
                            node_path(*parent_link(plan)) if parent_link(plan) else None),
                        level=level, plan=str(plan), run=run, title=row['title'], status=status_of(plan, task),
                        difficulty=row['difficulty'], files=row['files'], deps=row['deps'], model=row['dwarf'],
                        qa=row['qa'], decision=node.get('decision', 'atomic'), reason=node.get('reason', ''),
                        by=node.get('by', 'default'), children=len(kids),
                        merged=(plan / 'tasks' / task / MERGED).exists() if kids else None))
        if kids:
            out.extend(tree(sub, path, level + 1))
    return out


def waiting(plan, task: str) -> list:
    """One line per subtask of the task that has not merged, with its status and where it is."""
    sub = sub_dir(plan, task)
    lines = []
    for row in read_rows(sub):
        state = status_of(sub, row['id'])
        if state != 'MERGED':
            lines.append('%s %s' % (row['id'], state))
    return lines


def subs(plan) -> list:
    """Every sub-plan directory under the plan, a parent before its children."""
    found = []
    stack = [Path(plan)]
    while stack:
        current = stack.pop()
        inner = [sub_dir(current, row['id']) for row in read_rows(current) if is_split(current, row['id'])]
        found.extend(inner)
        stack.extend(reversed(inner))
    return found


def render(plan, split_only: bool = False) -> str:
    lines = []
    for node in tree(plan):
        if split_only and node['level'] == 0 and node['decision'] == 'atomic':
            continue
        mark = {'atomic': '', 'split': ' [split into %d]' % node['children'], 'bounded': ' [at a limit]'}[node['decision']]
        lines.append('%s%-*s %-14s %s%s' % ('  ' * node['level'], max(14, 28 - 2 * node['level']), node['task'],
                                            node['status'], node['title'], mark))
        if node['decision'] != 'atomic' and node['reason']:
            lines.append('%s  %s: %s' % ('  ' * node['level'], node['decision'], node['reason']))
    return '\n'.join(lines)


# --- goal -> plans: the stages after a plan ------------------------------------------------
# A planner that judges a goal larger than one plan plans its first stage and lists the others
# (later.json). Each later stage becomes a plan of its own, made by `next` once the plan before it
# has merged and planned from the code as it then is, on that plan's integration branch (--after).

def chain_of(plan) -> dict:
    data = _read_json(Path(plan) / CHAIN)
    return data if isinstance(data, dict) else {}


def _stages(raw) -> list:
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get('title'), str) and isinstance(item.get('goal'), str) \
                    and item['title'].strip() and item['goal'].strip():
                out.append(dict(title=' '.join(item['title'].split()), goal=item['goal']))
    return out


def later(plan) -> list:
    """The stages that follow this plan, in order: what its own planner listed, else what is left of
    the chain it was made from."""
    own = _read_json(Path(plan) / LATER)
    if isinstance(own, dict) and _stages(own.get('later')):
        return _stages(own.get('later'))
    return _stages(chain_of(plan).get('remaining'))


def unmerged(plan) -> list:
    return [row['id'] for row in read_rows(plan) if status_of(plan, row['id']) != 'MERGED']


def next_stage(plan, new=None) -> Path:
    """Make the directory of the plan for the next stage: request.md (the stage's goal, then the
    larger goal for context), goal.txt and chain.json. Nothing is planned or dispatched here."""
    plan = Path(plan).resolve()
    if parent_link(plan) is not None:
        raise Invalid('%s is a sub-plan: the stages of a goal follow its root plan' % plan)
    stages = later(plan)
    if not stages:
        raise Invalid('%s has no later stage: its planner proposed none' % plan)
    if not read_rows(plan):
        raise Invalid('%s has no tasks.tsv' % plan)
    left = unmerged(plan)
    if left:
        raise Invalid('the next stage is planned when this plan has merged, and %d task(s) have not: %s'
                      % (len(left), ', '.join(left[:8])))
    chain = chain_of(plan)
    first = Path(chain['first']) if isinstance(chain.get('first'), str) and chain['first'] else plan
    stage = (chain['stage'] if type(chain.get('stage')) is int else 1) + 1
    target = Path(new).resolve() if new else first.with_name('%s-%d' % (first.name, stage))
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Invalid('%s exists and is not empty: name another directory for the next plan' % target)
    try:
        whole = (first / 'request.md').read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        whole = ''
    head = stages[0]
    request = '# %s\n\n%s\n' % (head['title'], head['goal'].strip())
    request += ('\n## Where this stands\n\nThis is stage %d of a larger goal. The stages before it are done and '
                'merged: plan only this stage, from the code as it is now.\n' % stage)
    if stages[1:]:
        request += 'Stages that follow, not to be planned now: %s.\n' % '; '.join(s['title'] for s in stages[1:])
    if whole.strip():
        request += '\n## The larger goal, for context\n\n' + whole
    target.mkdir(parents=True, exist_ok=True)
    (target / 'request.md').write_text(request, encoding='utf-8')
    (target / 'goal.txt').write_text(head['title'][:200] + '\n', encoding='utf-8')
    _write_atomic(target / CHAIN, json.dumps(dict(first=str(first), stage=stage, previous=str(plan),
                                                  title=head['title'], remaining=stages[1:]), indent=1) + '\n')
    return target


# --- one concurrency bound for the tree ----------------------------------------------------
# A run and the sub-plan runs under it share the root run's --max-parallel: each dispatch that
# works (an implementer or a review) holds one slot, a lock on a file of <root>/tree.slots. A split
# task waiting for its subtasks holds none. Locks end with their process, so a killed run leaves no
# slot taken. Any error here means "no bound from this file": a run is never stopped by its slots.

class Slots:
    def __init__(self, root):
        self.dir = Path(root) / SLOTS
        self.broken = False

    def take(self, cap: int):
        """A held slot (an open file to keep and later close), None when all `cap` are taken, or
        False when slots cannot be used here (the caller dispatches without one)."""
        if self.broken:
            return False
        try:
            import forge_flock
            self.dir.mkdir(parents=True, exist_ok=True)
            for index in range(max(1, cap)):
                handle = open(self.dir / str(index), 'a')
                try:
                    forge_flock.flock(handle, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
                    return handle
                except OSError:
                    handle.close()
            return None
        except Exception:
            self.broken = True
            return False

    @staticmethod
    def give(handle) -> None:
        if handle:
            try:
                handle.close()
            except OSError:
                pass


def _pid_alive(pid: int) -> bool:
    try:
        import forge_proc
        return forge_proc.pid_alive(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def hold_slot(root, cap: int, pid: int, ready, wait: float = 0.2) -> int:
    """Take one slot of the tree, write `ready`, and hold the slot until process `pid` is gone.
    Used by a split task for its own review: its scheduler gave its slot back while the subtasks ran."""
    slots = Slots(root)
    handle = None
    while _pid_alive(pid):
        handle = slots.take(cap)
        if handle is not None:      # a slot, or False: slots cannot be used, so nothing to wait for
            break
        time.sleep(wait)
    try:
        Path(ready).write_text('held\n' if handle else 'unbounded\n')
    except OSError:
        pass
    while _pid_alive(pid):
        time.sleep(wait)
    Slots.give(handle)
    return 0


# --- command line --------------------------------------------------------------------------

def _proposal(path: str) -> dict:
    data = _read_json(Path(path))
    if not isinstance(data, dict):
        raise Invalid('%s is not a JSON object with the subtasks' % path)
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge_decompose.py', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('limits')
    p.add_argument('plan')
    for key in DEFAULT_LIMITS:
        p.add_argument('--' + key, type=int)
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('freeze')
    p.add_argument('plan')
    p = sub.add_parser('split')
    p.add_argument('plan')
    p.add_argument('task')
    p.add_argument('--proposal', required=True)
    p.add_argument('--reason', default='')
    p.add_argument('--by', choices=DECIDERS, default='user')
    p.add_argument('--start', choices=STARTS, default='keep')
    p.add_argument('--context-file')
    p.add_argument('--check', action='store_true', help='judge the split and write nothing')
    p = sub.add_parser('record')
    p.add_argument('plan')
    p.add_argument('task')
    p.add_argument('--decision', choices=DECISIONS, required=True)
    p.add_argument('--reason', default='')
    p.add_argument('--by', choices=DECIDERS, default='user')
    p = sub.add_parser('settle')
    p.add_argument('plan')
    p.add_argument('--by', choices=DECIDERS, default='default')
    p = sub.add_parser('tree')
    p.add_argument('plan')
    p.add_argument('--json', action='store_true')
    p.add_argument('--split-only', action='store_true', help='only the tasks that were split or bounded, with their subtasks')
    for name in ('state', 'waiting'):
        p = sub.add_parser(name)
        p.add_argument('plan')
        p.add_argument('task')
    for name in ('root', 'subs'):
        p = sub.add_parser(name)
        p.add_argument('plan')
    p = sub.add_parser('later')
    p.add_argument('plan')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('next')
    p.add_argument('plan')
    p.add_argument('--dir')
    p = sub.add_parser('slot')
    p.add_argument('--root', required=True)
    p.add_argument('--cap', type=int, required=True)
    p.add_argument('--while-pid', type=int, required=True)
    p.add_argument('--ready', required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'limits':
            values = set_limits(args.plan, depth=args.depth, children=args.children, nodes=args.nodes)
            if args.json:
                print(json.dumps(dict(values, frozen=is_frozen(args.plan), used=count_subnodes(root_of(args.plan)))))
            else:
                print('decomposition limits: %s%s' % (describe(values), ' (frozen)' if is_frozen(args.plan) else ''))
            return 0
        if args.cmd == 'freeze':
            freeze(args.plan)
            return 0
        if args.cmd == 'split':
            proposal = _proposal(args.proposal)
            if args.check:
                check_split(args.plan, args.task, proposal.get('tasks'))
                return 0
            context = ''
            if args.context_file:
                try:
                    context = Path(args.context_file).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    context = ''
            print(split(args.plan, args.task, proposal, args.reason, args.by, args.start, context))
            return 0
        if args.cmd == 'record':
            record(args.plan, args.task, args.decision, args.reason, args.by)
            return 0
        if args.cmd == 'settle':
            for line in settle(args.plan, args.by):
                print(line)
            return 0
        if args.cmd == 'tree':
            text = json.dumps(tree(args.plan), indent=1) if args.json else render(args.plan, args.split_only)
            if text:
                print(text)
            return 0
        if args.cmd == 'state':
            print('none' if not is_split(args.plan, args.task) else 'open' if is_open(args.plan, args.task) else 'merged')
            return 0
        if args.cmd == 'waiting':
            for line in waiting(args.plan, args.task):
                print(line)
            return 0
        if args.cmd == 'root':
            print(root_of(args.plan))
            return 0
        if args.cmd == 'subs':
            for path in subs(args.plan):
                print(path)
            return 0
        if args.cmd == 'later':
            stages = later(args.plan)
            if args.json:
                print(json.dumps(stages))
            else:
                for index, stage in enumerate(stages, 1):
                    print('%d. %s' % (index, stage['title']))
            return 0
        if args.cmd == 'next':
            print(next_stage(args.plan, args.dir))
            return 0
        if args.cmd == 'slot':
            return hold_slot(args.root, args.cap, args.while_pid, args.ready)
    except Atomic as error:
        print('kept whole: %s' % error, file=sys.stderr)
        return 5
    except Bounded as error:
        print('bounded: %s' % error, file=sys.stderr)
        return 4
    except Invalid as error:
        print('refused: %s' % error, file=sys.stderr)
        return 3
    return 2


if __name__ == '__main__':
    sys.exit(main())
