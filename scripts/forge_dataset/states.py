"""State assemblers for the dataset export's routing and retry slices.

`siblings`, `assemble` and `retry` build the state of those slices from events the
export has already read. `task_size` is the size block on that state. This only caps
and arranges what callers pass in. Pure: no file reads.

The caps are the export's (`forge dataset export`): goal and prompt are not cut.

A task's size is the strongest regime signal in forge's run history: the median prompt of
the newest runs is twice the older ones' and their first-pass rate is half. A model reading
prose and a 40-entry sibling list can only estimate that, so the state spells it out.
"""
from __future__ import annotations

FILE_EXCERPT_CHARS = 600
MAX_EXCERPT_FILES = 6
MEMORY_CHARS = 4000
MAX_SIBLINGS = 40
SIBLING_TITLE_CHARS = 120
REVIEW_CHARS = 12000


def siblings(tasks, task_id: str) -> list[dict]:
    """The rest of the plan, in plan order: [{id, title, files, deps}] without `task_id`."""
    out = []
    for row in tasks:
        if row.get('id') == task_id:
            continue
        out.append(dict(id=row.get('id'), title=str(row.get('title', ''))[:SIBLING_TITLE_CHARS],
                        files=row.get('files', ''), deps=row.get('deps', '')))
        if len(out) >= MAX_SIBLINGS:
            break
    return out


def assemble(*, goal: str, task_id: str, title: str, files: str, deps: str, prompt: str, approach: str,
             plan_tasks: list, file_excerpts: dict, memory: str, dwarf: dict, reviewer: dict, size: dict) -> dict:
    """The first-pass state: the task as planned, the plan around it, who runs and reviews it."""
    # Values pass through as given: coercing a missing title to '' would change the body,
    # and with it the case id, of every exported case that had none.
    return dict(goal=goal, task=dict(id=task_id, title=title, files=files, deps=deps),
                prompt=prompt, approach=approach, plan_tasks=plan_tasks,
                file_excerpts=file_excerpts, memory=(memory or '')[:MEMORY_CHARS],
                dwarf=dwarf, reviewer=reviewer, size=size)


def retry(state: dict, *, attempt: int, previous_review: str) -> dict:
    """The retry-pass state: the first-pass state plus which try this is and what it must fix."""
    review = previous_review or ''
    return dict(state, attempt=attempt, previous_review=review[:REVIEW_CHARS],
                previous_review_truncated=len(review) > REVIEW_CHARS)


def _count(value) -> int:
    if isinstance(value, (list, tuple)):
        return len([v for v in value if v])
    return len([w for w in str(value or '').replace(',', ' ').split() if w and w != '-'])


def task_size(*, prompt: str, declared_files, plan_tasks: int, deps, decompose_level: str | None) -> dict:
    return dict(prompt_chars=len(prompt or ''), declared_files=_count(declared_files),
                plan_tasks=int(plan_tasks), deps=_count(deps),
                decompose_level=decompose_level if decompose_level in ('low', 'medium', 'high') else None)
