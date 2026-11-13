#!/usr/bin/env python3
"""Attribute a group QA reply to the member whose diff each finding belongs to.

One group review covers several tasks. Each member has its own file-disjoint diff, and the
reviewer saw the combined diff. `attribute` sends each finding back to the members that must
change, so only those members are retried. It parses with forge-contract.py: the same finding
blocks and the same severity and status the gate would decide. The facts brace
`{reach=… origin=… evidence=…}` is not a tasks tag; both may be on one bullet.

    attribute(qa_last_text, members, threshold) -> dict

`members` is an ordered list of `(task_id, changed_paths)`. `changed_paths` are the
repo-relative files that member's own diff changed. A path matches after a leading `./` is
removed, and no other normalisation: `src/./a.py` does not match `src/a.py`.

The dict is `{"findings", "implicated", "ambiguous", "blocking"}`. Each finding is
`{n, severity, status, file, line, tasks, blocking, ambiguous}`:

- `n` is 1-based in the parser's order. `severity` is `P0`–`P3` or null. `status` is the
  parser's CONFIRMED, PLAUSIBLE or UNSTATED.
- `file` and `line` are the block's first `path:line`, or, when the block has no `path:line`,
  the first path. `line` is null then. A dotted word earlier in the prose (`os.path.join`,
  `json.loads`, `node.js`) is not that path when a `path:line` follows it.
- `tasks` is `{tasks=…}` when the bullet has one: comma or space separated, trimmed,
  duplicates dropped, member ids in member order and any other id after them in the order
  written. Otherwise it is every member whose changed paths contain `file`, in member order.
  A present tag with no id (`{tasks=}`, `{tasks= , }`) names nobody and does not fall back
  to the file.
- `ambiguous` is true when there is no `{tasks=…}` and either there is no file or no member
  changed that file, when `{tasks=…}` names an id that is not a member, and when `{tasks=…}`
  names no id.
- `blocking` is the gate's per-finding decision at `threshold` (`none` is P3; unset is P2).
  PLAUSIBLE never blocks.

`implicated` is the union, in member order, of `tasks` over blocking findings that are not
ambiguous. `ambiguous` at the top is the `n` of findings that are both blocking and ambiguous.
`blocking` is true when any finding blocks. Non-blocking findings stay in the list, attributed
the same way, so a tolerated one can still be written to that member's known issues.

    python3 scripts/forge_groups/attribute.py --qa-last F --members members.tsv --threshold P2

`members.tsv` has one `task<TAB>path` row per changed path. Blank lines and `#` comments are
ignored. The command prints the JSON and exits 0, or 2 on bad input.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

# The contract's facts brace is `{reach=… origin=… evidence=…}`. A tasks tag is its own brace,
# so a facts group is not read as one even when the two sit on the same line.
_TASKS_TAG = re.compile(r'\{[ \t]*tasks[ \t]*=[ \t]*([^{}]*)\}', re.IGNORECASE)
_ID_SPLIT = re.compile(r'[\s,]+')
# A segment ends on a word character, so a sentence's final dot stays outside the path.
_SEG = r'[\w+-]+(?:\.[\w+-]+)*'
_PATH_LINE = re.compile(r'(?<![\w./])((?:\./)?' + _SEG + r'(?:/' + _SEG + r')*):(\d+)')
_PATH = re.compile(
    r'(?<![\w./])((?:\./)?' + _SEG + r'(?:/' + _SEG + r')+'
    r'|(?:\./)?' + _SEG + r'\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w:/])')
_NOT_PATH = {'e.g', 'i.e'}

_contract_mod = None


def _contract():
    """forge-contract.py, loaded by path: its file name is not importable."""
    global _contract_mod
    if _contract_mod is None:
        path = Path(__file__).resolve().parents[1] / 'forge-contract.py'
        spec = importlib.util.spec_from_file_location('forge_contract', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _contract_mod = module
    return _contract_mod


def norm_path(path: str) -> str:
    """Repo-relative path with a leading `./` removed. `..` and an inner `./` stay."""
    path = str(path).strip().strip('`"\'')
    while path.startswith('./'):
        path = path[2:]
    return path


def _clean_file(path: str) -> str:
    return norm_path(path).rstrip('.')


def _usable(path: str) -> bool:
    return bool(path) and path.lower() not in _NOT_PATH


def _locate(text: str):
    """(file, line) of the first path:line, else the first path, else (None, None).

    A path:line anywhere in the block wins over a bare path, including one that starts
    earlier. `Calls os.path.join badly in src/a.py:3` is `src/a.py` line 3, and `src/a.py:10`
    keeps its line rather than the bare `src/a.py` at the same character."""
    for match in _PATH_LINE.finditer(text):
        path = _clean_file(match.group(1))
        if _usable(path):
            return path, int(match.group(2))
    for match in _PATH.finditer(text):
        path = _clean_file(match.group(1))
        if _usable(path):
            return path, None
    return None, None


def _split_ids(raw: str) -> list:
    return [part for part in _ID_SPLIT.split(raw.strip()) if part]


def _in_member_order(ids: list, member_ids: list) -> tuple:
    """(ids, ambiguous). Member ids lead, in member order; other ids keep written order."""
    seen = []
    for task_id in ids:
        if task_id not in seen:
            seen.append(task_id)
    known = set(member_ids)
    ordered = [task_id for task_id in member_ids if task_id in seen]
    ordered.extend(task_id for task_id in seen if task_id not in known)
    return ordered, any(task_id not in known for task_id in seen)


def _index(members):
    """(member ids in order, {normalised path: [task ids]})."""
    if isinstance(members, (str, bytes)):
        raise ValueError('members must be a list of (task, paths) pairs')
    order = []
    by_path = {}
    seen = set()
    for item in members:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('members must be a list of (task, paths) pairs')
        task, changed = item
        task = str(task).strip()
        if not task:
            raise ValueError('member task id is empty')
        if task not in seen:
            seen.add(task)
            order.append(task)
        if isinstance(changed, (str, bytes)):
            changed = [changed]
        try:
            paths = list(changed)
        except TypeError as error:
            raise ValueError('changed paths for %s must be a list of paths' % task) from error
        for path in paths:
            key = norm_path(path)
            if not key:
                raise ValueError('member %s has an empty path' % task)
            owners = by_path.setdefault(key, [])
            if task not in owners:
                owners.append(task)
    return order, by_path


def attribute(qa_last_text, members, threshold=None) -> dict:
    """Findings of one group QA reply, each attributed to the members that must change."""
    contract = _contract()
    # The gate's threshold rule, checked even when the reply has no findings.
    contract.finding_blocks(contract.Finding('CONFIRMED', None, False, 1, ''), threshold)
    order, by_path = _index(members)
    rows = []
    blocked = False
    wanted = set()
    ambiguous_ns = []
    for number, finding in enumerate(contract.parse_findings(qa_last_text or ''), 1):
        tags = list(_TASKS_TAG.finditer(finding.text))
        file, line = _locate(_TASKS_TAG.sub(' ', finding.text))
        if not tags:
            if file is None:
                tasks, ambiguous = [], True
            else:
                tasks = list(by_path.get(file, ()))
                ambiguous = not tasks
        else:
            # The last {tasks=…} wins. A tag with no id names nobody: do not fall back to the
            # file, or a blocking finding would skip the member it must escalate as unattributable.
            tasks, ambiguous = _in_member_order(_split_ids(tags[-1].group(1)), order)
            if not tasks:
                ambiguous = True
        blocking = contract.finding_blocks(finding, threshold)
        rows.append({
            'n': number,
            'severity': None if finding.severity is None else 'P%d' % finding.severity,
            'status': finding.status,
            'file': file,
            'line': line,
            'tasks': tasks,
            'blocking': blocking,
            'ambiguous': ambiguous,
        })
        if not blocking:
            continue
        blocked = True
        if ambiguous:
            ambiguous_ns.append(number)
        else:
            wanted.update(tasks)
    return {
        'findings': rows,
        'implicated': [task for task in order if task in wanted],
        'ambiguous': ambiguous_ns,
        'blocking': blocked,
    }


def load_members(text: str) -> list:
    """`(task, [paths])` in first-seen order, from `task<TAB>path` rows."""
    order = []
    paths = {}
    for number, line in enumerate((text or '').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.count('\t') != 1:
            raise ValueError('members line %d: needs task<TAB>path' % number)
        task, path = (part.strip() for part in line.split('\t'))
        if not task or not path:
            raise ValueError('members line %d: needs task<TAB>path' % number)
        if task not in paths:
            order.append(task)
            paths[task] = []
        paths[task].append(path)
    return [(task, paths[task]) for task in order]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='attribute.py',
        description='Attribute a group QA reply to the member tasks its findings name.')
    parser.add_argument('--qa-last', required=True, metavar='FILE', help='the QA reply')
    parser.add_argument('--members', required=True, metavar='FILE',
                        help='rows of task<TAB>path, one changed path per row')
    parser.add_argument('--threshold', default=None,
                        help='least severe level that still blocks (default P2; none means P3)')
    args = parser.parse_args(argv)
    try:
        qa = Path(args.qa_last).read_text(encoding='utf-8', errors='replace')
        members = load_members(Path(args.members).read_text(encoding='utf-8', errors='replace'))
        result = attribute(qa, members, args.threshold)
    except (OSError, ValueError) as error:
        sys.stderr.write('attribute: %s\n' % error)
        return 2
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
