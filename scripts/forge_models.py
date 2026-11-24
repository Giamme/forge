#!/usr/bin/env python3
"""The model a spec runs, said in words people can check: `sol:high` is gpt-6.2-sol on codex.

An alias is one model on every harness (registry.tsv, #187), but the alias alone does not say
which version that is today. Everywhere a person picks or confirms a model (the approval table,
forge-ui's pickers, `forge setup`) shows the model id the spec resolves to, so nobody has to know
that `astra` moved from gpt-6-astra to gpt-6.2-astra.

  forge_models.py describe SPEC...       one line per spec: `SPEC = MODEL on HARNESS`
  forge_models.py legend TASKS_TSV       the approval table's models block for a plan's specs
  forge_models.py json SPEC...           {spec: {alias, harness, model, effort, known, label}}

Resolution is forge-dispatch.sh's: the first row of an alias is its default harness,
alias:effort:harness picks that harness's row, and an unknown alias with a harness is a literal
model id. Standard library only; never reads anything but the registry.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent

# CLI names that are not model versions: the CLI maps them to its current model of that family.
MOVING_NAMES = {'opus': 'Opus', 'sonnet': 'Sonnet', 'haiku': 'Haiku', 'fable': 'Fable'}
NOT_MODELS = ('', '-', 'UNASSIGNED', 'script')


def registry_rows(path=None) -> list:
    path = path or os.environ.get('FORGE_REGISTRY') or str(SKILL / 'registry.tsv')
    rows = []
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) >= 3:
            rows.append(cells)
    return rows


def resolve(spec: str, rows: list | None = None) -> dict | None:
    """{alias, effort, harness, model, known} for one spec, or None when it names no model
    (empty, `script`, a pool) or cannot be resolved (an unknown alias with no harness)."""
    spec = (spec or '').strip()
    if spec in NOT_MODELS or ',' in spec:
        return None
    rows = registry_rows() if rows is None else rows
    parts = spec.split(':')
    alias = parts[0]
    effort = parts[1] if len(parts) > 1 else ''
    harness = ':'.join(parts[2:]) if len(parts) > 2 else ''
    if not alias:
        return None
    for row in rows:
        if row[0] == alias and (not harness or row[1] == harness):
            return dict(alias=alias, effort=effort, harness=row[1], model=row[2], known=True)
    if harness:
        return dict(alias=alias, effort=effort, harness=harness, model=alias, known=False)
    return None


def label(resolved: dict | None) -> str:
    """`gpt-6.2-sol on codex`; `opus on claude (the CLI's current Opus)`; `kimi-x on opencode
    (literal id, not in the registry)`; `unresolved` for None."""
    if not resolved:
        return 'unresolved'
    text = '%s on %s' % (resolved['model'], resolved['harness'])
    family = MOVING_NAMES.get(resolved['model'])
    if family:
        text += " (the CLI's current %s)" % family
    elif not resolved['known']:
        text += ' (literal id, not in the registry)'
    return text


def describe(spec: str, rows: list | None = None) -> str:
    return label(resolve(spec, rows))


def pool(cell: str) -> list:
    """The specs of a tasks.tsv dwarf or qa cell: one spec, or a comma-separated pool."""
    return [s.strip() for s in (cell or '').split(',') if s.strip() and s.strip() not in NOT_MODELS]


def plan_specs(tasks_tsv) -> list:
    """Every distinct spec in a plan's dwarf and qa columns, in first-seen order."""
    seen = []
    try:
        lines = Path(tasks_tsv).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return seen
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        for column in (4, 5):
            if len(cells) > column:
                for spec in pool(cells[column]):
                    if spec not in seen:
                        seen.append(spec)
    return seen


def legend(specs: list, rows: list | None = None) -> list:
    """`  sol:high   gpt-6.2-sol on codex` lines, aligned, for each spec that names a model."""
    rows = registry_rows() if rows is None else rows
    if not specs:
        return []
    width = max(len(s) for s in specs)
    return ['  %-*s  %s' % (width, s, describe(s, rows)) for s in specs]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__.strip())
        return 0 if argv else 2
    command, args = argv[0], argv[1:]
    rows = registry_rows()
    if command == 'describe':
        for spec in args:
            print('%s = %s' % (spec, describe(spec, rows)))
        return 0
    if command == 'legend':
        if len(args) != 1:
            print('usage: forge_models.py legend TASKS_TSV', file=sys.stderr)
            return 2
        lines = legend(plan_specs(args[0]), rows)
        if lines:
            print('models (what each spec runs today):')
            print('\n'.join(lines))
        return 0
    if command == 'json':
        out = {}
        for spec in args:
            found = resolve(spec, rows)
            out[spec] = dict(found, label=label(found)) if found else dict(label=label(None))
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    print('forge_models.py: unknown command %r (describe, legend, json)' % command, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
