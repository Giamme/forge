"""Natural language to typed routing flags.

`registry.tsv` is a closed set of aliases, which makes "which model did they mean" a
Choice over a fixed list rather than free generation. That distinction is what keeps
this inside Forge's first invariant: the user chose the models, and Jev only maps their
sentence onto aliases they already have. Nothing here is applied -- the result is echoed
back as flags for the user to confirm, copy, or ignore.
"""
from __future__ import annotations

from pathlib import Path

from . import threshold
from .client import ask, choice
from .questions import spec_from_sentence

TIERS = ('low', 'medium', 'high')


def aliases(skill_dir) -> dict:
    """Every alias in registry.tsv mapped to the model ids it resolves to.

    The model ids matter. An alias on its own is an opaque name -- `sol`, `luna`,
    `haiku` -- and asking which one is "cheap" or "strongest" from the name alone is
    unanswerable, which is exactly what happened when this sent bare aliases: every
    tier came back `none` at 0.86-1.00 confidence. The model id is the only thing in
    the registry that says what an alias actually is.

    One alias appears on several rows (one per harness pairing), so ids are collected
    per alias and the order of first appearance is preserved.
    """
    path = Path(skill_dir) / 'registry.tsv'
    found = {}
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return {}
    for line in lines:
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        alias, model = parts[0].strip(), parts[2].strip()
        if not alias:
            continue
        models = found.setdefault(alias, [])
        # The last path segment: `github-copilot/claude-opus-5` says no more than
        # `claude-opus-5` does, and the prefix repeats across every opencode row.
        model = model.rsplit('/', 1)[-1]
        if model and model not in models:
            models.append(model)
    return found


def parse(sentence: str, *, skill_dir, run_dir=None, config: dict | None = None):
    """Returns {tier: alias} for the tiers the sentence actually names.

    A tier the sentence does not mention is absent from the result rather than guessed
    at: proposing a model nobody asked for is how a confirmation prompt gets approved
    out of habit and spends quota on the wrong thing.
    """
    catalogue = aliases(skill_dir)
    if not catalogue or not (sentence or '').strip():
        return None
    options = list(catalogue)
    questions = {tier: spec_from_sentence(tier, options) for tier in TIERS}
    result = ask(dict(request=sentence, available_models=catalogue), questions,
                 site='jev-parse-spec', run_dir=run_dir, config=config)
    if result is None:
        return None

    floor = threshold('gate_warn', config=config)
    chosen = {}
    for tier in TIERS:
        alias, confidence = choice(result, tier)
        if (alias and alias != 'none' and alias in options
                and confidence is not None and confidence >= floor):
            chosen[tier] = dict(alias=alias, confidence=confidence)
    return chosen


def as_flags(chosen: dict) -> str:
    """The flags a user could paste, in tier order."""
    return ' '.join(f'--dwarf-{tier} {chosen[tier]["alias"]}'
                    for tier in TIERS if tier in chosen)
