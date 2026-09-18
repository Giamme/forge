"""Curation of what reaches `.forge/memory.md`.

The stakes are asymmetric, and that shapes every default here. `memory.md` is injected
into every dispatch of every future run, so a wrong fact is paid for forever and gets
corrected only when a human happens to read it. The ledger is append-only and never
injected, so **nothing recorded is ever lost** -- this gates injection, not recording.

That asymmetry is why each judgment below fails toward today's behaviour rather than
toward acting: an unreachable API, a missing key, a malformed answer and a disabled
capability all leave the model's own category and key exactly as they were.
"""
from __future__ import annotations

from . import threshold
from .client import ask, choice, noul
from .questions import memory_category, memory_durable, memory_same_fact

CATEGORIES = ('verify', 'trap', 'finding')

# Enough context to match a paraphrase without turning one dispatch's bookkeeping into
# the largest request in the run.
MAX_EXISTING = 40
MAX_TEXT = 500


def curate(*, category: str, text: str, existing=None, run_dir=None,
           config: dict | None = None) -> dict | None:
    """Three judgments in one request: is it a duplicate, is it durable, is it filed right.

    Returns None when nothing could be judged, which callers must treat as "keep what the
    model said". The returned dict carries only *suggestions*; `key` is None unless a
    paraphrase was matched confidently enough to merge.
    """
    text = (text or '').strip()[:MAX_TEXT]
    if not text:
        return None
    entries = [line for line in (existing or []) if line][:MAX_EXISTING]

    questions = dict(durable=memory_durable(text), category=memory_category(text))
    if entries:
        # Choice needs at least two options; with no existing entries there is nothing
        # to be a duplicate of, so the question is not worth asking.
        questions['same'] = memory_same_fact(text, entries)

    result = ask(dict(recorded=text, existing=entries), questions,
                 site='jev-memory-curate', run_dir=run_dir, config=config)
    if result is None:
        return None

    durable = noul(result, 'durable')
    suggested, category_confidence = choice(result, 'category')
    matched, match_confidence = (choice(result, 'same') if entries else (None, None))

    out = dict(durable=durable, category=category, key=None, matched=None,
               suggested_category=suggested, category_confidence=category_confidence)

    # A paraphrase only merges above the threshold. Merging two facts that merely look
    # alike loses one of them permanently, while a duplicate is only untidy -- so the
    # bar sits on the side of keeping both.
    if (matched and matched != 'none' and match_confidence is not None
            and match_confidence >= threshold('memory_merge', config=config)):
        out['matched'] = matched
        out['match_confidence'] = match_confidence

    # Category is corrected only toward a real category. 'none' means the model could not
    # place it, which is a reason to leave the recorded category alone, not to discard it.
    if (suggested in CATEGORIES and suggested != category
            and category_confidence is not None
            and category_confidence >= threshold('memory_recategorize', config=config)):
        out['category'] = suggested

    # The quality gate. Silence is the safe direction: a rejected line still lands in the
    # ledger in full, so the only thing lost is injection into future prompts.
    out['injectable'] = not (durable is not None and durable < threshold('gate_warn', config=config))

    # A line that merged onto an existing entry is exempt, and the two judgments have to
    # be read together to see why. `rebuild` skips a non-injectable row before it counts
    # anything, and a `finding` needs two DISTINCT runs before it is promoted -- which is
    # the whole reason dedup exists, since two runs wording one fact differently would
    # otherwise never reach two. So a restatement judged not durable is dropped from the
    # count of the fact it restates, defeating dedup in exactly the case it was built for.
    #
    # Measured: paraphrases score 0.44-0.83 on durability against 0.82-0.83 for the same
    # facts stated fresh -- a restatement reads as less durable than the thing it
    # restates, which is the wrong question. The entry it matched is already in
    # memory.md, so its durability was settled when that entry was written; a line
    # matching it is by definition not the one-off observation this gate is for.
    if out['matched']:
        out['injectable'] = True
    return out


# The number of facts a single dispatch is likely to act on. The existing 40-line / 4KB
# cap stays the outer bound -- this only narrows within it, and never adds a line.
SLICE_KEEP = 8
FILE_CHARS = 4000


def still_true(*, text: str, anchor_path, run_dir=None, config: dict | None = None):
    """Has this fact gone false while the file it points at still exists?

    Path-anchor staleness already drops an entry whose file was deleted. This catches
    the other case: the file is still there and the fact about it stopped being true.
    Returns None when nothing could be judged, which must be read as "keep the entry" --
    dropping a fact on a failed request would quietly erase memory over time.
    """
    from .questions import memory_still_true
    from pathlib import Path

    try:
        content = Path(anchor_path).read_text(errors='replace')[:FILE_CHARS]
    except OSError:
        return None
    if not (text or '').strip():
        return None
    result = ask(dict(fact=text, file=str(anchor_path)),
                 dict(still=memory_still_true(text, content)),
                 site='jev-memory-stale', run_dir=run_dir, config=config)
    if result is None:
        return None
    probability = noul(result, 'still')
    if probability is None:
        return None
    # Its own threshold, well below gate_warn. Measured over six facts against real
    # files: ones that had genuinely gone false scored 0.03-0.10, ones still true scored
    # 0.47-0.96. gate_warn's 0.60 sits inside the true range and would have deleted a
    # fact the file still supports. Dropping a fact is irreversible and silent, while
    # keeping a stale one costs a line until someone notices, so the bar sits low enough
    # that only a confident "this went false" acts.
    return dict(probability=probability,
                stale=probability < threshold('stale_drop', config=config))


def relevant_lines(*, task: str, lines: list, keep: int = SLICE_KEEP,
                   run_dir=None, config: dict | None = None):
    """The subset of memory worth spending prompt space on for THIS task.

    Returns None when no judgment could be made, so the caller injects everything it
    would have injected before. Never returns more lines than it was given, and never
    reorders them -- the section structure above these lines is what gives them meaning.
    """
    from .questions import memory_relevant

    lines = [line for line in (lines or []) if line.strip()]
    if len(lines) <= keep:
        # Nothing to narrow, and asking would cost a request to learn that.
        return None
    questions = {f'l{i}': memory_relevant(line, task) for i, line in enumerate(lines)}
    result = ask(dict(task=task, facts=lines), questions, site='jev-memory-slice',
                 run_dir=run_dir, config=config)
    if result is None:
        return None

    scored = []
    for i, line in enumerate(lines):
        probability = noul(result, f'l{i}')
        if probability is None:
            # An unscored line is kept: absence of a judgment is not evidence against.
            probability = 1.0
        scored.append((probability, i, line))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = {index for _p, index, _line in scored[:keep]}
    return [line for i, line in enumerate(lines) if i in chosen]
