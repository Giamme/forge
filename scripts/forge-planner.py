#!/usr/bin/env python3
"""The planner contract: the prompt a planner gets, the strict parse of its reply, the pre-plan
gate and the one-task plan. Standard library only; no shell wiring.

  forge-planner.py prompt   --plan P --goal-file F [--level low|medium|high]
      Writes P/planner.prompt (the role, the request verbatim, the decomposition rules, the
      reply format) and prints its path.

  forge-planner.py parse    --plan P --reply FILE
      Turns the planner's reply into P/tasks.tsv, P/tasks/<id>/prompt.md and, when it is not
      empty, P/tasks/<id>/approach.md; a task's `risk` (#122) goes to P/tasks/<id>/risk, and a
      missing or unknown one writes nothing. A reply that includes `groups` also writes
      P/groups.tsv (review always `task`, qa `-`). It never guesses or repairs: a reply that breaks
      one rule writes nothing but P/planner.refused (the first reason and the reply's path), prints
      both and exits 4. P/request.md must hold the user's request (exit 2 when it is missing);
      it is attached to every task prompt so no requirement is lost.

  forge-planner.py gate     --plan P --goal-file F
      Prints `one-task` when the request is small (at most MAX_CHARS characters and
      MAX_REQUIREMENTS requirements), else `plan`, and records the signals in P/plan-gate.json.

  forge-planner.py one-task --plan P --goal-file F [--difficulty low|medium|high]
      Writes a one-row plan without a planner: id `task`, files `.`, the request as its prompt.

  forge-planner.py node-prompt --plan P --task ID [--start keep|fresh]
      Writes P/tasks/ID/split.prompt, the question a planner gets about ONE task of an existing
      plan (#153): split it into subtasks inside its own files, or keep it whole and say why.

  forge-planner.py node-parse  --plan P --task ID --reply FILE
      The strict parse of that reply, or of subtasks a person wrote (a forge-plan block, or the
      bare JSON object): P/tasks/ID/split.proposal.json, which forge_decompose.py turns into the
      task's sub-plan. A refusal writes P/tasks/ID/split.refused and exits 4.

Recursive decomposition (#153) in a whole-plan reply: a task may carry `subtasks` (its own small
plan, same keys, nested as deep as the reply likes) and a one-line `reason`. `parse` checks them as
strictly as tasks, and that each subtask's files lie inside its parent's, and keeps them in
P/tasks/<id>/split.proposal.json for `plan` to apply within the run's limits; a `reason` on a task
kept whole goes to P/tasks/<id>/split.reason. A reply may also carry `later`, the stages of a goal
larger than one plan: they go to P/later.json, and forge plans each when the one before has merged.

prompt, parse, gate and one-task refuse (exit 2) a plan dir that already has tasks.tsv, so nothing
is overwritten. Exit codes: 0 done, 2 usage or input problem, 4 reply refused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_decompose  # noqa: E402
import forge_groups  # noqa: E402
from forge_jev.coverage import split_requirements  # noqa: E402  (read-only use)

# The gate: a request at or under both limits skips the planner. On #86's 16 benchmark plans
# size did not predict the plan's shape (element-web: 3262 characters, 15 requirements, one
# task; flipt: 3276, 22, two parallel tasks), so the limits only let small goals through.
MAX_CHARS = 1500
MAX_REQUIREMENTS = 6

MAX_TASKS = 20
TASK_KEYS = ('id', 'deps', 'difficulty', 'files', 'title', 'approach', 'prompt')
# #122: a task's risk, recorded in shadow beside its difficulty. Optional, and the one key that never refuses a
# reply: a missing or unknown value records nothing, because a label nothing routes on must not cost a plan.
# #153: `subtasks` (the task's own small plan) and `reason` (one line: why it is split, or why it is
# kept whole). Both optional; a present one is checked strictly.
OPTIONAL_KEYS = ('risk', 'subtasks', 'reason')
MAX_SUBTASKS = 20
MAX_REASON = 500
# #153: the stages of a goal larger than one plan, in order. Optional on the reply.
LATER_KEYS = ('title', 'goal')
MAX_LATER = 10
MAX_LATER_GOAL = 4000
# #136: groups are optional on the reply. A missing key writes no groups.tsv. A present one is
# checked strictly; the planner never sets review=group (that is plan --group-review, or an edit).
GROUP_KEYS = ('id', 'title', 'goal', 'members')
MAX_GROUP_TEXT = 200
DIFFICULTIES = ('low', 'medium', 'high')
RISKS = ('low', 'medium', 'high')
LEVELS = DIFFICULTIES
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
ACCEPTANCE_RE = re.compile(r'## Acceptance criteria[ \t]*:?[ \t]*')
FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
# Characters that break a one-line TSV field or a path: C0 controls (NUL, tab, newline, CR ...),
# DEL, and the Unicode line separators that str.splitlines() also splits on.
LINE_BREAKS_RE = re.compile('[\x00-\x1f\x7f\x85\u2028\u2029]')
OPEN_RE = re.compile(r'^```forge-plan[ \t]*$')
CLOSE_RE = re.compile(r'^```[ \t]*$')
PATH_RE = re.compile(r'(?<![\w/.-])(?:[\w.-]+/)*[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}(?![\w-])')
TSV_HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle'
WHOLE_REQUEST = '## The whole request'
ONE_TASK_CRITERION = ('- Every requirement of the request above is met, and the tests for the '
                      'files you changed pass.')

LEVEL_GUIDANCE = {
    'low': 'low: split only where the request holds obviously independent pieces; 2-3 coarse tasks.',
    'medium': 'medium: one task per coherent unit (module, feature slice); typically 4-6 tasks.',
    'high': ('high: the finest split in which each task is still independently reviewable and '
             'independently correct; typically 7-12 tasks.'),
}

ROLE = """\
You are the planner of a software change. You are read-only: inspect the repository as much
as you need, and return a plan. Never edit, create or delete a file, and never run anything
that changes the repository. Your reply is a plan; other models carry it out, each task in a
worktree of its own, and a reviewer judges each task against its prompt."""

EXAMPLE = """\
```forge-plan
{"tasks": [
  {"id": "parse-config", "deps": [], "difficulty": "medium", "risk": "low",
   "files": ["src/config.py", "tests/test_config.py"],
   "title": "Parse the new config section",
   "approach": "Add parse_section() beside load(); keep load()'s signature.",
   "prompt": "Add the `limits` section to the config parser in src/config.py ...\\n\\n## Acceptance criteria\\n\\n- `limits` is read into Config.limits, with the defaults the request names.\\n- python3 -m unittest tests.test_config passes."}
],
 "groups": [
  {"id": "config-limits", "title": "Config limits",
   "goal": "The limits section is read into Config with the defaults the request names.",
   "members": ["parse-config"]}
]}
```"""


class Refusal(Exception):
    """A reply that breaks the contract. Carries the first reason."""


def die(message: str, code: int = 2):
    print(f'forge-planner: {message}', file=sys.stderr)
    raise SystemExit(code)


# --- shared plumbing -----------------------------------------------------------------------

def plan_dir(args) -> Path:
    plan = Path(args.plan)
    if (plan / 'tasks.tsv').exists():
        die(f'{plan / "tasks.tsv"} already exists; refusing to overwrite a plan')
    return plan


def read_text(path, what: str) -> str:
    try:
        with open(path, encoding='utf-8', newline='') as handle:   # newline='': CRLF stays CRLF
            return handle.read()
    except (OSError, UnicodeDecodeError) as err:
        die(f'cannot read {what} {path}: {err}')


def write_atomic(path: Path, data):
    """Write str (as UTF-8) or bytes to `path` through a temp file and a rename."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- prompt --------------------------------------------------------------------------------

def build_prompt(request: str, level: str | None) -> str:
    rules = []
    if level:
        rules.append(f'Decomposition level: {LEVEL_GUIDANCE[level]} The counts are guidance, '
                     'not quotas: a request with three natural seams yields three tasks at any level.')
    rules += [
        '**One task is a valid answer**, and the right one when the change is one coherent piece. '
        'Do not split to look thorough.',
        '**A pure chain is worse than one task.** In a chain no two tasks can run at once, and each '
        'link pays its own implementation and review and sees the earlier links\' code only after '
        'they merge. Prefer branches that can run in parallel; when the request allows none, return '
        'one task.',
        'Never split so fine that a task cannot be verified on its own: each task must be '
        'reviewable and correct by itself.',
        '**Never two tasks that edit the same file**, unless a dependency orders them. Either make '
        'it one task or add a dep. `files` is a promise: it is what decides which tasks may run '
        'concurrently, and an understated list is how two workers end up in the same file. List every '
        'file the task will create or edit, tests included.',
        '**A single-file deliverable** (one HTML page, one script, one notebook) is split by module '
        'when it splits at all. Each task owns its own source file (for example `src/physics.js`), '
        'and one final low-difficulty task assembles them into the required file. That keeps file '
        'ownership per file and makes the parallel wave real.',
        'Rate `difficulty` by what the task demands of the model, not by how much code it produces: '
        '`low` is mechanical and local (rename, move, docs, a test for behaviour that already exists); '
        '`medium` is a self-contained implementation against a clear spec; `high` needs design '
        'judgment, touches cross-cutting behaviour, has subtle edge cases or state or concurrency, or '
        'the right approach is not obvious from the request. A five-hundred-line mechanical rename is '
        '`low`; a ten-line concurrency fix is `high`.',
        'Rate `risk` apart from difficulty, by what a mistake that gets through would cost: what breaks, for '
        'whom, and how hard it is to undo. `low`: local and easy to see and revert (docs, a test, an internal '
        'helper). `medium`: wrong behaviour users would notice, undone by a revert. `high`: hard to undo or '
        'reaching everyone: release and CI files, migrations and persisted data, auth and permissions, public '
        'APIs. A one-line change to a release script is low difficulty and high risk. The rating is recorded '
        'only; it changes no model or reviewer.',
        '**Each task\'s `prompt`** carries the whole requirement it owns, written so that someone who '
        'sees only that prompt can do it, and ends with a `## Acceptance criteria` section of checks '
        'a reviewer can verify (a behaviour, a command that passes, a file that exists), not restated '
        'goals. Its tests are the ones for the files it touches. Never ask for the whole test suite '
        '(`tests/check.sh`, `npm test`, `make check` and the like): forge runs that after the merge.',
        'Where tasks share a seam (an interface, a file format, a name), fix it in the `approach` of '
        'every task that touches it, so both sides build the same thing.',
        '**Keep each task** under about 7000 characters of prompt and 9 files. A smaller task with '
        'stated acceptance criteria passes its first review far more often.',
        f'At most {MAX_TASKS} tasks.',
        '**Groups.** Tasks that together deliver one goal, and that the user will think of as one '
        'step, form one group (usually one per wave of the plan\'s dependency order). Each group '
        'has a short title and a goal sentence that says what is true once its tasks are done. A '
        'group may have one task. A task may be left out of every group.',
        '**Subtasks.** A task that is still large after the rules above, and whose own `files` hold '
        'several pieces that can be built and reviewed apart, may carry `subtasks`: its own small plan. '
        'forge runs them on the task\'s branch and then reviews the task on everything they produced. '
        'Each subtask owns paths inside its parent\'s `files` and depends only on its sibling subtasks. '
        'A split is never required and most tasks have none: when the pieces do not live inside one '
        'task\'s files, make them tasks instead. Say in the task\'s `reason`, in one line, why you split '
        'it, or why you kept whole a task that looked splittable. forge applies the run\'s limits (by '
        'default two levels and six subtasks per task); a split beyond them runs as one task.',
        '**A goal larger than one plan.** When the request is too large for one plan, put only its '
        'first coherent stage in `tasks` and list the stages that follow in `later`, in order. forge '
        'plans each later stage only when the one before has merged, from the code as it then is, so '
        'do not write their tasks now.',
    ]
    numbered = '\n'.join(f'{n}. {rule}' for n, rule in enumerate(rules, 1))
    return f"""\
# Role

{ROLE}

# The request

The request follows verbatim between the markers. It is the whole requirement.

<<<REQUEST
{request}{'' if request.endswith(chr(10)) else chr(10)}REQUEST>>>

# Decomposition rules

{numbered}

# Reply format

Reply with exactly one fenced block whose opening line is ```forge-plan and whose body is one
JSON object with the key `tasks` and, optionally, `groups` and `later`. `tasks` is a list of 1 to
{MAX_TASKS} objects, each with these keys and no others:

- `id`: lowercase slug matching `^[a-z0-9][a-z0-9-]{{0,39}}$`, unique in the plan;
- `deps`: list of ids of tasks that must land first (`[]` for none), no cycles;
- `difficulty`: `low`, `medium` or `high`;
- `risk`: `low`, `medium` or `high`, as the rules above define it;
- `files`: non-empty list of repo-relative paths the task creates or edits (no absolute path, no
  `..`, no tab, comma or newline in a path);
- `title`: one line without a tab;
- `approach`: a few lines on the intended shape (a string; `""` for none);
- `prompt`: the task's full instruction, ending with a `## Acceptance criteria` section;
- `subtasks`: optional. A list of 2 to {MAX_SUBTASKS} objects with these same keys, each `files`
  entry inside this task's `files`, `deps` naming sibling subtasks only. Omit it for a task that
  is one piece;
- `reason`: optional. One line, at most {MAX_REASON} characters: why the task is split, or why it
  is kept whole.

`later`, when present, is a list of at most {MAX_LATER} objects with exactly the keys `title` (one
short line) and `goal` (what that stage must reach, in full, since it becomes that plan's request).
Omit the key when the request fits one plan.

`groups`, when present, is a list of objects with exactly these keys and no others. Omit the key
to return no groups:

- `id`: the same slug as a task id, unique in the plan, and not a task id;
- `title`: one short line, at most 200 characters;
- `goal`: one line, at most 200 characters, what is true once the group's tasks are done;
- `members`: ids of tasks from this reply, in order. A group may have one task. A task is in at
  most one group, and a task may be left out of every group.

Put no other `forge-plan` block in the reply. Text outside the block is ignored. Anything that
breaks these rules makes the whole reply refused; nothing is repaired. A valid reply:

{EXAMPLE}
"""


def cmd_prompt(args) -> int:
    plan = plan_dir(args)
    request = read_text(args.goal_file, 'goal file')
    out = plan / 'planner.prompt'
    write_atomic(out, build_prompt(request, args.level))
    print(out)
    return 0


# --- parse ---------------------------------------------------------------------------------

def extract_block(reply: str) -> str:
    # Split on \n only: str.splitlines() would also cut at U+2028 and friends, which are legal
    # inside a JSON string. A trailing \r (CRLF replies) is ignored when matching the fences.
    lines = reply.split('\n')
    openings = [i for i, line in enumerate(lines) if OPEN_RE.match(line.rstrip('\r'))]
    if not openings:
        raise Refusal('no forge-plan block in the reply')
    if len(openings) > 1:
        raise Refusal(f'{len(openings)} forge-plan blocks in the reply; exactly one is allowed')
    for j in range(openings[0] + 1, len(lines)):
        if CLOSE_RE.match(lines[j].rstrip('\r')):
            return '\n'.join(lines[openings[0] + 1:j])
    raise Refusal('the forge-plan block is never closed')


def no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    for k in keys:
        if keys.count(k) > 1:
            raise Refusal(f'duplicate key {k!r} in the JSON')
    return dict(pairs)


def check_keys(obj, expected, where: str, optional=()):
    if not isinstance(obj, dict):
        raise Refusal(f'{where} is not a JSON object')
    missing = [k for k in expected if k not in obj]
    extra = [k for k in obj if k not in expected and k not in optional]
    if missing or extra:
        parts = []
        if missing:
            parts.append('missing ' + ', '.join(missing))
        if extra:
            parts.append('unexpected ' + ', '.join(extra))
        raise Refusal(f'{where} must have exactly the keys {", ".join(expected)} ({"; ".join(parts)})')


def has_acceptance_heading(prompt: str) -> bool:
    """True when a `## Acceptance criteria` heading line stands outside any fenced code block."""
    fence = None
    for line in prompt.split('\n'):
        line = line.rstrip('\r')
        found = FENCE_RE.match(line)
        if fence is None:
            if found and not (found.group(1)[0] == '`' and '`' in found.group(2)):
                fence = (found.group(1)[0], len(found.group(1)))
            elif ACCEPTANCE_RE.fullmatch(line):
                return True
        elif (found and found.group(1)[0] == fence[0] and len(found.group(1)) >= fence[1]
              and not found.group(2).strip()):
            fence = None
    return False


def check_text(value, where: str):
    """Refuse a string that cannot be written as UTF-8 (a lone surrogate from a \\ud800 escape)."""
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise Refusal(f'{where}: holds a character that is not valid Unicode (a lone surrogate)')


def check_path(path, where: str):
    if not isinstance(path, str) or not path:
        raise Refusal(f'{where}: a file is not a non-empty string: {path!r}')
    if ',' in path or LINE_BREAKS_RE.search(path):
        raise Refusal(f'{where}: file {path!r} holds a tab, comma, newline or control character')
    check_text(path, where)
    if path.startswith('/') or path.startswith('\\') or re.match(r'^[A-Za-z]:', path):
        raise Refusal(f'{where}: file {path!r} is an absolute path')
    if '..' in re.split(r'[\\/]', path):
        raise Refusal(f'{where}: file {path!r} has a .. component')


MAX_SPLIT_DEPTH = 6   # forge_decompose.LIMIT_RANGE's deepest; a reply nested past it is not a plan


def check_task(task, index: int, seen: set, depth: int = 0, prefix: str = '') -> dict:
    where = f'{prefix}task {index + 1}' if not prefix else f'{prefix}{index + 1}'
    check_keys(task, TASK_KEYS, where, OPTIONAL_KEYS)
    tid = task['id']
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        raise Refusal(f'{where}: id {tid!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$')
    where = f'{prefix}{tid}' if prefix else f'task {tid}'
    if tid in seen:
        raise Refusal(f'{where}: duplicate id')
    seen.add(tid)
    deps = task['deps']
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise Refusal(f'{where}: deps is not a list of ids')
    if len(set(deps)) != len(deps):
        raise Refusal(f'{where}: deps repeats an id')
    if task['difficulty'] not in DIFFICULTIES:
        raise Refusal(f'{where}: difficulty {task["difficulty"]!r} is not low, medium or high')
    files = task['files']
    if not isinstance(files, list) or not files:
        raise Refusal(f'{where}: files is not a non-empty list')
    for path in files:
        check_path(path, where)
    title = task['title']
    if not isinstance(title, str) or not title.strip() or LINE_BREAKS_RE.search(title):
        raise Refusal(f'{where}: title is not one non-empty line without a tab')
    check_text(title, where)
    if not isinstance(task['approach'], str):
        raise Refusal(f'{where}: approach is not a string')
    check_text(task['approach'], where)
    prompt = task['prompt']
    if not isinstance(prompt, str) or not prompt.strip():
        raise Refusal(f'{where}: prompt is empty')
    check_text(prompt, where)
    if not has_acceptance_heading(prompt):
        raise Refusal(f'{where}: prompt has no "## Acceptance criteria" heading')
    risk = task.get('risk')
    checked = dict(task, risk=risk if isinstance(risk, str) and risk in RISKS else None)
    reason = task.get('reason')
    if reason is not None:
        if not isinstance(reason, str) or LINE_BREAKS_RE.search(reason) or len(reason) > MAX_REASON:
            raise Refusal(f'{where}: reason is not one line of at most {MAX_REASON} characters')
        check_text(reason, where)
    checked['reason'] = (reason or '').strip() or None
    if 'subtasks' in task:
        checked['subtasks'] = check_subtasks(task['subtasks'], tid, files, depth + 1)
    return checked


def check_subtasks(raw, parent: str, parent_files: list, depth: int = 1, low: int = 2) -> list:
    """A task's `subtasks`: `low` to MAX_SUBTASKS tasks under the same rules, ids unique among the
    siblings, deps naming siblings only and forming no cycle, and every file inside the parent's."""
    where = f'task {parent}'
    if depth > MAX_SPLIT_DEPTH:
        raise Refusal(f'{where}: subtasks nest more than {MAX_SPLIT_DEPTH} levels deep')
    if not isinstance(raw, list) or not low <= len(raw) <= MAX_SUBTASKS:
        count = f' ({len(raw)})' if isinstance(raw, list) else ''
        raise Refusal(f'{where}: subtasks must be a list of {low} to {MAX_SUBTASKS} objects{count}; '
                      'omit the key for a task that is one piece')
    seen: set = set()
    checked = []
    for index, item in enumerate(raw):
        child = check_task(item, index, seen, depth, f'{where}: subtask ')
        for path in child['files']:
            if not forge_decompose.inside(path, parent_files):
                raise Refusal(f'{where}: subtask {child["id"]}: file {path!r} is outside its parent\'s files '
                              f'({", ".join(parent_files)})')
        checked.append(child)
    try:
        check_graph(checked)
    except Refusal as err:
        raise Refusal(f'{where}: among its subtasks: {err}')
    return checked


def proposal_of(task: dict) -> dict:
    """What forge_decompose.py reads: {reason, tasks}, each task with `split` where it has subtasks."""
    def plain(item):
        out = {key: item[key] for key in TASK_KEYS}
        out['risk'] = item.get('risk')
        if item.get('subtasks'):
            out['split'] = proposal_of(item)
        return out
    return {'reason': task.get('reason') or '', 'tasks': [plain(item) for item in task['subtasks']]}


def check_later(raw) -> list:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LATER:
        count = f' ({len(raw)})' if isinstance(raw, list) else ''
        raise Refusal(f'later must be a list of 1 to {MAX_LATER} objects{count}; omit the key when the request fits one plan')
    stages = []
    for index, stage in enumerate(raw):
        where = f'later {index + 1}'
        check_keys(stage, LATER_KEYS, where)
        check_group_line(stage['title'], f'{where}: title')
        goal = stage['goal']
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_LATER_GOAL:
            raise Refusal(f'{where}: goal is not a non-empty text of at most {MAX_LATER_GOAL} characters')
        check_text(goal, where)
        stages.append({'title': stage['title'].strip(), 'goal': goal})
    return stages


def check_group_line(value, where: str):
    """A group's title or goal: one non-empty line, at most MAX_GROUP_TEXT characters."""
    if not isinstance(value, str):
        raise Refusal(f'{where} is not a string')
    # Line separators first, so a tab or other control is not reported as a second line.
    # Not a raw string: \n, \x85 and \u2028 have to be the characters, not the escape text.
    if re.search('[\n\r\x85\u2028\u2029]', value):
        raise Refusal(f'{where} is not one line')
    if LINE_BREAKS_RE.search(value):
        raise Refusal(f'{where} holds a tab or a control character')
    if not value.strip():
        raise Refusal(f'{where} is empty')
    if len(value) > MAX_GROUP_TEXT:
        raise Refusal(f'{where} is longer than {MAX_GROUP_TEXT} characters')
    check_text(value, where)


def check_groups(raw, task_ids: set) -> list:
    """The reply's groups, each forced to review=task and qa=-. Order is the reply's."""
    if not isinstance(raw, list):
        raise Refusal('groups is not a list')
    seen: set = set()
    owner: dict = {}
    groups = []
    for index, group in enumerate(raw):
        where = f'group {index + 1}'
        check_keys(group, GROUP_KEYS, where)
        gid = group['id']
        if not isinstance(gid, str) or not ID_RE.fullmatch(gid):
            raise Refusal(f'{where}: id {gid!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$')
        where = f'group {gid}'
        if gid in seen:
            raise Refusal(f'{where}: duplicate id')
        seen.add(gid)
        if gid in task_ids:
            raise Refusal(f'{where}: the id is also a task id')
        check_group_line(group['title'], f'{where}: title')
        check_group_line(group['goal'], f'{where}: goal')
        members = group['members']
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise Refusal(f'{where}: members is not a list of task ids')
        if not members:
            raise Refusal(f'{where}: members is empty')
        for member in members:
            if member not in task_ids:
                raise Refusal(f'{where}: member {member!r} is not a task of the reply')
            if member in owner:
                prior = owner[member]
                if prior == gid:
                    raise Refusal(f'{where}: task {member} is listed twice')
                raise Refusal(f'{where}: task {member} is in two groups ({prior} and {gid})')
            owner[member] = gid
        groups.append(forge_groups.Group(gid, 'task', '-', tuple(members), group['title'], group['goal']))
    return groups


def groups_tsv_bytes(groups) -> bytes:
    """Bytes `forge_groups.write` emits, held until the other outputs are written too.

    A refusal must write none of the plan. write() would touch the disk now, so the bytes are
    captured and joined to the outputs list cmd_parse already flushes in one pass.
    """
    with tempfile.TemporaryDirectory() as tmp:
        forge_groups.write(tmp, groups)
        return (Path(tmp) / 'groups.tsv').read_bytes()


def check_graph(tasks: list):
    ids = {t['id'] for t in tasks}
    for t in tasks:
        for dep in t['deps']:
            if dep not in ids:
                raise Refusal(f'task {t["id"]}: unknown dep {dep!r}')
    deps = {t['id']: t['deps'] for t in tasks}
    state: dict = {}

    def visit(node, path):
        state[node] = 1
        for nxt in deps[node]:
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt]
                raise Refusal('dependency cycle: ' + ' -> '.join(cycle))
            if nxt not in state:
                visit(nxt, path + [nxt])
        state[node] = 2

    for t in tasks:
        if t['id'] not in state:
            visit(t['id'], [t['id']])


MAX_JSON_DEPTH = 64   # a plan is a few levels deep; anything near this is not a plan
MAX_INT_DIGITS = 4300  # Python's own limit from 3.11 (and late 3.9/3.10 patch releases); not in 3.9.13 or Apple's 3.9.6


def bounded_int(text: str) -> int:
    """int() for json.loads, refusing the very long integers that recent Pythons refuse too."""
    if len(text.lstrip('-')) > MAX_INT_DIGITS:
        raise ValueError(f'an integer of more than {MAX_INT_DIGITS} digits')
    return int(text)


def nested_deeper_than(value, limit: int) -> bool:
    """Whether lists and objects nest more than `limit` levels, checked without recursion."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def load_block(body: str):
    """The JSON of a forge-plan block, under the limits every reply is read with."""
    try:
        data = json.loads(body, object_pairs_hook=no_duplicate_keys, parse_int=bounded_int)
    except (ValueError, RecursionError) as err:
        # JSONDecodeError is a ValueError; so is Python's limit on integer digits. Nesting deeper
        # than the interpreter allows raises RecursionError (up to Python 3.11; from 3.12 the
        # parser goes much deeper, hence the explicit limit below).
        raise Refusal(f'the forge-plan body is not valid JSON: {type(err).__name__}: {str(err)[:200]}')
    if nested_deeper_than(data, MAX_JSON_DEPTH):
        raise Refusal(f'the forge-plan body is not valid JSON: nested more than {MAX_JSON_DEPTH} levels deep')
    return data


def parse_plan(reply: str):
    """(tasks, groups, later). groups is None when the reply has no groups key, else a list (maybe
    empty); later is None when it has no later key."""
    data = load_block(extract_block(reply))
    check_keys(data, ('tasks',), 'the forge-plan body', ('groups', 'later'))
    tasks = data['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        count = f' ({len(tasks)})' if isinstance(tasks, list) else ''
        raise Refusal(f'tasks must be a list of 1 to {MAX_TASKS} objects{count}')
    seen: set = set()
    checked = [check_task(t, i, seen) for i, t in enumerate(tasks)]
    check_graph(checked)
    groups = check_groups(data['groups'], seen) if 'groups' in data else None
    later = check_later(data['later']) if 'later' in data else None
    return checked, groups, later


def parse_reply(reply: str):
    """(tasks, groups), as before `later` existed: what the callers that only need the plan read."""
    tasks, groups, _later = parse_plan(reply)
    return tasks, groups


def cmd_parse(args) -> int:
    plan = plan_dir(args)
    request_path = plan / 'request.md'
    if not request_path.is_file():
        die(f'{request_path} is missing; it holds the user\'s request')
    request = read_text(request_path, 'request')
    reply = read_text(args.reply, 'reply')
    try:
        tasks, groups, later = parse_plan(reply)
    except Refusal as err:
        # A key or path may hold a lone surrogate; escape it so the reason can be written and printed.
        reason = str(err).encode('utf-8', 'backslashreplace').decode('utf-8')
        reply_path = str(args.reply).encode('utf-8', 'backslashreplace').decode('utf-8')
        write_atomic(plan / 'planner.refused', f'{reason}\nreply: {reply_path}\n')
        print(f'planner reply refused: {reason}')
        print(f'reply: {reply_path}')
        return 4
    # Everything is encoded before the first file is written, so a text that cannot be written
    # cannot leave half a plan behind.
    rows = [TSV_HEADER] + ['\t'.join([
        t['id'], ','.join(t['deps']) or '-', t['difficulty'], ','.join(t['files']), '-', '-', t['title'],
    ]) for t in tasks]
    outputs = []
    for t in tasks:
        folder = plan / 'tasks' / t['id']
        text = t['prompt'].rstrip('\n') + f'\n\n{WHOLE_REQUEST}\n\n' + request
        outputs.append((folder / 'prompt.md', text.encode('utf-8')))
        outputs.append((folder / 'approach.md', t['approach'].encode('utf-8') if t['approach'].strip() else None))
        outputs.append((folder / 'risk', (t['risk'] + '\n').encode('utf-8') if t['risk'] else None))
        # #153: what the planner decided about splitting this task. `plan` applies a proposal within
        # the run's limits (forge_decompose.py settle) and records the reason of a task kept whole.
        split = json.dumps(proposal_of(t)).encode('utf-8') if t.get('subtasks') else None
        outputs.append((folder / forge_decompose.PROPOSAL, split))
        kept = (t['reason'] + '\n').encode('utf-8') if t.get('reason') and not t.get('subtasks') else None
        outputs.append((folder / forge_decompose.REASON, kept))
    outputs.append((plan / 'tasks.tsv', ('\n'.join(rows) + '\n').encode('utf-8')))
    if groups is not None:
        outputs.append((plan / 'groups.tsv', groups_tsv_bytes(groups)))
    # #153: the stages after this plan, for the table, the run summary and `next`.
    chain = json.dumps({'later': later}, indent=1).encode('utf-8') + b'\n' if later else None
    outputs.append((plan / forge_decompose.LATER, chain))
    for path, data in outputs:
        if data is None:
            path.unlink(missing_ok=True)       # an empty approach must not keep an older one alive
        else:
            write_atomic(path, data)
    (plan / 'planner.refused').unlink(missing_ok=True)
    return 0


# --- gate ----------------------------------------------------------------------------------

def cmd_gate(args) -> int:
    plan = plan_dir(args)
    request = read_text(args.goal_file, 'goal file')
    chars = len(request)
    requirements = len(split_requirements(request))
    paths = len(set(PATH_RE.findall(request)))
    decision = 'one-task' if chars <= MAX_CHARS and requirements <= MAX_REQUIREMENTS else 'plan'
    record = {'decision': decision,
              'signals': {'chars': chars, 'requirements': requirements, 'paths': paths},
              'thresholds': {'chars': MAX_CHARS, 'requirements': MAX_REQUIREMENTS},
              'rule': 'size'}
    write_atomic(plan / 'plan-gate.json', json.dumps(record, indent=2) + '\n')
    print(decision)
    return 0


# --- one-task ------------------------------------------------------------------------------

def request_title(request: str) -> str:
    for line in request.splitlines():
        line = line.strip()
        if line:
            title = LINE_BREAKS_RE.sub(' ', re.sub(r'^#+\s*', '', line)).strip()
            return title[:80] or 'task'
    return ''


def cmd_one_task(args) -> int:
    plan = plan_dir(args)
    request = read_text(args.goal_file, 'goal file')
    title = request_title(request)
    if not title:
        die(f'goal file {args.goal_file} is empty')
    # The request stays a verbatim prefix; only a missing final newline is supplied.
    prompt = (request + ('' if request.endswith('\n') else '\n')
              + f'\n## Acceptance criteria\n\n{ONE_TASK_CRITERION}\n')
    write_atomic(plan / 'tasks' / 'task' / 'prompt.md', prompt)
    row = '\t'.join(['task', '-', args.difficulty, '.', '-', '-', title])
    write_atomic(plan / 'tasks.tsv', TSV_HEADER + '\n' + row + '\n')
    return 0


# --- one task of an existing plan (#153) -------------------------------------------------------

NODE_ROLE = """\
You are the planner of a software change that is already under way. You are read-only: inspect the
repository as much as you need, and return a plan. Never edit, create or delete a file, and never
run anything that changes the repository. One task of the plan is in front of you. Decide whether it
should be split into subtasks, each built in a worktree of its own and judged by a reviewer against
its prompt, or kept as one task."""


def node_task(args):
    plan = Path(args.plan)
    row = forge_decompose.row_of(plan, args.task)
    if row is None:
        die(f'no task {args.task!r} in {plan}')
    return plan, row


def tail_text(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return text if len(text) <= limit else '[...]\n' + text[-limit:]


def build_node_prompt(plan: Path, row: dict, start: str) -> str:
    tdir = plan / 'tasks' / row['id']
    limits = forge_decompose.limits(plan)
    most = min(limits['children'], MAX_SUBTASKS)
    prompt = read_text(tdir / 'prompt.md', 'task prompt')
    review = tail_text(tdir / 'qa.last', 6000).strip()
    stopped = tail_text(tdir / 'decision.md', 1500).strip()
    so_far = ''
    if stopped:
        so_far += f'\n# Why it stopped\n\n{stopped}\n'
    if review:
        so_far += f'\n# The last review of the work so far\n\n{review}\n'
    if start == 'fresh':
        work = ('The work done on this task so far is set aside: the subtasks start from the code as it was '
                'before the task began.')
    else:
        work = ('The work done on this task so far stays on its branch, and the subtasks build on it: plan '
                'what is still missing or wrong, not what is already there and right.')
    rules = [
        '**Keeping it whole is a valid answer**, and the right one when the task is one coherent piece. '
        'Reply then with exactly one task (the task as it should be done) and say why in `reason`. '
        'A split is never required.',
        '**Each subtask owns paths inside this task\'s files** (' + ', '.join(row['files']) + '). A path '
        'outside them refuses the whole reply.',
        '**Never two subtasks that edit the same file**, unless a dependency orders them. `deps` names '
        'sibling subtasks only.',
        'Never split so fine that a subtask cannot be verified on its own: each must be reviewable and '
        'correct by itself. Prefer subtasks that can run in parallel to a pure chain.',
        '**Each subtask\'s `prompt`** carries the whole requirement it owns, written so that someone who '
        'sees only that prompt can do it, and ends with a `## Acceptance criteria` section of checks a '
        'reviewer can verify. The task\'s own prompt is attached to each subtask for context.',
        'Rate `difficulty` by what the subtask demands of the model: `low` mechanical and local, '
        '`medium` a self-contained implementation against a clear spec, `high` design judgment or '
        'subtle edge cases.',
        work,
        f'At most {most} subtasks.',
    ]
    numbered = '\n'.join(f'{n}. {rule}' for n, rule in enumerate(rules, 1))
    return f"""\
# Role

{NODE_ROLE}

# The task

Its id is `{row['id']}`, its difficulty `{row['difficulty']}`, its title "{row['title']}". Its prompt
follows verbatim between the markers.

<<<TASK
{prompt}{'' if prompt.endswith(chr(10)) else chr(10)}TASK>>>
{so_far}
# Rules

{numbered}

# Reply format

Reply with exactly one fenced block whose opening line is ```forge-plan and whose body is one
JSON object with the keys `reason` (one line: why you split the task this way, or why you kept it
whole) and `tasks`, a list of 1 to {most} objects (one means: keep it whole), each with exactly
these keys and no others:

- `id`: lowercase slug matching `^[a-z0-9][a-z0-9-]{{0,39}}$`, unique in the reply;
- `deps`: list of ids of subtasks of this reply that must land first (`[]` for none), no cycles;
- `difficulty`: `low`, `medium` or `high`;
- `files`: non-empty list of repo-relative paths inside the task's files;
- `title`: one line without a tab;
- `approach`: a few lines on the intended shape (a string; `""` for none);
- `prompt`: the subtask's full instruction, ending with a `## Acceptance criteria` section.

Put no other `forge-plan` block in the reply. Text outside the block is ignored. Anything that
breaks these rules makes the whole reply refused; nothing is repaired.
"""


def cmd_node_prompt(args) -> int:
    plan, row = node_task(args)
    out = plan / 'tasks' / row['id'] / 'split.prompt'
    write_atomic(out, build_node_prompt(plan, row, args.start))
    print(out)
    return 0


def parse_node(text: str, row: dict) -> dict:
    """{reason, tasks} from a planner's reply about one task, or from subtasks a person wrote: the
    forge-plan block when the text has one, else the text as the bare JSON object."""
    lines = text.split('\n')
    fenced = any(OPEN_RE.match(line.rstrip('\r')) for line in lines)
    data = load_block(extract_block(text) if fenced else text)
    check_keys(data, ('tasks',), 'the forge-plan body', ('reason',))
    reason = data.get('reason')
    if reason is not None and (not isinstance(reason, str) or LINE_BREAKS_RE.search(reason) or len(reason) > MAX_REASON):
        raise Refusal(f'reason is not one line of at most {MAX_REASON} characters')
    tasks = check_subtasks(data['tasks'], row['id'], row['files'], low=1)
    return {'reason': (reason or '').strip(), 'tasks': proposal_of({'subtasks': tasks})['tasks']}


def cmd_node_parse(args) -> int:
    plan, row = node_task(args)
    tdir = plan / 'tasks' / row['id']
    reply = read_text(args.reply, 'reply')
    try:
        proposal = parse_node(reply, row)
    except Refusal as err:
        reason = str(err).encode('utf-8', 'backslashreplace').decode('utf-8')
        write_atomic(tdir / 'split.refused', f'{reason}\nreply: {args.reply}\n')
        print(f'subtasks refused: {reason}')
        return 4
    out = Path(args.out) if args.out else tdir / forge_decompose.PROPOSAL
    write_atomic(out, json.dumps(proposal))
    (tdir / 'split.refused').unlink(missing_ok=True)
    print(out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-planner.py', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prompt', help='write the planner prompt')
    p.add_argument('--plan', required=True)
    p.add_argument('--goal-file', required=True)
    p.add_argument('--level', choices=LEVELS)
    p.set_defaults(fn=cmd_prompt)
    p = sub.add_parser('parse', help='strictly parse a planner reply into a plan')
    p.add_argument('--plan', required=True)
    p.add_argument('--reply', required=True)
    p.set_defaults(fn=cmd_parse)
    p = sub.add_parser('gate', help='decide whether the request needs a planner')
    p.add_argument('--plan', required=True)
    p.add_argument('--goal-file', required=True)
    p.set_defaults(fn=cmd_gate)
    p = sub.add_parser('one-task', help='write a one-task plan without a planner')
    p.add_argument('--plan', required=True)
    p.add_argument('--goal-file', required=True)
    p.add_argument('--difficulty', choices=DIFFICULTIES, default='medium')
    p.set_defaults(fn=cmd_one_task)
    p = sub.add_parser('node-prompt', help='write the prompt that asks a planner about one task of a plan')
    p.add_argument('--plan', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--start', choices=forge_decompose.STARTS, default='keep')
    p.set_defaults(fn=cmd_node_prompt)
    p = sub.add_parser('node-parse', help='strictly parse the subtasks proposed for one task')
    p.add_argument('--plan', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--reply', required=True)
    p.add_argument('--out', help='where the checked subtasks go (default: tasks/<task>/split.proposal.json)')
    p.set_defaults(fn=cmd_node_parse)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
