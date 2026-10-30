"""Question builders for the TypeSafe wire format, plus a trivial liveness probe.

Every rubric Jev is ever asked lives in this one file, not scattered at each call site.
That is deliberate: a human auditing what gets sent to an external API, and what
probability threshold decides anything, should be able to read one file top to bottom
instead of hunting through client.py and cli.py for scattered prompts.
"""
from __future__ import annotations


def Noul(instructions: str, *, true: str | None = None, false: str | None = None) -> dict:
    question = dict(type='noul', instructions=instructions)
    if true is not None or false is not None:
        question['criteria'] = {'true': true, 'false': false}
    return question


def Choice(instructions: str, criteria: dict) -> dict:
    if len(criteria) < 2:
        raise ValueError('Choice needs at least 2 options')
    return dict(type='choice', instructions=instructions, criteria=criteria)


def Score(instructions: str, criteria: list) -> dict:
    if not 2 <= len(criteria) <= 10:
        raise ValueError('Score needs between 2 and 10 levels')
    return dict(type='score', instructions=instructions, criteria=criteria)


# Used by `setup`/`doctor --live` to confirm a key actually works, with no ambiguity
# about the expected answer.
PROBE_STATE = 'yes'
PROBE = {'probe': Noul('Is the state the word "yes"?')}


def verify_selection(candidates: list[str]) -> dict:
    """Which existing command, if any, actually verifies this repo by running its tests?

    Asked once per discover() call, over every candidate enumerated from real files in
    the repo (see verify.candidates) plus a 'none' escape hatch. Jev may only choose
    among commands that demonstrably exist -- this rubric selects, it never generates,
    so it cannot invent 'make test' for a repo with no Makefile.
    """
    criteria = {
        command: (
            f'{command!r} runs this project\'s actual test suite end to end -- the same '
            'checks a human would run before trusting a change, not a narrow slice of '
            'it and not an unrelated build, lint, format, or deploy step.'
        )
        for command in candidates
    }
    criteria['none'] = (
        'No candidate above actually runs this project\'s test suite. Every one either '
        'lints, type-checks, builds, formats, or covers only a small, unrelated slice of '
        'the project. Pick this rather than force a bad guess: a wrong pick here reports '
        'an unverified change as verified.'
    )
    instructions = (
        'Given this repo\'s top-level files and any known verify facts (in state), which '
        'of these existing commands actually verifies a change to this project by '
        'running its test suite? Choose \'none\' if nothing listed truly does.'
    )
    return Choice(instructions, criteria)


def runs_tests(command: str) -> dict:
    """Does this command execute tests, as opposed to only build/lint/typecheck/format?

    Asked once per candidate alongside verify_selection, so a command that only builds
    can be rejected even if the Choice rubric picked it confidently -- a build that
    passes is not a verified change.
    """
    instructions = (
        f'Does the command {command!r} execute this project\'s tests, as opposed to '
        'only linting, type-checking, building, formatting, or deploying it?'
    )
    return Noul(
        instructions,
        true=(
            'The command invokes a test runner or test framework (for example pytest, '
            'go test, cargo test, npm test, a test shell script, tox) that exercises the '
            'project\'s actual behavior and can fail when that behavior is wrong.'
        ),
        false=(
            'The command only compiles, bundles, lints, type-checks, formats, or deploys '
            '-- it can succeed while the project\'s behavior is broken. A build or lint '
            'that passes is not a verified change, even if its name contains the word '
            '"check" or "verify".'
        ),
    )


def verify_runtime(command: str) -> dict:
    """How long does this command typically take? Ordered levels, cheapest first.

    Mirrors the kind of fact Forge's own `verify` memory category already records
    ("pytest -q runs the suite; make test also lints and is 4x slower") so the levels
    describe situations exactly like that one.
    """
    instructions = (
        f'How long does {command!r} typically take to run against this project, based '
        'on what its name, scope, and any known verify facts (in state) imply?'
    )
    criteria = [
        'seconds -- a small, focused check: a single test file or a fast unit-test slice.',
        'under a minute -- a normal unit test run with no heavy setup or external services.',
        'minutes -- a fuller suite, integration tests, or a build-then-test pipeline.',
        'very slow -- end-to-end or browser tests, a full CI pipeline, or a command that '
        'bundles lint, typecheck, build and test together (e.g. "make test also lints and '
        'is 4x slower" than a plain test runner).',
    ]
    return Score(instructions, criteria)


def drift_prediction(file_path: str) -> dict:
    """Is this file likely to need editing to complete the described task?

    Asked once per file a Forge task's `files` column does NOT declare, with the task
    description supplied as state. Used to warn when a task's declared touch set
    understates what it will actually touch (see references/decompose.md, "files is a
    promise, not a prediction"). A false positive here is a warning nobody asked for on
    a file that was only ever read; a false negative silently lets real drift through.
    Because the warning itself has a cost (noise erodes trust in every future warning),
    the middle case — a file that might be read or referenced but not edited — is
    written to fall on the false side rather than the true side.
    """
    instructions = (
        'A Forge task is being carried out. Given the task description (in state), is '
        f'the file {file_path!r} likely to need editing to complete this task, as '
        'opposed to merely being read, imported, or referenced?'
    )
    return Noul(
        instructions,
        true=(
            'The task description names this file directly, or names a behavior, '
            'function, route, schema, or config that only this file defines and that '
            'must change for the task to be complete — the task cannot plausibly be '
            'finished without an edit landing here.'
        ),
        false=(
            'The file would only be read, imported, or called into during the task '
            'without itself changing (a dependency, a shared utility invoked but not '
            'modified, a config merely consulted), or the file has no plausible '
            'connection to the task at all. Treat "might be touched incidentally" as '
            'false — this rubric exists to warn on files that will need an edit, not '
            'on every file in the change\'s vicinity, since a warning that fires on '
            'files nobody edits trains people to ignore it.'
        ),
    )

# --- pre-dispatch gates (Phase 4) --------------------------------------------------
#
# Asked by `score-plan` before a plan's table is printed (gates.prompt_adequacy, site
# jev-prompt). They warn and never block: a gate that can stop a dispatch is a gate that
# will eventually stop a good one.


def prompt_adequacy() -> dict:
    """Could a reviewer judge correctness against this prompt at all?

    SKILL.md already requires this of a decomposition ("short titles are insufficient
    for QA") and nothing enforces it. The failure it prevents is expensive and silent:
    a dwarf produces something plausible, and QA has no stated requirement to check it
    against, so a PASS means only "nothing looked wrong".
    """
    instructions = (
        'A Forge task is about to be dispatched to a model, and its diff will then be '
        'reviewed by a separate reviewer who sees this same task text. Does the task '
        '(in state) state requirements specific enough that the reviewer could judge '
        'whether the work is correct?'
    )
    return Noul(
        instructions,
        true=(
            'The task names the behaviour that must hold when it is done -- what should '
            'happen, to what, under which conditions -- specifically enough that a '
            'reviewer reading only this text could point at a diff and say whether it '
            'meets them.'
        ),
        false=(
            'The task states an area, a file, or an intention rather than a '
            'requirement -- "improve error handling", "refactor the parser", "add '
            'tests" -- so a reviewer could only judge whether the diff looks reasonable, '
            'not whether it is correct. A title with no body belongs here.'
        ),
    )


# --- post-review annotation (Phase 4, item 6) -------------------------------
#
# This reads a FAIL and says whether the review is grounded. It NEVER overturns it. Forge's
# second invariant is reviewer independence: a model that can talk another model out of
# a FAIL is worth less than no reviewer at all, because it produces confident PASSes on
# code nobody checked. So the only output here is an annotation a human reads.


def findings_cite_the_diff() -> dict:
    """Citation check: is the review talking about code that is actually there?

    A review that cites a line the diff does not contain is describing something else --
    a stale read, a hallucinated hunk, or the wrong file -- and its verdict is worth
    nothing regardless of how confident it sounds.
    """
    instructions = (
        'Given the review text and the diff it reviewed (in state), do the specific '
        'places the review cites -- files, line numbers, function names, quoted code -- '
        'actually appear in that diff, saying what the review claims they say?'
    )
    return Noul(
        instructions,
        true=(
            'The code the review quotes or points at is present in the diff and matches '
            'the description given of it. Minor paraphrasing or an off-by-a-line '
            'reference is fine as long as the cited code is really there.'
        ),
        false=(
            'The review cites a file, function, or line that the diff does not contain, '
            'or quotes code that does not appear in it, or describes the cited code as '
            'doing something it plainly does not do.'
        ),
    )

# --- forward dependency (Phase 4, item 4) --------------------------------------


def forward_dependency(earlier: str, later: str) -> dict:
    """Is an earlier-wave task written against something a LATER task has not built yet?

    This asks about ordering: a task that runs in wave 1 is reviewed,
    verified and merged before wave 2 exists, so if it was written against wave 2's work
    it is wrong at the moment anyone checks it -- and its own review cannot tell, because
    the thing it is wrong about is not in the tree yet.

    The concrete case: a `readme` task in wave 1 documenting a CLI built in wave 2. It
    passed review and shipped describing an interface that was never built that way.
    """
    instructions = (
        f'Forge runs plan tasks in waves. Task EARLIER: {earlier!r} runs first and is '
        f'reviewed, verified and merged before task LATER: {later!r} has started. '
        'LATER is not a declared dependency of EARLIER, so nothing forces that order to '
        'be safe. Does EARLIER depend on what LATER will build -- describing it, calling '
        'it, or assuming its shape -- so that EARLIER cannot be confirmed correct until '
        'LATER lands?'
    )
    return Noul(
        instructions,
        true=(
            'EARLIER is written against something LATER produces: it documents a command '
            'or interface LATER defines, calls an API LATER adds, or assumes a shape only '
            'LATER fixes. When EARLIER is reviewed that thing does not exist yet, so the '
            'review cannot catch a mismatch and the error ships.'
        ),
        false=(
            'EARLIER stands on its own. It depends only on what already exists in the '
            'repo or on its own files, and nothing in LATER can make it wrong. LATER '
            'building on EARLIER is the normal, safe direction and is not this.'
        ),
    )


# --- memory curation (Phase 5, items 1-3) ------------------------------------------
#
# The stakes here are asymmetric and worth stating. memory.md is injected into EVERY
# dispatch of EVERY future run, so a wrong fact is paid for forever and is corrected
# only when a human happens to read it. The ledger is append-only and never injected, so
# nothing recorded is ever lost -- only injection is gated.


def memory_same_fact(text: str, existing: list) -> dict:
    """Which recorded fact, if any, is this one restating?

    `memory.md` counts recurrence by exact normalised key, which the docs already name
    as a limitation: two runs learning the same thing in different words count as two
    separate facts and neither reaches the promotion threshold. Matching paraphrases to
    the existing key is what makes that threshold work.
    """
    criteria = {
        entry: f'The new note states the same fact as {entry!r}, in different words.'
        for entry in existing
    }
    criteria['none'] = (
        'The new note states something none of the entries above says. Pick this '
        'whenever the match is partial -- merging two facts that only look alike loses '
        'one of them permanently, and a duplicate is merely untidy.'
    )
    return Choice(
        f'A model working in this repo recorded: {text!r}. Which of the facts already '
        'recorded, if any, states that same thing?', criteria)


def memory_durable(text: str) -> dict:
    """A durable project fact, or a one-off observation about today's task?"""
    return Noul(
        f'A model working in this repo recorded: {text!r}. Is this a durable fact about '
        'the project that would still be useful to a different model, working on a '
        'different task, months from now?',
        true=(
            'It states something stable about the project itself -- how it is built, '
            'tested or verified, a trap that will catch the next person, a constraint '
            'that holds across tasks. It would read as true and useful out of context.'
        ),
        false=(
            'It is about this particular change, task, or moment -- what was just '
            'edited, what the model did, a status report, a restatement of the task, or '
            'an observation that is only meaningful next to that diff. Injecting it '
            'into unrelated future work would be noise at best and misleading at worst.'
        ),
    )


def memory_category(text: str) -> dict:
    """verify | trap | finding | none -- the categories references/memory.md defines."""
    criteria = {
        'verify': 'How this project is checked: the command that runs its tests, what '
                  'that command covers, how long it takes, what it needs set up first.',
        'trap': 'A sharp edge that will catch the next person: a flaky test, a generated '
                'file that must not be hand-edited, a surprising dependency, a thing '
                'that looks wrong but is deliberate.',
        'finding': 'A recurring correctness problem in this codebase -- a mistake that '
                   'has been made before and is likely to be made again.',
        'none': 'None of the above fits. Prefer this over forcing a bad fit: a fact '
                'filed under the wrong category is injected into the wrong role\'s '
                'prompt, where it is noise.',
    }
    return Choice(
        f'A model working in this repo recorded: {text!r}. Which kind of fact is it?',
        criteria)


def memory_still_true(text: str, file_content: str) -> dict:
    """Has this fact quietly gone false while its anchor file still exists?"""
    excerpt = (file_content or '')[:4000]
    return Noul(
        f'A fact recorded about this project says: {text!r}. The file it refers to now '
        f'contains:\n\n{excerpt}\n\nIs the recorded fact still true?',
        true='The file still behaves the way the fact describes, or the fact is about '
             'something the file does not contradict.',
        false='The file has changed in a way that makes the fact wrong -- the command, '
              'behaviour, constraint or trap it describes is no longer there.',
    )


def memory_relevant(text: str, task: str) -> dict:
    """Is this fact worth spending prompt space on for THIS task?"""
    return Noul(
        f'A model is about to work on this task: {task!r}. A fact recorded about the '
        f'project says: {text!r}. Is that fact worth including in the prompt for this '
        'particular task?',
        true='The fact bears on what this task touches -- the same area, the same '
             'command, a trap the task could plausibly hit, or a mistake it could '
             'plausibly repeat.',
        false='The fact is true but unrelated to this task. Every line included costs '
              'context on every dispatch, so a fact that will not change what the model '
              'does here should be left out.',
    )


# --- rubrics about one item (C3-C5) ------------------------------------------------
#
# A rubric about one requirement, finding or candidate issue quotes that item into its own
# instructions: the question id alone cannot tie a question to its item, so two items asked
# in one request would otherwise get the same text. A quoted item is cut first, so one long
# one cannot crowd out the state the question is about.
#
# Every Noul here is worded so that a HIGHER probability means the gate acts. A Choice's
# gate acts on `none` (coverage) or on a candidate other than `none` (culprit, duplicate).

QUOTE_CHARS = 600


def _quoted(text) -> str:
    return repr(str(text)[:QUOTE_CHARS])


# Plan covers the request (#47): one Choice per requirement the request splits into.

def requirement_coverage(requirement: str, options: list) -> dict:
    """Which task's acceptance criteria, if any, deliver this requirement of the request?

    Asked once per requirement (coverage.py) over every task id plus 'none'. The gate acts
    on 'none': a requirement no task's criteria cover goes back to the planner. A task whose
    title only sounds related is not coverage, so the rubric says that outright.
    """
    instructions = (
        'A user asked for the work in state.request. The plan splits it into the tasks in '
        'state.tasks, each with its acceptance criteria. Which task\'s acceptance criteria, '
        f'if met, would deliver this requirement: {_quoted(requirement)}? Answer none when no '
        'task\'s criteria cover it, even when a task\'s title sounds related.'
    )
    criteria = {task_id: f'task {task_id} covers it' for task_id in options}
    criteria['none'] = 'no task\'s acceptance criteria cover it'
    return Choice(instructions, criteria)


# A retry touched its blocking findings (#43).

def finding_untouched(finding: str) -> dict:
    """Does the retry's diff leave this blocking finding unaddressed?

    Asked once per P0/P1 finding of a retry (addressed.py). Higher means the gate acts: the
    attempt is bounced before QA. An edit that might fix the finding, even one that may not
    be enough, is the false side.
    """
    return Noul(
        'A reviewer rejected the previous attempt of a Forge task with this finding: '
        f'{_quoted(finding)}. The implementer then changed the code; state.diff is everything '
        'it changed since the reviewed attempt. Does this diff leave the finding unaddressed '
        '-- the code the finding names is untouched, or only unrelated code, comments or tests '
        'that do not exercise the finding\'s case changed?',
        true='The cited code and behaviour are not changed in a way that could fix the finding.',
        false='The diff edits the cited code or the behaviour the finding describes in a way '
              'that could fix it, even if it may not be enough.',
    )


# The dwarf's final message (#44). One request per attempt; message.py picks the first that fires.

def message_unfinished() -> dict:
    """Did the implementer's final message say part of the requested work is left undone?

    Higher means the gate acts: the attempt is sent back as FAIL before its review. Caveats,
    suggestions for later and notes about style are the false side.
    """
    return Noul(
        'Does the implementer\'s final message (state.message) say it did not finish the task '
        '-- that part of the requested work is left undone, skipped, only partly done, or left '
        'as a TODO? Caveats, suggestions for later and notes about style do not count.'
    )


def message_blocked() -> dict:
    """Did the implementer say something outside the task stopped it from doing the work?

    Higher means the gate acts: the attempt is sent back as FAIL before its review. A message
    that reports a finished or merely difficult task is the false side.
    """
    return Noul(
        'Does the message say the implementer was blocked by something outside the task -- a '
        'missing dependency, an environment that fails or is unavailable, a tool or permission '
        'it lacked -- so that it could not do the work?'
    )


def message_question() -> dict:
    """Did the implementer ask a question only the user can answer?

    Higher means the gate acts: the attempt ends NEEDS_INPUT and waits for the answer. A
    choice the requirements already settle is not a question for the user.
    """
    return Noul(
        'Does the message ask a question only the user can answer -- a choice between behaviours '
        'the requirements leave open -- and say it could not proceed correctly without the answer?'
    )


# Integration attribution (#45): which merged task broke a run's combined verification.

def culprit(options: list) -> dict:
    """Which merged task most likely caused the failing combined verification of a run?

    Asked once per failing integration (attribution.py) over every merged task id plus
    'none'. The gate acts on a task: its boundary is probed before the binary search. 'none'
    (environmental, or unrelated to every change) leaves the search alone.
    """
    instructions = (
        'The combined verification of a Forge run failed after these tasks were merged one '
        'after another (state.tasks: id, title, files, diff excerpt). state.log is the end of '
        'the failing verification log. Which merged task most likely caused the failure? Answer '
        'none when the failure looks environmental or unrelated to every listed change.'
    )
    criteria = {task_id: f'task {task_id} most likely caused the failure' for task_id in options}
    criteria['none'] = 'the failure looks environmental, or unrelated to every listed change'
    return Choice(instructions, criteria)


# Findings duplicates and triage (#46): options maps each candidate issue id to its title.

def finding_duplicate(finding: str, options: dict) -> dict:
    """Does this review finding restate one of the existing issues?

    Asked once per finding when there are candidate issues (findings.py), over the candidate
    ids plus 'none'. Higher probability on a candidate means the gate acts: the finding becomes
    a comment on that issue rather than a new one. 'none' is the safe side: a finding that
    only shares a file or topic with an issue is not a duplicate.
    """
    instructions = (
        'Is this review finding -- '
        f'{_quoted(finding)} -- the same defect as one of the existing issues in state.candidates, '
        'described in other words? Same defect means the same broken behaviour in the same code, '
        'not just the same file or topic.'
    )
    criteria = {
        candidate: (f'issue {candidate} ({_quoted(title)}) reports this same defect: the same '
                    'broken behaviour in the same code')
        for candidate, title in options.items()
    }
    criteria['none'] = ('no existing issue reports this defect; sharing a file or a topic with '
                        'one is not a duplicate')
    return Choice(instructions, criteria)


def finding_concrete(finding: str) -> dict:
    """Is this PLAUSIBLE review finding a preference, with no concrete defect behind it?

    Asked once per PLAUSIBLE finding (findings.py). Higher means the gate acts: the finding is
    pre-deselected rather than filed. The question is therefore worded as the preference, not
    as concreteness, so the probability keeps the same direction as every other Noul here. A
    finding that names an input, state or sequence of steps that gives wrong behaviour is the
    false side.
    """
    return Noul(
        'Does this review finding -- '
        f'{_quoted(finding)} -- read as a preference about style, naming or structure rather '
        'than a defect, because it names no specific input, state or sequence of steps that '
        'produces wrong behaviour?',
        true='The finding is a preference about style, naming, structure or taste. It names no '
             'specific input, state or sequence of steps that produces wrong behaviour, so '
             'fixing it would not fix a defect.',
        false='The finding names a concrete defect: a specific input, state or sequence of steps '
              'that produces wrong behaviour, even when it also mentions style or naming.',
    )
