"""Plan-time difficulty scoring.

Jev rates five properties of a task; **this file**, not the model, turns them into a
tier. That split is the whole design. Weights below can be re-tuned against accrued
outcomes without spending a single request, because re-weighting replays recorded
scores rather than re-asking.

Nothing here writes to tasks.tsv. Scoring is advisory at best and silent in shadow
mode; see references/jev.md for how the result reaches (or does not reach) a decision.
"""
from __future__ import annotations

from pathlib import Path

from . import enabled, threshold
from .client import ask, noul, score
from .questions import (blast_radius, design_judgment, effort_level,
                        independently_verifiable, prompt_adequacy, spec_clarity,
                        state_subtlety, test_coverage)

# forge-dispatch.sh's CANON ladder, weakest first. Kept in the same order as the Score
# levels in questions.effort_level so a position maps onto a word without a lookup that
# could drift.
EFFORT_WORDS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')

# Weights sum to 1.0. design_judgment carries the most because decompose.md defines
# difficulty by it first ("rated ... by what the task demands of the model"), and
# state_subtlety comes second because that same table names concurrency explicitly as a
# thing that makes a ten-line diff `high`.
#
# test_coverage is inverted -- good coverage LOWERS the tier -- and weighted lightly on
# purpose. Coverage does not reduce the judgment a task demands; it only reduces what a
# mistake costs. Letting it dominate would route a genuinely hard change to a cheap
# model because the area happens to be well tested.
WEIGHTS = dict(design_judgment=0.35, state_subtlety=0.25, blast_radius=0.20,
               spec_clarity=0.15, test_coverage=-0.05)

QUESTIONS = dict(design_judgment=design_judgment, state_subtlety=state_subtlety,
                 blast_radius=blast_radius, spec_clarity=spec_clarity,
                 test_coverage=test_coverage)

# Composite cut points. A task exactly on a boundary is the case most likely to be wrong
# in either direction, so BOUNDARY_BAND either side of a cut escalates a tier: paying
# for a stronger model on an ambiguous task is the cheap mistake, and under-routing a
# hard task is the expensive one.
LOW_MAX = 0.33
MEDIUM_MAX = 0.63
BOUNDARY_BAND = 0.05

# Below this, the judgment is too uncertain to act on in either direction, so it
# escalates for the same reason a boundary does.
CONFIDENCE_FLOOR = 0.55

FILE_EXCERPT_CHARS = 600
MAX_EXCERPT_FILES = 6
MEMORY_CHARS = 4000


def _normalized(result, qid: str, levels: int):
    """A Score's position mapped onto 0.0-1.0, with its confidence.

    Score returns a probability-weighted position across ordered levels, so a task
    sitting between 'some' and 'substantial' lands between them rather than being forced
    onto one. Dividing by (levels - 1) makes rubrics with different level counts
    comparable before they are weighted against each other.
    """
    position, confidence = score(result, qid)
    if position is None or levels < 2:
        return None, None
    return max(0.0, min(1.0, float(position) / (levels - 1))), confidence


def tier_for(composite: float) -> str:
    if composite < LOW_MAX:
        return 'low'
    if composite < MEDIUM_MAX:
        return 'medium'
    return 'high'


def _escalate(tier: str) -> str:
    return dict(low='medium', medium='high', high='high')[tier]


def _near_boundary(composite: float) -> bool:
    """Is this close enough to a cut, from BELOW, that it should round up to it?

    Only from below. A composite just above a cut is already on the higher side of that
    boundary, and escalating it again promotes it a whole tier past the line it was
    merely near -- measured on a real plan, a task at 0.373 sat 0.043 above LOW_MAX,
    scored `medium`, and was escalated to `high`. Rounding up to a boundary is a
    tie-break; jumping the next one is not a tie-break at all.
    """
    # Strictly below: at exactly the cut, tier_for already returns the higher tier.
    return any(0.0 < cut - composite <= BOUNDARY_BAND for cut in (LOW_MAX, MEDIUM_MAX))


def combine(dimensions: dict) -> dict | None:
    """Weighted composite -> tier, plus why it may have been escalated.

    `dimensions` maps a rubric name to (normalized_position, confidence). A missing
    dimension is not fatal: the remaining weights are renormalized, because a partial
    answer that is honest about being partial beats refusing to say anything.
    """
    usable = {name: value for name, value in dimensions.items()
              if value and value[0] is not None}
    if not usable:
        return None
    total_weight = sum(abs(WEIGHTS[name]) for name in usable)
    if not total_weight:
        return None

    # test_coverage's weight is negative, so its contribution is inverted here: a
    # well-covered surface pulls the composite down.
    raw = 0.0
    for name, (position, _confidence) in usable.items():
        weight = WEIGHTS[name]
        raw += (1.0 - position) * abs(weight) if weight < 0 else position * weight
    composite = max(0.0, min(1.0, raw / total_weight))

    # Confidence is weighted by the same weights as the composite, because that is what
    # it is the confidence OF. Measured: taking the minimum instead let one low-weight
    # dimension veto a well-determined answer -- on a real task, blast_radius came back
    # at 0.0 while design_judgment, which carries 0.35, was 0.94, and the minimum
    # reported 0.0 for a composite that was mostly settled. The weakest dimension is
    # still reported separately as `weakest`, since it is the one worth looking at.
    scored = [(name, value) for name, value in usable.items() if value[1] is not None]
    confidence = weakest = None
    if scored:
        weight_sum = sum(abs(WEIGHTS[name]) for name, _ in scored)
        confidence = round(sum(abs(WEIGHTS[name]) * value[1]
                               for name, value in scored) / weight_sum, 3)
        weakest = min(scored, key=lambda item: item[1][1])[0]

    tier = tier_for(composite)
    escalated = None
    if _near_boundary(composite):
        escalated = 'boundary'
    elif confidence is not None and confidence < CONFIDENCE_FLOOR:
        escalated = 'low-confidence'
    if escalated:
        tier = _escalate(tier)
    return dict(tier=tier, composite=round(composite, 3), confidence=confidence,
                weakest=weakest, escalated=escalated, base_tier=tier_for(composite),
                dimensions={name: round(value[0], 3) for name, value in usable.items()},
                missing=sorted(set(WEIGHTS) - set(usable)))


def _excerpt(repo: Path, files: str) -> dict:
    out = {}
    for rel in (files or '').replace(',', ' ').split():
        if len(out) >= MAX_EXCERPT_FILES:
            break
        path = repo / rel
        if not path.is_file():
            # A file the task will CREATE is normal and informative in itself.
            out[rel] = '(does not exist yet)'
            continue
        try:
            out[rel] = path.read_text(errors='replace')[:FILE_EXCERPT_CHARS]
        except OSError:
            continue
    return out


def _memory(repo: Path) -> str:
    path = repo / '.forge' / 'memory.md'
    try:
        return path.read_text(errors='replace')[:MEMORY_CHARS] if path.is_file() else ''
    except OSError:
        return ''


def score_task(repo, *, goal: str, task_id: str, title: str, files: str,
               approach: str = '', prompt: str = '', declared_difficulty: str = '',
               run_dir=None, config: dict | None = None) -> dict | None:
    """One request per task. All five rubrics evaluate in parallel server-side.

    Returns None whenever anything at all goes wrong, because a plan that cannot be
    scored must still be a plan.
    """
    repo_path = Path(repo)
    questions = {name: builder() for name, builder in QUESTIONS.items()}
    # `prompt` is the task's requirements -- tasks/<id>/prompt.md, the text the dwarf is
    # dispatched with and the reviewer reads. It was missing from this state until a real
    # plan was scored, and its absence was not visible from any unit test: every rubric
    # still answered, plausibly, about the one-line title.
    #
    # It mattered most to the gate that exists to catch under-specified tasks. Asked
    # whether "the task states requirements specific enough that a reviewer could judge
    # correctness", with only a title in state, the honest answer is no -- so a 460-word
    # spec with a declared output shape and named edge cases scored 0.23 and tripped its
    # own warning. The gate could not have done anything else; it was never shown the
    # requirements it was asked about.
    state = dict(goal=goal, task=dict(id=task_id, title=title, files=files),
                 prompt=prompt, approach=approach,
                 file_excerpts=_excerpt(repo_path, files), memory=_memory(repo_path))
    # The planner's own rating is deliberately NOT sent. Jev is here to be a second,
    # independent opinion on the same evidence; showing it the answer first would buy an
    # agreement rate instead of a judgment.
    # Phase 4's plan-time gates need exactly this state, and questions in one request
    # evaluate in parallel, so folding them in here costs no extra round trip -- the
    # alternative is a second request per task carrying an identical payload.
    # Effort rides along too: same state, same request, no extra round trip. It is
    # advisory in every mode -- acting on it would need its own calibration, and there
    # is no outcome data that says a Jev-chosen effort produces better work.
    questions['effort'] = effort_level()

    gate_ids = {}
    if enabled('gates', config=config):
        gate_ids = dict(prompt_adequacy=prompt_adequacy,
                        independently_verifiable=independently_verifiable)
        for name, builder in gate_ids.items():
            questions[name] = builder()

    result = ask(state, questions, site='jev-score-plan', run_dir=run_dir, config=config)
    if result is None:
        return None

    dimensions = {}
    for name in QUESTIONS:
        dimensions[name] = _normalized(result, name, len(questions[name]['criteria']))
    combined = combine(dimensions)
    if combined is None:
        return None
    combined.update(id=task_id, declared=declared_difficulty)

    effort_position, effort_confidence = score(result, 'effort')
    if effort_position is not None:
        index = max(0, min(len(EFFORT_WORDS) - 1, int(round(float(effort_position)))))
        combined['effort'] = EFFORT_WORDS[index]
        combined['effort_confidence'] = effort_confidence

    # Warnings, not gates: recorded below the threshold and silent above it. A LOW
    # probability is what is worth saying out loud here.
    #
    # Each rubric gets its own threshold because they are not on one scale, which is the
    # same trap `tests_act` set earlier. Measured over 15 hand-labelled task prompts:
    # genuinely vague prompts ("add tests", "make it faster") all landed at 0.04 while
    # specific ones ran 0.55-0.87, and task fragments that need a sibling to land first
    # scored 0.11-0.24 against 0.51-0.72 for self-contained work. The shared gate_warn
    # of 0.60 -- which IS right for drift, where it was measured -- would have warned on
    # 5 of 9 and 5 of 11 perfectly good tasks.
    warn_levels = dict(prompt_adequacy=threshold('prompt_warn', config=config),
                       independently_verifiable=threshold('verifiable_warn', config=config))
    warnings = {}
    for name in gate_ids:
        probability = noul(result, name)
        if probability is not None and probability < warn_levels[name]:
            warnings[name] = round(probability, 3)
    if warnings:
        combined['warnings'] = warnings
    return combined


def acts(judgment: dict, *, config: dict | None = None) -> bool:
    """Is this judgment strong enough to be written into the plan?

    Separate from combine() so the threshold can be read straight out of config and
    tested without a request. Note that an escalated judgment is never acted on: the
    escalation exists precisely because the composite was not trustworthy there.
    """
    if not judgment or judgment.get('escalated'):
        return False
    confidence = judgment.get('confidence')
    return confidence is not None and confidence >= threshold('routing_act', config=config)


# Written only by `forge jev calibrate`, and only when it actually had the sample to
# justify it. Its absence is the normal state and means "advisory".
CALIBRATION_FILE = 'jev-routing-calibration.json'


def calibration(repo) -> dict | None:
    """The stored calibration for this repo, or None if routing is not calibrated here.

    Deliberately per-repo. The verify-discovery measurements in references/jev.md showed
    the same rubric behaving differently across repos, and test selection needed
    thresholds 2.25x apart on two repos, so a calibration earned in one project says
    nothing about another.
    """
    path = Path(repo) / '.forge' / CALIBRATION_FILE
    if not path.is_file():
        return None
    try:
        import json
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or not stored.get('calibrated'):
        return None
    return stored


def may_act(repo, judgment: dict, *, config: dict | None = None) -> bool:
    """The complete gate: calibrated for THIS repo, and confident enough.

    `--jev-act` is not sufficient on its own. Routing decides which model spends the
    user's quota, and an uncalibrated threshold is a guess wearing a number -- the
    measurements already recorded for verify discovery showed a placeholder threshold
    sitting either side of every real value. Until `forge jev calibrate` has the sample
    to say otherwise, this returns False and the human's difficulty column stands.
    """
    stored = calibration(repo)
    if not stored or not judgment:
        return False
    # Per tier, not per repo: a project whose `low` tier has 40 samples and whose `high`
    # tier has 3 is calibrated for one and guessing about the other.
    tiers = stored.get('tiers')
    if not isinstance(tiers, dict) or judgment.get('tier') not in tiers:
        return False
    return acts(judgment, config=config)
