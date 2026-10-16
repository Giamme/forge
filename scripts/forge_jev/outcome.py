"""The state forge's outcome rubrics (first_pass, retry_pass) are asked on.

One assembler for both sides of the measurement: the export rebuilds this state from
stored events, live forge builds it from a plan dir. Each side hands over evidence it
has already read; this only caps and arranges it, so the state a model is scored on and
the state it answers in production cannot drift apart in shape. Pure: no file reads.

The caps are the export's (`forge dataset export`), because those are the states
Clef was measured on: goal and prompt are NOT cut here, unlike score-plan's request.
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
