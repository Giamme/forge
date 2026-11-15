"""The name a recorded dispatch is counted under in statistics keyed by alias.

A dispatch records the alias it was asked for and the model id that alias resolved to then.
When an alias later moves to a newer model (`terra` from gpt-5.6-terra to gpt-6.1-terra), its
old dispatches must not be counted as the new model's. `dispatch_name` names such a dispatch
by the alias that resolves to its recorded model today on the same harness (`terra-5.6`).
When no alias does, it keeps the alias it recorded: moving an alias without a versioned alias for
the old model is a choice to count both together. Statistics keyed by model id (routing,
forecast, efforts, pairs) need none of this.
"""
from __future__ import annotations

import os
from pathlib import Path

# No package import: forge-ui loads this file on its own to name its model rows the same way.
SKILL = Path(__file__).resolve().parents[2]

_CACHE = {}


def registry_models() -> dict:
    """{alias: {harness: model id}}; the first row of an alias is its default."""
    out = {}
    path = Path(os.environ.get('FORGE_REGISTRY') or str(SKILL / 'registry.tsv'))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) >= 3:
            out.setdefault(cells[0], {}).setdefault(cells[1], cells[2])
    return out


def _registry() -> dict:
    key = os.environ.get('FORGE_REGISTRY') or ''
    if key not in _CACHE:
        _CACHE[key] = registry_models()
    return _CACHE[key]


def dispatch_name(d: dict, registry: dict | None = None):
    """The alias (or model id) a dispatch's event data `d` is counted under, or None."""
    alias, model, harness = d.get('alias'), d.get('model'), d.get('harness')
    if not alias:
        return model or None
    rows = (_registry() if registry is None else registry).get(alias)
    if not rows or not model:
        return alias
    current = rows.get(harness) if harness else None
    if current is None:
        current = next(iter(rows.values()))
    if current == model:
        return alias
    table = _registry() if registry is None else registry
    for other, other_rows in table.items():
        if other == alias:
            continue
        found = other_rows.get(harness) if harness else next(iter(other_rows.values()), None)
        if found == model:
            return other
    return alias
