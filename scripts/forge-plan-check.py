#!/usr/bin/env python3
"""Advisory checks on a plan's tasks, plus the plan's shape and chain collapse.

  forge-plan-check.py size --plan P
      Names the tasks that are large by the measure that predicts a failed first attempt, the
      tasks whose prompt.md has no acceptance criteria, and the tasks whose acceptance criteria
      ask for the whole suite (each attempt would run it; the run's verification already does,
      after each merge). Prints nothing when every task is fine. Exit status is always 0.

  forge-plan-check.py gates PLAN
      Prints the decision-model gate block (what drift, forward, prompt and coverage did to this
      plan) from PLAN/plan-gates.json. Prints nothing when no gate recorded anything. The
      slow-effort notes under the table are not this file's: it builds the planner's send-back
      prompt, which must not read the dataset (`forge-dataset.py slow-notes PLAN` prints them).

  forge-plan-check.py sendback PLAN
      Writes PLAN/plan.sendback.md from the items the gates marked in this `plan` call and prints
      it, exiting 4. Prints nothing and exits 0 when nothing was marked.

sendback and gates print nothing and exit 0 if forge_jev.plan_gates cannot be imported.

  forge-plan-check.py shape PLAN [--write]
      Prints {"shape", "tasks", "waves", "widest"}. shape is one (a single task), chain (two or
      more tasks and every wave has one) or branches (some wave has two or more). The waves are
      forge-parallel.sh compute_waves: deps in earlier waves, no file overlap within a wave.
      --write also saves PLAN/plan-shape.json. A cycle or a dep with no row exits 2.

  forge-plan-check.py collapse PLAN
      Collapses a chain into one task named combined. The original table is kept as
      tasks.precollapse.tsv and each tasks/<id>/ moves to tasks.precollapse/<id>/. A one or
      branches plan is not changed. A plan that already has tasks.precollapse.tsv or
      collapsed.json exits 2.

Why these two: in forge's run history (269 first attempts, measured 2026-10-17) the share of
first attempts a reviewer passed falls steadily with prompt length (62% under 3K characters,
51% at 5-7K, 33% at 7-10K, 23% at 14K and over) and with declared files (68% at 2, 29% at 9
or more). Size is free to measure, and a smaller task with stated acceptance criteria is the
intervention the numbers point at, rather than a model's guess at difficulty.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_dataset.states import task_size  # noqa: E402

LARGE_CHARS = 7000
LARGE_FILES = 9
# (low, high, first-pass share, n) from the run dataset on 2026-10-17; see the module docstring.
CHAR_BINS = ((7000, 10000, 33, 49), (10000, 14000, 28, 29), (14000, None, 23, 44))
FILE_BIN = (9, 29, 107)
HEADING = re.compile(r'^(#{1,6})\s+acceptance criteria\s*:?\s*$', re.IGNORECASE)
ANY_HEADING = re.compile(r'^(#{1,6})\s')
FENCE = re.compile(r'^\s*(```|~~~)')
# A whole-suite run named as a command: forge's own check.sh, bare discovery, or an ecosystem's
# default test target with no selection. A command that names tests (`cargo test foo`) is fine.
WHOLE_SUITE = re.compile(
    r'(?:(?:ba)?sh\s+|\./)?[\w./-]*check\.sh'
    r'|python3?\s+-m\s+(?:unittest(?:\s+discover(?:\s+-s\s+\S+)?)?|pytest)|pytest|tox'
    r'|(?:npm|pnpm|yarn)(?:\s+run)?\s+test|make\s+(?:test|check)|cargo\s+test|go\s+test\s+\./\.\.\.')
COMMAND_SPAN = re.compile(r'`([^`\n]+)`')


def has_acceptance_criteria(text: str) -> bool:
    """A complete `Acceptance criteria` heading outside fenced code, with content under it."""
    fenced = False
    level = None
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            if level is not None:
                return True        # a fenced block under the heading is content
            continue
        if fenced:
            continue
        if level is not None:
            heading = ANY_HEADING.match(line)
            if heading and len(heading.group(1)) <= level:
                level = None       # the section ended empty; another may follow
            elif line.strip():
                return True
        match = HEADING.match(line)
        if match:
            level = len(match.group(1))
    return False


def acceptance_lines(text: str) -> list:
    """The lines of the `Acceptance criteria` sections, fenced code included."""
    out, fenced, level = [], False, None
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            heading = ANY_HEADING.match(line)
            if level is not None and heading and len(heading.group(1)) <= level:
                level = None
            match = HEADING.match(line)
            if match:
                level = len(match.group(1))
                continue
        if level is not None:
            out.append(line)
    return out


def verify_commands(plan: Path) -> set:
    """Each command of the plan's --verify and the repository's .forge/verify, whitespace collapsed."""
    sources = [plan / 'verify_cmd']
    try:
        sources.append(Path((plan / 'repo').read_text().strip()) / '.forge' / 'verify')
    except OSError:
        pass
    out = set()
    for source in sources:
        try:
            text = source.read_text(errors='replace')
        except OSError:
            continue
        for part in re.split(r'&&|\|\||;|\n', text):
            part = ' '.join(part.split())
            if len(part) >= 6:
                out.add(part)
    return out


def whole_suite_asked(prompt: str, configured: set):
    """The first whole-suite command the acceptance criteria name, or None."""
    for line in acceptance_lines(prompt):
        spans = COMMAND_SPAN.findall(line) or [line]
        for span in spans:
            command = ' '.join(span.split())
            if WHOLE_SUITE.fullmatch(command) or command in configured:
                return command
    return None


def rows(plan: Path) -> list:
    out = []
    try:
        lines = (plan / 'tasks.tsv').read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        cells = line.split('\t')
        if line.startswith('#') or len(cells) < 7:
            continue
        out.append(dict(id=cells[0], deps=cells[1], files=cells[3]))
    return out


def size_notes(plan: Path) -> list:
    notes = []
    configured = verify_commands(plan)
    for row in rows(plan):
        try:
            prompt = (plan / 'tasks' / row['id'] / 'prompt.md').read_text(errors='replace')
        except OSError:
            continue
        size = task_size(prompt=prompt, declared_files=row['files'], plan_tasks=1, deps=row['deps'],
                         decompose_level=None)
        chars, files = size['prompt_chars'], size['declared_files']
        if chars >= LARGE_CHARS:
            low, high, share, n = next(b for b in CHAR_BINS if b[1] is None or chars < b[1])
            span = '%d-%dK' % (low // 1000, high // 1000) if high else '%dK and over' % (low // 1000)
            notes.append((row['id'], 'large prompt (%s characters): %d%% of first attempts at %s passed review '
                          'in forge\'s history (n=%d), against 56%% at 3-5K; consider splitting it'
                          % ('{:,}'.format(chars), share, span, n)))
        if files >= LARGE_FILES:
            notes.append((row['id'], 'touches %d declared files: %d%% of first attempts at %d or more files passed '
                          '(n=%d), against 68%% at 2; consider splitting it' % (files, FILE_BIN[1], FILE_BIN[0],
                                                                                FILE_BIN[2])))
        if not has_acceptance_criteria(prompt):
            notes.append((row['id'], 'no acceptance criteria: end prompt.md with an "## Acceptance criteria" '
                          'section of checks the reviewer can verify'))
        command = whole_suite_asked(prompt, configured)
        if command:
            notes.append((row['id'], 'acceptance criteria ask for the whole suite (`%s`): every attempt would run '
                          'it; name the task\'s own tests instead, the run\'s verification runs the suite after '
                          'each merge' % command))
    return notes


# --- shape and chain collapse -------------------------------------------------
# Waves follow compute_waves in forge-parallel.sh: walk the table, and a task joins the
# current wave only when every dep is already in an earlier wave and its files do not
# overlap a file already placed in this wave. lists_overlap's rule, including the `-`
# short-circuit and the single `./` / trailing-slash strip, is copied so a chain here is
# a chain there.
TASK_COLS = ('id', 'deps', 'difficulty', 'files', 'dwarf', 'qa', 'title')
DIFFICULTY_RANK = {'low': 0, 'medium': 1, 'high': 2}
SHAPE_KEYS = ('shape', 'tasks', 'waves', 'widest')
COLLAPSE_INTRO = (
    'This task combines the %d parts of a plan that could only run one after another. '
    'Do them in order, in this one task; each part\'s requirements and acceptance criteria still hold.')
NO_CRITERIA = '- (no criteria were written for this part)'
PRECOLLAPSE_TSV = 'tasks.precollapse.tsv'
PRECOLLAPSE_DIR = 'tasks.precollapse'
COLLAPSED_JSON = 'collapsed.json'


class PlanShapeError(Exception):
    """The waves cannot be computed: a dependency cycle, or a dep that has no row."""


def parse_task_rows(text: str) -> list:
    """Task rows of a tasks.tsv. Comment lines and rows with fewer than 7 columns are skipped."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) < 7:
            continue
        out.append({name: cells[index] for index, name in enumerate(TASK_COLS)})
    return out


def _norm_path(path: str) -> str:
    """One leading `./` and one trailing `/`, the same two strips lists_overlap does."""
    if path.startswith('./'):
        path = path[2:]
    if path.endswith('/'):
        path = path[:-1]
    return path


def lists_overlap(files: str, wave_files: str) -> bool:
    """True when a task's comma-list overlaps the space-separated files already in the wave.

    `files` of exactly `-` overlaps nothing. `.` overlaps every path already in the wave.
    A path overlaps another when the two are equal or one is a directory prefix of the
    other (`src` overlaps `src/a.py`, not `src2`).
    """
    if files == '-':
        return False
    for raw in files.replace(',', ' ').split():
        path = _norm_path(raw)
        for other_raw in wave_files.split():
            other = _norm_path(other_raw)
            if (path + '/').startswith(other + '/') or (other + '/').startswith(path + '/'):
                return True
            if path == '.' or other == '.':
                return True
    return False


def waves(rows: list) -> list:
    """Waves of task ids, in the order compute_waves places them. Raises PlanShapeError."""
    order, by_id = [], {}
    for row in rows:
        task_id = row.get('id') or ''
        if not task_id:
            continue
        order.append(task_id)
        by_id.setdefault(task_id, row)
    known = set(by_id)
    missing = []
    for task_id in order:
        deps = by_id[task_id].get('deps')
        if deps is None:
            deps = '-'
        if deps == '-':
            continue
        for dep in deps.replace(',', ' ').split():
            if dep not in known:
                missing.append('%s depends on %s' % (task_id, dep))
    if missing:
        raise PlanShapeError('unknown dep: ' + '; '.join(missing))

    remaining = list(order)
    done = set()
    grouped = []
    while remaining:
        wave_files = ''
        current, nxt = [], []
        for task_id in remaining:
            row = by_id.get(task_id) or {}
            deps = row.get('deps')
            if deps is None:
                deps = '-'
            files = row.get('files') or ''
            ok = True
            if deps != '-':
                for dep in deps.replace(',', ' ').split():
                    if dep not in done:
                        ok = False
                        break
            if ok and lists_overlap(files, wave_files):
                ok = False
            if ok:
                current.append(task_id)
                wave_files = wave_files + ' ' + files.replace(',', ' ')
            else:
                nxt.append(task_id)
        if not current:
            raise PlanShapeError('dependency cycle or unsatisfiable deps among: ' + ' '.join(nxt))
        done.update(current)
        grouped.append(current)
        remaining = nxt
    return grouped


def shape(rows: list) -> dict:
    """`{"shape", "tasks", "waves", "widest"}` for these task rows.

    `one` is a single task. `chain` is two or more tasks whose every wave has one task.
    `branches` is anything with a wave of two or more.
    """
    grouped = waves(rows)
    count = sum(len(group) for group in grouped)
    if count == 0:
        raise PlanShapeError('no tasks')
    widest = max(len(group) for group in grouped)
    if count == 1:
        name = 'one'
    elif widest == 1:
        name = 'chain'
    else:
        name = 'branches'
    return {'shape': name, 'tasks': count, 'waves': len(grouped), 'widest': widest}


def shape_text(info: dict) -> str:
    return json.dumps({key: info[key] for key in SHAPE_KEYS}) + '\n'


def _highest_difficulty(parts: list) -> str:
    known = [part.get('difficulty') or '' for part in parts if (part.get('difficulty') or '') in DIFFICULTY_RANK]
    if known:
        return max(known, key=lambda value: DIFFICULTY_RANK[value])
    values = [part.get('difficulty') or '' for part in parts]
    if values and all(value == values[0] for value in values):
        return values[0]
    return '-'


def _common(parts: list, key: str) -> str:
    values = [part.get(key, '-') for part in parts]
    if values and all(value == values[0] for value in values):
        return values[0]
    return '-'


def _union_files(parts: list) -> str:
    seen, ordered = set(), []
    for part in parts:
        files = part.get('files') or ''
        if files == '-':
            continue
        for token in files.split(','):
            token = token.strip()
            if not token or token == '-' or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
    return ','.join(ordered) if ordered else '-'


# Collapse follows CommonMark for ATX headings and fenced code. The size advisory above keeps
# its own simpler scan; changing that would change which tasks it flags.
_FENCE_LINE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
_CRITERIA_TEXT = re.compile(r'acceptance criteria\s*:?', re.IGNORECASE)


def _strip_closing_hashes(content: str) -> str:
    """Drop a closing `#` run that is the whole content or is preceded by a space or tab."""
    end = len(content)
    while end > 0 and content[end - 1] == '#':
        end -= 1
    if end == len(content):
        return content
    if end == 0 or content[end - 1] in ' \t':
        return content[:end].strip(' \t')
    return content


def _atx_heading(line: str):
    """(indent, opening hashes, content) of a CommonMark ATX heading, else None.

    Up to three leading spaces. A leading tab is a code indent, not a heading. The opening
    `#` run is 1-6 and must be followed by a space or a tab, or the end of the line. A
    closing `#` run, preceded by a space or tab and followed only by spaces or tabs, is not
    part of the content (`## Acceptance criteria ##` is that heading, and so is the same
    line with a tab in place of the space). Leading and trailing spaces and tabs are not
    part of the content.
    """
    if not line or line[0] == '\t' or line.startswith('    '):
        return None
    indent_len = 0
    while indent_len < 3 and indent_len < len(line) and line[indent_len] == ' ':
        indent_len += 1
    marks_end = indent_len
    while marks_end < len(line) and line[marks_end] == '#':
        marks_end += 1
    marks = line[indent_len:marks_end]
    if not marks or len(marks) > 6:
        return None
    if marks_end < len(line) and line[marks_end] not in ' \t':
        return None
    raw = line[marks_end + 1:] if marks_end < len(line) else ''
    return line[:indent_len], marks, _strip_closing_hashes(raw.strip(' \t'))


def _fence_step(line: str, open_fence):
    """(line is fence content or a fence marker, fence state after the line).

    `open_fence` is None or `(marker, length)`. A fence opens on three or more backticks
    or tildes, indented at most three spaces. A backtick fence whose info string contains
    a backtick does not open. It closes only on the same marker, at least as long, with
    nothing after it but spaces or tabs, so a shorter or different marker inside the block
    is content. A tab after the closing marker still closes the fence.
    """
    found = _FENCE_LINE.match(line)
    if open_fence is None:
        if not found:
            return False, None
        marker, rest = found.group(2), found.group(3)
        if marker[0] == '`' and '`' in rest:
            return False, None
        return True, (marker[0], len(marker))
    if found:
        marker, rest = found.group(2), found.group(3)
        if marker[0] == open_fence[0] and len(marker) >= open_fence[1] and rest.strip(' \t') == '':
            return True, None
    return True, open_fence


def split_acceptance(text: str):
    """(body without acceptance-criteria sections, criteria lines or None).

    Criteria is None when no acceptance-criteria heading sits outside a fence. The heading
    is a CommonMark ATX heading (`## Acceptance criteria`, `## Acceptance criteria ##`,
    the same indented up to three spaces, or with a tab where a space would separate the
    hashes from the text). The lines under every such heading are kept
    as written, fence markers included, and the headings themselves are not part of either piece.
    """
    body, criteria = [], []
    found = False
    fence = None
    level = None
    for line in (text or '').splitlines():
        inside, fence = _fence_step(line, fence)
        if inside:
            (criteria if level is not None else body).append(line)
            continue
        heading = _atx_heading(line)
        if level is not None and heading and len(heading[1]) <= level:
            level = None
        if level is None and heading and _CRITERIA_TEXT.fullmatch(heading[2]):
            found = True
            level = len(heading[1])
            continue
        if level is not None:
            criteria.append(line)
        else:
            body.append(line)
    return '\n'.join(body), (criteria if found else None)


def demote_headings(text: str) -> str:
    """One extra `#` on every ATX heading outside a fence. `## X` becomes `### X`.

    An indented heading keeps its indent (`  ## X` becomes `  ### X`). A heading inside
    a fence, including one opened by a longer marker, stays as written.
    """
    if not text:
        return ''
    out, fence = [], None
    for line in text.splitlines():
        inside, fence = _fence_step(line, fence)
        if inside:
            out.append(line)
            continue
        heading = _atx_heading(line)
        if heading:
            indent = heading[0]
            out.append(indent + '#' + line[len(indent):])
            continue
        out.append(line)
    return '\n'.join(out)


def _render_criteria(criteria) -> str:
    if criteria is None:
        return NO_CRITERIA
    rendered = '\n'.join(criteria).strip('\n')
    return rendered


def build_prompt(parts: list, prompts: dict) -> str:
    """The combined prompt: parts in wave order, then one acceptance-criteria section."""
    blocks = [COLLAPSE_INTRO % len(parts), '']
    tail = ['## Acceptance criteria', '']
    for index, part in enumerate(parts, 1):
        raw = (prompts or {}).get(part['id']) or ''
        body, criteria = split_acceptance(raw)
        body = demote_headings(body).strip('\n')
        title = part.get('title') or ''
        blocks.append('## Part %d: %s (%s)' % (index, title, part['id']))
        blocks.append('')
        if body:
            blocks.append(body)
            blocks.append('')
        tail.append('### Part %d: %s' % (index, title))
        tail.append('')
        rendered = _render_criteria(criteria)
        if rendered:
            tail.append(rendered)
            tail.append('')
    return '\n'.join(blocks).rstrip('\n') + '\n\n' + '\n'.join(tail).rstrip('\n') + '\n'


def build_approach(parts: list, approaches: dict):
    """Approaches that exist, in wave order, each under `### Part i`. None when none do."""
    blocks = []
    for index, part in enumerate(parts, 1):
        text = (approaches or {}).get(part['id'])
        if text is None or not text.strip():
            continue
        blocks.append('### Part %d\n\n%s\n' % (index, text.strip()))
    if not blocks:
        return None
    return '\n'.join(blocks)


def _parts_in_wave_order(rows: list) -> list:
    by_id = {}
    for row in rows:
        by_id.setdefault(row.get('id'), row)
    return [by_id[task_id] for group in waves(rows) for task_id in group]


def collapse_rows(rows: list, prompts: dict, approaches: dict):
    """(row, prompt, approach) of a chain merged in wave order.

    The row is the single `combined` task. `approach` is None when no part had one.
    Callers decide whether the plan is a chain; this only merges.
    """
    parts = _parts_in_wave_order(rows)
    if not parts:
        raise PlanShapeError('no tasks')
    title = ('%d parts: %s' % (len(parts), parts[0].get('title') or ''))[:100]
    row = {
        'id': 'combined',
        'deps': '-',
        'difficulty': _highest_difficulty(parts),
        'files': _union_files(parts),
        'dwarf': _common(parts, 'dwarf'),
        'qa': _common(parts, 'qa'),
        'title': title,
    }
    return row, build_prompt(parts, prompts), build_approach(parts, approaches)


def _atomic_write(path: Path, data) -> None:
    """Write via a temp file in the same directory, then rename into place."""
    raw = data.encode('utf-8', 'surrogateescape') if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_task_file(path: Path):
    """The text of a task file, or None when it is absent.

    A file that exists but cannot be read raises PlanShapeError. Treating that as an empty
    prompt would drop the part's requirements and still collapse.
    """
    try:
        if not path.is_file():
            return None
        return path.read_text(errors='replace')
    except OSError as err:
        raise PlanShapeError('cannot read %s' % path) from err


def read_plan(plan: Path):
    """(original tasks.tsv bytes, parsed rows). PlanShapeError when the table cannot be read."""
    path = Path(plan) / 'tasks.tsv'
    try:
        data = path.read_bytes()
    except OSError as err:
        raise PlanShapeError('cannot read %s' % path) from err
    return data, parse_task_rows(data.decode('utf-8', 'surrogateescape'))


def render_tasks(original: bytes, row: dict) -> bytes:
    """The preamble of the old table (comments and blank lines) plus the one combined row."""
    text = original.decode('utf-8', 'surrogateescape')
    preamble = []
    for line in text.splitlines(keepends=True):
        if line.startswith('#') or not line.strip():
            preamble.append(line)
            continue
        break
    body = ''.join(preamble)
    if body and not body.endswith('\n'):
        body += '\n'
    cells = '\t'.join(row[name] for name in TASK_COLS)
    return (body + cells + '\n').encode('utf-8', 'surrogateescape')


def _collapsed_text(ids: list, info: dict) -> str:
    payload = {
        'from': ids,
        'into': 'combined',
        'shape': {key: info[key] for key in SHAPE_KEYS},
    }
    return json.dumps(payload, indent=2) + '\n'


def _rollback(plan: Path, original: bytes, moved: list, made_pre: bool, replaced: bool, made_combined: bool) -> None:
    """Put back what collapse had changed. Leftover part dirs are not deleted."""
    if made_combined:
        combined = plan / 'tasks' / 'combined'
        if combined.exists():
            shutil.rmtree(combined)
    marker = plan / COLLAPSED_JSON
    if marker.exists():
        marker.unlink()
    if replaced:
        _atomic_write(plan / 'tasks.tsv', original)
    pre = plan / PRECOLLAPSE_DIR
    for task_id in moved:
        src = pre / task_id
        dst = plan / 'tasks' / task_id
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
    copy = plan / PRECOLLAPSE_TSV
    if copy.exists():
        copy.unlink()
    if made_pre and pre.exists() and not any(pre.iterdir()):
        pre.rmdir()


RISK_RANK = {'low': 0, 'medium': 1, 'high': 2}


def read_risk(path: Path):
    """A task's recorded risk (#122): the first word of tasks/<id>/risk when it is low, medium or high, else None."""
    try:
        words = (_read_task_file(path) or '').split()
    except PlanShapeError:      # a label nothing routes on never stops a collapse
        return None
    return words[0] if words and words[0] in RISK_RANK else None


def combined_risk(risks: list):
    """The highest risk of the parts when every part was rated, else None: an unrated part could be the riskiest."""
    if not risks or any(risk is None for risk in risks):
        return None
    return max(risks, key=RISK_RANK.get)


def commit_collapse(plan: Path, original: bytes, rows: list, row: dict, prompt: str, approach, info: dict,
                    risk=None) -> None:
    """Move the chain aside and write the combined task. A failure restores the plan."""
    moved, made_pre, replaced, made_combined = [], False, False, False
    try:
        _atomic_write(plan / PRECOLLAPSE_TSV, original)
        pre = plan / PRECOLLAPSE_DIR
        pre.mkdir()
        made_pre = True
        for task in rows:
            src = plan / 'tasks' / task['id']
            if src.exists():
                shutil.move(src, pre / task['id'])
                moved.append(task['id'])
        _atomic_write(plan / 'tasks.tsv', render_tasks(original, row))
        replaced = True
        combined = plan / 'tasks' / 'combined'
        combined.mkdir(parents=True)
        made_combined = True
        _atomic_write(combined / 'prompt.md', prompt)
        if approach is not None:
            _atomic_write(combined / 'approach.md', approach)
        if risk is not None:
            _atomic_write(combined / 'risk', risk + '\n')
        ids = [task['id'] for task in _parts_in_wave_order(rows)]
        _atomic_write(plan / COLLAPSED_JSON, _collapsed_text(ids, info))
    except Exception as err:
        try:
            _rollback(plan, original, moved, made_pre, replaced, made_combined)
        except Exception as restore_err:
            raise PlanShapeError('collapse failed (%s) and the plan could not be restored (%s)'
                                 % (err, restore_err)) from err
        raise


def command_shape(plan: Path, write: bool) -> int:
    try:
        info = shape(read_plan(plan)[1])
    except PlanShapeError as err:
        print('forge-plan-check: %s' % err, file=sys.stderr)
        return 2
    text = shape_text(info)
    if write:
        try:
            _atomic_write(Path(plan) / 'plan-shape.json', text)
        except OSError as err:
            print('forge-plan-check: %s' % err, file=sys.stderr)
            return 2
    sys.stdout.write(text)
    return 0


def command_collapse(plan: Path) -> int:
    plan = Path(plan)
    for name in (PRECOLLAPSE_TSV, COLLAPSED_JSON):
        if (plan / name).exists():
            print('forge-plan-check: %s already exists' % name, file=sys.stderr)
            return 2
    try:
        original, rows = read_plan(plan)
        info = shape(rows)
    except PlanShapeError as err:
        print('forge-plan-check: %s' % err, file=sys.stderr)
        return 2
    if info['shape'] != 'chain':
        print('not collapsed: %s' % info['shape'])
        return 0
    try:
        prompts, approaches = {}, {}
        for row in rows:
            root = plan / 'tasks' / row['id']
            prompts[row['id']] = _read_task_file(root / 'prompt.md') or ''
            approach = _read_task_file(root / 'approach.md')
            if approach is not None:
                approaches[row['id']] = approach
        combined, prompt, approach = collapse_rows(rows, prompts, approaches)
        risk = combined_risk([read_risk(plan / 'tasks' / row['id'] / 'risk') for row in rows])
        commit_collapse(plan, original, rows, combined, prompt, approach, info, risk)
    except PlanShapeError as err:
        print('forge-plan-check: %s' % err, file=sys.stderr)
        return 2
    except Exception as err:
        print('forge-plan-check: collapse failed: %s' % err, file=sys.stderr)
        return 2
    print('collapsed a sequential plan of %d tasks into one (%s keeps the original)'
          % (info['tasks'], PRECOLLAPSE_TSV))
    return 0


def plan_gates():
    try:
        from forge_jev import plan_gates as module
    except Exception:
        return None
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-plan-check.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('size')
    p.add_argument('--plan', required=True)
    for name in ('gates', 'sendback'):
        sub.add_parser(name).add_argument('plan')
    shape_cmd = sub.add_parser('shape')
    shape_cmd.add_argument('plan')
    shape_cmd.add_argument('--write', action='store_true')
    sub.add_parser('collapse').add_argument('plan')
    args = ap.parse_args(argv)
    if args.command == 'shape':
        return command_shape(Path(args.plan), args.write)
    if args.command == 'collapse':
        return command_collapse(Path(args.plan))
    if args.command == 'sendback':
        module = plan_gates()
        if module is None:
            return 0
        try:
            if module.sendback(args.plan):
                sys.stdout.write((Path(args.plan) / module.SENDBACK_FILE).read_text())
                return 4
        except Exception:
            return 0
        return 0
    if args.command == 'gates':
        try:
            module = plan_gates()
            if module is not None:
                for line in module.marks(args.plan):
                    print(line)
        except Exception:
            pass
        return 0
    try:
        notes = size_notes(Path(args.plan))
    except Exception:
        return 0
    if notes:
        print('task size and acceptance criteria (advisory; the table is unchanged):')
        for task, text in notes:
            print('  %-14s %s' % (task, text))
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
