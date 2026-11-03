#!/usr/bin/env python3
"""The planner contract: the prompt a planner gets, the strict parse of its reply, the pre-plan
gate and the one-task plan. Standard library only; no shell wiring.

  forge-planner.py prompt   --plan P --goal-file F [--level low|medium|high]
      Writes P/planner.prompt (the role, the request verbatim, the decomposition rules, the
      reply format) and prints its path.

  forge-planner.py parse    --plan P --reply FILE
      Turns the planner's reply into P/tasks.tsv, P/tasks/<id>/prompt.md and, when it is not
      empty, P/tasks/<id>/approach.md. It never guesses or repairs: a reply that breaks one
      rule writes nothing but P/planner.refused (the first reason and the reply's path), prints
      both and exits 4. P/request.md must hold the user's request (exit 2 when it is missing);
      it is attached to every task prompt so no requirement is lost.

  forge-planner.py gate     --plan P --goal-file F
      Prints `one-task` when the request is small (at most MAX_CHARS characters and
      MAX_REQUIREMENTS requirements), else `plan`, and records the signals in P/plan-gate.json.

  forge-planner.py one-task --plan P --goal-file F [--difficulty low|medium|high]
      Writes a one-row plan without a planner: id `task`, files `.`, the request as its prompt.

Every subcommand refuses (exit 2) a plan dir that already has tasks.tsv, so nothing is
overwritten. Exit codes: 0 done, 2 usage or input problem, 4 reply refused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_jev.coverage import split_requirements  # noqa: E402  (read-only use)

# The gate: a request at or under both limits skips the planner. On #86's 16 benchmark plans
# size did not predict the plan's shape (element-web: 3262 characters, 15 requirements, one
# task; flipt: 3276, 22, two parallel tasks), so the limits only let small goals through.
MAX_CHARS = 1500
MAX_REQUIREMENTS = 6

MAX_TASKS = 20
TASK_KEYS = ('id', 'deps', 'difficulty', 'files', 'title', 'approach', 'prompt')
DIFFICULTIES = ('low', 'medium', 'high')
LEVELS = DIFFICULTIES
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
ACCEPTANCE_RE = re.compile(r'^## Acceptance criteria[ \t]*:?[ \t]*$', re.MULTILINE)
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
  {"id": "parse-config", "deps": [], "difficulty": "medium",
   "files": ["src/config.py", "tests/test_config.py"],
   "title": "Parse the new config section",
   "approach": "Add parse_section() beside load(); keep load()'s signature.",
   "prompt": "Add the `limits` section to the config parser in src/config.py ...\\n\\n## Acceptance criteria\\n\\n- `limits` is read into Config.limits, with the defaults the request names.\\n- python3 -m unittest tests.test_config passes."}
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
        return Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as err:
        die(f'cannot read {what} {path}: {err}')


def write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


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
    ]
    numbered = '\n'.join(f'{n}. {rule}' for n, rule in enumerate(rules, 1))
    return f"""\
# Role

{ROLE}

# The request

The request follows verbatim between the markers. It is the whole requirement.

<<<REQUEST
{request.rstrip(chr(10))}
REQUEST>>>

# Decomposition rules

{numbered}

# Reply format

Reply with exactly one fenced block whose opening line is ```forge-plan and whose body is one
JSON object with exactly the key `tasks`: a list of 1 to {MAX_TASKS} objects, each with exactly
these keys and no others:

- `id`: lowercase slug matching `^[a-z0-9][a-z0-9-]{{0,39}}$`, unique in the plan;
- `deps`: list of ids of tasks that must land first (`[]` for none), no cycles;
- `difficulty`: `low`, `medium` or `high`;
- `files`: non-empty list of repo-relative paths the task creates or edits (no absolute path, no
  `..`, no tab, comma or newline in a path);
- `title`: one line without a tab;
- `approach`: a few lines on the intended shape (a string; `""` for none);
- `prompt`: the task's full instruction, ending with a `## Acceptance criteria` section.

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
    lines = reply.splitlines()
    openings = [i for i, line in enumerate(lines) if OPEN_RE.match(line)]
    if not openings:
        raise Refusal('no forge-plan block in the reply')
    if len(openings) > 1:
        raise Refusal(f'{len(openings)} forge-plan blocks in the reply; exactly one is allowed')
    for j in range(openings[0] + 1, len(lines)):
        if CLOSE_RE.match(lines[j]):
            return '\n'.join(lines[openings[0] + 1:j])
    raise Refusal('the forge-plan block is never closed')


def no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    for k in keys:
        if keys.count(k) > 1:
            raise Refusal(f'duplicate key {k!r} in the JSON')
    return dict(pairs)


def check_keys(obj, expected, where: str):
    if not isinstance(obj, dict):
        raise Refusal(f'{where} is not a JSON object')
    missing = [k for k in expected if k not in obj]
    extra = [k for k in obj if k not in expected]
    if missing or extra:
        parts = []
        if missing:
            parts.append('missing ' + ', '.join(missing))
        if extra:
            parts.append('unexpected ' + ', '.join(extra))
        raise Refusal(f'{where} must have exactly the keys {", ".join(expected)} ({"; ".join(parts)})')


def check_path(path, where: str):
    if not isinstance(path, str) or not path:
        raise Refusal(f'{where}: a file is not a non-empty string: {path!r}')
    if any(c in path for c in '\t,\n\r'):
        raise Refusal(f'{where}: file {path!r} holds a tab, comma or newline')
    if path.startswith('/') or path.startswith('\\') or re.match(r'^[A-Za-z]:', path):
        raise Refusal(f'{where}: file {path!r} is an absolute path')
    if '..' in re.split(r'[\\/]', path):
        raise Refusal(f'{where}: file {path!r} has a .. component')


def check_task(task, index: int, seen: set) -> dict:
    where = f'task {index + 1}'
    check_keys(task, TASK_KEYS, where)
    tid = task['id']
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        raise Refusal(f'{where}: id {tid!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$')
    where = f'task {tid}'
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
    if not isinstance(title, str) or not title.strip() or any(c in title for c in '\t\n\r'):
        raise Refusal(f'{where}: title is not one non-empty line without a tab')
    if not isinstance(task['approach'], str):
        raise Refusal(f'{where}: approach is not a string')
    prompt = task['prompt']
    if not isinstance(prompt, str) or not prompt.strip():
        raise Refusal(f'{where}: prompt is empty')
    if not ACCEPTANCE_RE.search(prompt):
        raise Refusal(f'{where}: prompt has no "## Acceptance criteria" heading')
    return task


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


def parse_reply(reply: str) -> list:
    body = extract_block(reply)
    try:
        data = json.loads(body, object_pairs_hook=no_duplicate_keys)
    except json.JSONDecodeError as err:
        raise Refusal(f'the forge-plan body is not valid JSON: {err}')
    check_keys(data, ('tasks',), 'the forge-plan body')
    tasks = data['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        count = f' ({len(tasks)})' if isinstance(tasks, list) else ''
        raise Refusal(f'tasks must be a list of 1 to {MAX_TASKS} objects{count}')
    seen: set = set()
    checked = [check_task(t, i, seen) for i, t in enumerate(tasks)]
    check_graph(checked)
    return checked


def cmd_parse(args) -> int:
    plan = plan_dir(args)
    request_path = plan / 'request.md'
    if not request_path.is_file():
        die(f'{request_path} is missing; it holds the user\'s request')
    request = read_text(request_path, 'request')
    reply = read_text(args.reply, 'reply')
    try:
        tasks = parse_reply(reply)
    except Refusal as err:
        reason = str(err)
        write_atomic(plan / 'planner.refused', f'{reason}\nreply: {args.reply}\n')
        print(f'planner reply refused: {reason}')
        print(f'reply: {args.reply}')
        return 4
    for t in tasks:
        folder = plan / 'tasks' / t['id']
        text = t['prompt'].rstrip('\n') + f'\n\n{WHOLE_REQUEST}\n\n' + request
        write_atomic(folder / 'prompt.md', text)
        if t['approach'].strip():
            write_atomic(folder / 'approach.md', t['approach'])
    rows = [TSV_HEADER] + ['\t'.join([
        t['id'], ','.join(t['deps']) or '-', t['difficulty'], ','.join(t['files']), '-', '-', t['title'],
    ]) for t in tasks]
    write_atomic(plan / 'tasks.tsv', '\n'.join(rows) + '\n')
    (plan / 'planner.refused').unlink(missing_ok=True)
    return 0


# --- gate ----------------------------------------------------------------------------------

def cmd_gate(args) -> int:
    plan = plan_dir(args)
    request = read_text(args.goal_file, 'goal file')
    chars = len(request.rstrip('\r\n'))
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
            title = re.sub(r'^#+\s*', '', line).replace('\t', ' ').strip()
            return title[:80] or 'task'
    return ''


def cmd_one_task(args) -> int:
    plan = plan_dir(args)
    request = read_text(args.goal_file, 'goal file')
    title = request_title(request)
    if not title:
        die(f'goal file {args.goal_file} is empty')
    prompt = request.rstrip('\n') + f'\n\n## Acceptance criteria\n\n{ONE_TASK_CRITERION}\n'
    write_atomic(plan / 'tasks' / 'task' / 'prompt.md', prompt)
    row = '\t'.join(['task', '-', args.difficulty, '.', '-', '-', title])
    write_atomic(plan / 'tasks.tsv', TSV_HEADER + '\n' + row + '\n')
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
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
