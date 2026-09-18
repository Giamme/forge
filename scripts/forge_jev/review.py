"""Post-review annotation of a FAIL.

Reads a reviewer's FAIL and says whether it looks sound. It cannot change the verdict,
and no caller is given a way to. Forge's second invariant is reviewer independence: a
model that can talk another model out of a FAIL is worth less than no reviewer at all,
because what it produces is a confident PASS on code nobody checked.

So the output is an annotation for a human, and the status file is never touched. The
asymmetry is deliberate in the other direction too -- nothing here ever annotates a PASS
into looking suspect, because a PASS that gets merged is the outcome that needs the
reviewer's judgement left alone.
"""
from __future__ import annotations

from pathlib import Path

from . import threshold
from .client import ask, noul, score
from .questions import failure_is_correctness, findings_cite_the_diff

# A diff large enough to exceed this is one no reviewer read carefully either. Truncating
# is honest: the annotation is advisory, and a partial read is flagged in the output.
DIFF_CHARS = 24000
REVIEW_CHARS = 12000


def _read(path, limit: int) -> tuple[str, bool]:
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return '', False
    return text[:limit], len(text) > limit


def annotate_failure(*, review_path, diff_path, run_dir=None, config: dict | None = None):
    """Judge a FAIL. Returns None when no judgment could be made.

    Both questions travel in one request. The result carries `suspect` when either
    probability falls below `gate_warn`, which is the only thing a caller should branch
    on -- and the only branch available is what to print.
    """
    review, review_truncated = _read(review_path, REVIEW_CHARS)
    diff, diff_truncated = _read(diff_path, DIFF_CHARS)
    if not review.strip() or not diff.strip():
        return None

    questions = dict(correctness=failure_is_correctness(), citation=findings_cite_the_diff())
    state = dict(review=review, diff=diff,
                 truncated=dict(review=review_truncated, diff=diff_truncated))
    result = ask(state, questions, site='jev-review-triage', run_dir=run_dir, config=config)
    if result is None:
        return None

    correctness = noul(result, 'correctness')
    citation = noul(result, 'citation')
    if correctness is None and citation is None:
        return None

    warn_at = threshold('gate_warn', config=config)
    reasons = []
    citation_bad = citation is not None and citation < warn_at
    if citation_bad:
        reasons.append('the review cites code that may not be in this diff')
    elif correctness is not None and correctness < warn_at:
        # Only when the citations hold up. A review that points at a file the diff does
        # not contain scores low on correctness too, but reporting that as "these are
        # style preferences" describes the wrong problem: nothing can be said about the
        # correctness of code that is not there. Measured on a hallucinated review --
        # citation 0.02, correctness 0.45 -- where both reasons printed and one was wrong.
        reasons.append('the findings look like style preferences rather than correctness bugs')
    return dict(correctness=correctness, citation=citation, suspect=bool(reasons),
                reasons=reasons, truncated=review_truncated or diff_truncated)

# forge-dispatch.sh's ladder, weakest first, matching qa_effort's Score levels.
QA_EFFORT_WORDS = ('low', 'medium', 'high', 'xhigh', 'max')


def size_review(*, diff_path, task_path=None, run_dir=None, config: dict | None = None):
    """Suggest a review effort for this diff. Advisory only; never skips a review.

    Returned, printed and recorded -- never applied. Sizing a review down is exactly the
    decision that should not rest on an uncalibrated threshold, because its failure mode
    is a bug that a cheaper review missed and nobody ever learns about.
    """
    from .questions import qa_effort

    diff, truncated = _read(diff_path, DIFF_CHARS)
    if not diff.strip():
        return None
    task = _read(task_path, REVIEW_CHARS)[0] if task_path else ''
    result = ask(dict(diff=diff, task=task, truncated=truncated),
                 dict(effort=qa_effort()), site='jev-qa-effort', run_dir=run_dir,
                 config=config)
    if result is None:
        return None
    position, confidence = score(result, 'effort')
    if position is None:
        return None
    index = max(0, min(len(QA_EFFORT_WORDS) - 1, int(round(float(position)))))
    return dict(effort=QA_EFFORT_WORDS[index], confidence=confidence)

