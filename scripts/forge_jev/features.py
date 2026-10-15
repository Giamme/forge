"""Plain numeric features of a task, for a decision model that reads a routing state.

A task's size is the strongest regime signal in forge's run history: the median prompt of
the newest runs is twice the older ones' and their first-pass rate is half. A model reading
prose and a 40-entry sibling list can only estimate that, so the state spells it out.

The dataset export adds this block to the routing state it trains on. Hosted Jev's live
request does not carry it (changing that request changes every fixture key), so a local
model wired in later must send the same block, with the exact plan size and decompose
level: the live state caps siblings at 40 and has no level. No I/O here; callers pass values.
"""
from __future__ import annotations


def _count(value) -> int:
    if isinstance(value, (list, tuple)):
        return len([v for v in value if v])
    return len([w for w in str(value or '').replace(',', ' ').split() if w and w != '-'])


def task_size(*, prompt: str, declared_files, plan_tasks: int, deps, decompose_level: str | None) -> dict:
    return dict(prompt_chars=len(prompt or ''), declared_files=_count(declared_files),
                plan_tasks=int(plan_tasks), deps=_count(deps),
                decompose_level=decompose_level if decompose_level in ('low', 'medium', 'high') else None)
