"""Read Jev run logs back and report what each rubric actually returned.

Every threshold in this package was, at some point, fitted to numbers the model was not
actually producing -- a rubric measured against a state production never sent, or a
threshold borrowed from a differently-scaled rubric. Two things make that visible after
the fact, and both are already in `jev.jsonl`:

- the distribution each (site, question) returned across a run, so a threshold can be
  placed against real answers rather than remembered ones;
- the spread across byte-identical repeated requests (same `key`), which is the noise
  floor: a margin smaller than that spread is measuring nothing.

This reads only; it never sends a request.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median


def logs_under(paths) -> list[Path]:
    """Every `jev.jsonl` at or below the given paths, in a stable order.

    A solo run dir holds one; a plan dir holds its own plus one per `tasks/<id>/`.
    Passing the file itself also works.
    """
    found = set()
    for given in paths:
        given = Path(given)
        if given.is_file():
            found.add(given.resolve())
        elif given.is_dir():
            for log in given.rglob('jev.jsonl'):
                found.add(log.resolve())
    return sorted(found)


def _value(answer: dict) -> float | None:
    """The one number a threshold would look at for this answer type."""
    if not isinstance(answer, dict):
        return None
    kind = answer.get('type')
    if kind == 'noul':
        value = answer.get('noul')
    elif kind in ('choice', 'score'):
        value = answer.get('confidence')
    else:
        return None
    return float(value) if isinstance(value, (int, float)) else None


def collect(logs) -> dict:
    """Group answers by (site, question id) and repeats by request key."""
    values: dict[tuple[str, str], list[float]] = {}
    repeats: dict[tuple[str, str], dict[str, list[float]]] = {}
    skips: dict[str, dict[str, int]] = {}
    rows = 0
    for log in logs:
        try:
            lines = Path(log).read_text(errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            rows += 1
            site = str(row.get('site', '?'))
            if not row.get('ok'):
                reason = str(row.get('reason') or 'unknown').split(':', 1)[0]
                skips.setdefault(site, {})
                skips[site][reason] = skips[site].get(reason, 0) + 1
                continue
            key = row.get('key')
            for qid, answer in (row.get('answers') or {}).items():
                value = _value(answer)
                if value is None:
                    continue
                values.setdefault((site, qid), []).append(value)
                if isinstance(key, str):
                    repeats.setdefault((site, qid), {}).setdefault(key, []).append(value)
    return dict(rows=rows, values=values, repeats=repeats, skips=skips)


def report(paths) -> dict:
    logs = logs_under(paths)
    data = collect(logs)
    rubrics = []
    for (site, qid), seen in sorted(data['values'].items()):
        spread = None
        groups = [g for g in data['repeats'].get((site, qid), {}).values() if len(g) > 1]
        if groups:
            spread = max(max(g) - min(g) for g in groups)
        rubrics.append(dict(site=site, question=qid, n=len(seen), min=round(min(seen), 3),
                            median=round(median(seen), 3), max=round(max(seen), 3),
                            repeated_requests=len(groups),
                            repeat_spread=None if spread is None else round(spread, 3)))
    spreads = [r['repeat_spread'] for r in rubrics if r['repeat_spread'] is not None]
    return dict(logs=[str(p) for p in logs], rows=data['rows'], rubrics=rubrics,
                noise_floor=max(spreads) if spreads else None,
                skips={site: dict(sorted(v.items())) for site, v in sorted(data['skips'].items())})


def render(result: dict) -> str:
    lines = [f"{len(result['logs'])} log(s), {result['rows']} request(s)"]
    if result['rubrics']:
        lines.append('')
        lines.append(f"{'site':<18} {'question':<26} {'n':>4} {'min':>6} {'median':>6} {'max':>6} {'repeats':>7} {'spread':>6}")
        for r in result['rubrics']:
            spread = '-' if r['repeat_spread'] is None else f"{r['repeat_spread']:.3f}"
            lines.append(f"{r['site']:<18} {r['question']:<26} {r['n']:>4} {r['min']:>6.3f} "
                         f"{r['median']:>6.3f} {r['max']:>6.3f} {r['repeated_requests']:>7} {spread:>6}")
    if result['noise_floor'] is not None:
        lines.append('')
        lines.append(f"noise floor: {result['noise_floor']:.3f} -- the largest spread across "
                     'byte-identical repeated requests; a threshold margin below this is noise')
    else:
        lines.append('')
        lines.append('noise floor: unknown -- no request was repeated byte-identically')
    if result['skips']:
        lines.append('')
        lines.append('skipped:')
        for site, reasons in result['skips'].items():
            lines.append('  ' + site + ': ' + ', '.join(f'{k} x{v}' for k, v in reasons.items()))
    return '\n'.join(lines) + '\n'
