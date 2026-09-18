"""Question builders for the TypeSafe wire format, plus a trivial liveness probe.

Every rubric Jev is ever asked lives in this one file, not scattered at each call site.
That is deliberate: a human auditing what gets sent to an external API, and what
probability threshold decides anything, should be able to read one file top to bottom
instead of hunting through backtest.py, client.py and cli.py for scattered prompts.
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


def test_relevance(test_path: str, header: str = '') -> dict:
    """Could this test plausibly be broken by the change under review?

    Asked once per candidate test, with the task description and the changed source
    files supplied as state. Used both by backtest (scored against which tests a human
    actually touched in the same commit) and, eventually, by live test selection.

    The true/false split is deliberately asymmetric: a missed test is a missed
    regression (the change ships broken and nobody notices until later), while an
    extra test only costs a few seconds of runtime. So "true" is written to capture
    genuine uncertainty, not just confident matches — a test that touches the same
    module, calls the same function, or exercises adjacent behavior should land on the
    "true" side even without a slam-dunk import-graph connection. Calibration should
    reflect that a borderline-relevant test is still worth running.
    """
    instructions = (
        'A change is being made to a codebase. Given the task description and the '
        f'source files it changed (in state), could the test at {test_path!r} '
        'plausibly be broken by that change?'
    )
    if header:
        instructions += f'\n\nFirst lines of that test file:\n{header}'
    return Noul(
        instructions,
        true=(
            'The test exercises the same module, function, class, route, or behavior '
            'that the change touches, directly or through a dependency; the test name '
            'or file path names the same feature area as the change; or the '
            'relationship is genuinely uncertain rather than clearly absent. Missing a '
            'test that should have run is a missed regression, so borderline cases '
            'belong here.'
        ),
        false=(
            'The test exercises an unrelated module, feature, or layer with no '
            'plausible dependency on the changed files — for example a test for the '
            'CLI when only a database migration changed, or a test fixture that is '
            'unaffected by the change. Only mark false when the lack of relevance is '
            'clear, since an unnecessary extra test only costs runtime.'
        ),
    )


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


def failure_triage(traps: list[str]) -> dict:
    """Why did verification fail: a real regression, a known flake, environment, or drift?

    Asked once per verification failure. Getting this wrong ships a regression, so
    'real_regression' is written as the default reading and 'known_flake' is written to
    require positive evidence -- a named trap, or an error signature characteristic of
    flakiness (timeout, ordering, port in use, a race) -- never just "it failed and a
    rerun might pass."
    """
    named = '; '.join(traps) if traps else '(none recorded)'
    instructions = (
        'Verification just failed. Given the command that was run, the tail of its '
        f'output, and this repo\'s known traps (in state -- recorded traps: {named}), '
        'classify why it failed.'
    )
    criteria = {
        'real_regression': (
            'DEFAULT reading. The failure looks like the change under test actually '
            'broke something: a specific assertion, a new error, or a changed output '
            'tied to the kind of change being verified. Pick this whenever the failure '
            'is not clearly explained by one of the other options -- an ambiguous '
            'failure is a regression until proven otherwise.'
        ),
        'known_flake': (
            'Requires positive evidence, not just an unwanted failure: the failing test '
            'or command is named among the recorded traps above, OR the error signature '
            'is characteristic of flakiness with no link to the change under test -- a '
            'timeout, a test-ordering or isolation failure, "address already in use" / '
            'port in use, or an evident race condition.'
        ),
        'environment': (
            'The failure is about the environment, not the code or a flaky test: a '
            'missing binary, dependency, credential, network resource, or a disk or '
            'permission error unrelated to test logic.'
        ),
        'generated_drift': (
            'The failure is a diff against a generated or derived artifact (a lockfile, '
            'a snapshot, generated bindings or code) that is expected to regenerate, not '
            'a behavioral failure.'
        ),
    }
    return Choice(instructions, criteria)


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

# --- routing (Phase 3 rubrics, recorded in shadow mode before they decide anything) ---
#
# Five Scores rather than one Choice asking for the tier directly. Two reasons. First,
# `decompose.md` already defines difficulty as a conjunction of distinct properties
# ("design judgment ... cross-cutting ... subtle edge cases or state/concurrency ... the
# right approach is not obvious"), and asking about each separately is what lets code,
# not the model, decide how to weigh them. Second, the composite is then computed here,
# so re-tuning weights against accrued outcomes costs nothing -- no re-inference.
#
# The levels below are quoted from decompose.md's difficulty table wherever it says the
# same thing, so that the rubric and the human-facing spec cannot drift apart.


def design_judgment() -> dict:
    """How much design judgment does this task demand? (decompose.md's primary axis.)"""
    instructions = (
        'Given the task and its context (in state), how much design judgment does '
        'carrying it out demand? Rate what the task demands of whoever does it, NOT how '
        'much code it produces: a five-hundred-line mechanical rename demands little, a '
        'ten-line concurrency fix demands a lot.'
    )
    return Score(instructions, [
        'none -- mechanical and local: a rename, a move, a docs edit, or a test for '
        'behaviour that already exists. A mistake would be obvious.',
        'some -- a self-contained implementation against a clear spec. A few choices to '
        'make, all of them local and reversible.',
        'substantial -- several defensible approaches exist and the choice between them '
        'changes the shape of the result.',
        'the approach must be invented -- the right way to do this is not obvious from '
        'the goal, and getting it wrong means the work is wasted rather than merely '
        'imperfect.',
    ])


def blast_radius() -> dict:
    """How far can a mistake in this task reach?"""
    instructions = (
        'If this task were done wrong, how far would the damage reach through the rest '
        'of the codebase (state describes the repo and the files the task declares)?'
    )
    return Score(instructions, [
        'local -- confined to the lines the task edits; nothing else observes the change.',
        'one module -- other code in the same module or package is affected, but the '
        'boundary holds.',
        'cross-module -- behaviour other parts of the project depend on changes, so a '
        'mistake surfaces somewhere the task never touched.',
        'a public contract -- an API, schema, wire format, CLI surface or file format '
        'that callers outside this repo, or already-stored data, depend on.',
    ])


def state_subtlety() -> dict:
    """Concurrency and state -- called out by name in decompose.md's `high` row."""
    instructions = (
        'How much subtle state or concurrency does this task involve? decompose.md '
        'names this on its own because it is the property most often underrated: a very '
        'small diff can still be the hardest kind of change.'
    )
    return Score(instructions, [
        'none -- pure or straight-line code; no shared state, no ordering, no time.',
        'sequential state -- state is read and written, but in one place, in a '
        'predictable order, by one caller at a time.',
        'shared or asynchronous state -- concurrency, locks, retries, caching, '
        'cross-process coordination, or ordering that is not obvious from reading the '
        'code, where a wrong interleaving produces a bug that does not reproduce.',
    ])


def spec_clarity() -> dict:
    """Is the approach given, or must it be worked out? Note the inverted direction."""
    instructions = (
        'How clearly does the task (and its approach.md, if state carries one) state '
        'HOW the work should be done, as opposed to only what outcome is wanted?'
    )
    return Score(instructions, [
        'the approach is stated -- the task says which files change and what to do in '
        'them; carrying it out is following instructions.',
        'the approach is implied -- the outcome is clear and there is an obvious way to '
        'reach it, but nobody has written it down.',
        'the approach must be invented -- the task states a goal and leaves the means '
        'open, so the first real work is deciding what to build.',
    ])


def test_coverage() -> dict:
    """Would a mistake here be caught? Coverage is why a wrong `low` is survivable."""
    instructions = (
        'How well is the behaviour this task touches already covered by tests in this '
        'repo (state lists the repo\'s files and the files the task declares)?'
    )
    return Score(instructions, [
        'none -- nothing exercises this behaviour; a regression ships silently.',
        'partial -- some tests touch the area, but not the specific behaviour being '
        'changed.',
        'well covered -- the behaviour has direct tests that would fail on a mistake.',
    ])

# --- pre-dispatch gates (Phase 4) --------------------------------------------------
#
# These ride in the SAME request as the routing rubrics. The state a gate needs -- goal,
# task, declared files, approach -- is exactly the state routing already sends, and
# questions in one request evaluate in parallel, so asking them costs no extra round
# trip. They warn and never block: a gate that can stop a dispatch is a gate that will
# eventually stop a good one.


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


def independently_verifiable() -> dict:
    """decompose.md's hard rule, checked: can this task be verified on its own?"""
    instructions = (
        'Forge runs this task in its own worktree and verifies it alone, before any '
        'other task in the plan is merged. Can this task (in state) be verified on its '
        'own, or does confirming it works require another task to land first?'
    )
    return Noul(
        instructions,
        true=(
            'Once this task is done, something observable proves it: a test that can '
            'run, a command that behaves differently, a check that passes. The proof '
            'does not depend on work declared in a different task.'
        ),
        false=(
            'The task is a fragment -- one half of a rename, a caller without its '
            'callee, an interface with no implementation -- so nothing can confirm it '
            'works until a sibling task lands. decompose.md forbids splitting this '
            'finely for exactly this reason.'
        ),
    )

# --- post-review annotation (Phase 4, items 5 and 6) -------------------------------
#
# These read a FAIL and say whether it looks sound. They NEVER overturn it. Forge's
# second invariant is reviewer independence: a model that can talk another model out of
# a FAIL is worth less than no reviewer at all, because it produces confident PASSes on
# code nobody checked. So the only output here is an annotation a human reads.


def failure_is_correctness() -> dict:
    """Does this FAIL rest on a correctness bug, or on taste?

    The QA prompt is explicit that "style nits are not failures", and a reviewer that
    fails a diff over naming or structure costs a whole retry cycle for nothing.
    """
    instructions = (
        'A reviewer failed a code change. Given the review text and the diff it '
        'reviewed (in state), does the review identify at least one genuine correctness '
        'problem -- behaviour that is wrong, an edge case that breaks, a requirement not '
        'met -- as opposed to only preferences about how the code is written?'
    )
    return Noul(
        instructions,
        true=(
            'At least one finding describes something that would produce a wrong result, '
            'a crash, data loss, a security hole, or a plain failure to do what the task '
            'asked. It would still be a bug if the code were formatted perfectly.'
        ),
        false=(
            'Every finding is about style, naming, structure, duplication, comments, '
            'idiom, or a preference for a different approach that would work no better. '
            'A reader could apply none of them and the code would still behave correctly.'
        ),
    )


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

# --- semantic wave coupling (Phase 4, item 4) --------------------------------------


def wave_coupling(first: str, second: str) -> dict:
    """Would running these two tasks concurrently produce an integration conflict?

    `compute_waves` bounds parallelism structurally: two tasks share a wave only when
    their file sets are disjoint. Disjointness cannot see the conflict where task A
    changes an interface that task B consumes -- both diffs are clean, both pass review
    against their own baseline, and the merge is broken. This asks about exactly that.
    """
    instructions = (
        f'Two Forge tasks are about to run at the same time, in separate worktrees '
        f'branched from the same commit, each reviewed only against its own baseline. '
        f'Task A: {first!r}. Task B: {second!r}. Their declared files do not overlap. '
        'Would running them concurrently still produce a broken result once both are '
        'merged?'
    )
    return Noul(
        instructions,
        true=(
            'One task changes something the other depends on -- a function signature, a '
            'return type, a schema, a config key, an interface, a shared constant -- so '
            'the second task is written against a version of the code that will no '
            'longer exist. Both diffs can be individually correct and the merge still '
            'broken.'
        ),
        false=(
            'The two tasks touch genuinely independent behaviour. Neither one changes '
            'anything the other reads, calls, or relies on, so the order they land in '
            'does not matter.'
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

# --- effort sizing and spec parsing (Phase 3, remaining items) ---------------------


def effort_level() -> dict:
    """How much thinking should the chosen model spend on this task?

    Ordered to match forge-dispatch.sh's CANON ladder (low..ultra), so the answer maps
    onto a real effort word rather than a number this file invented. forge-dispatch.sh
    already clamps a request above what a pairing offers, so a level this repo's models
    cannot reach degrades to their top tier instead of failing.

    This does not choose a MODEL -- the alias is whatever the user supplied for the tier.
    """
    instructions = (
        'A model has already been chosen for this task. Given the task and its context '
        '(in state), how much reasoning effort should it spend? Rate what the work '
        'requires, not how much code it produces.'
    )
    return Score(instructions, [
        'low -- mechanical and local; the answer is evident from reading the task.',
        'medium -- ordinary implementation work against a clear spec.',
        'high -- needs the approach worked out before any code is written.',
        'xhigh -- several interacting constraints have to be held at once.',
        'max -- subtle correctness the obvious implementation would get wrong.',
        'ultra -- the hardest class of change: concurrency, protocol or migration work '
        'where a plausible-looking answer is usually wrong.',
    ])


def spec_from_sentence(tier: str, aliases: list) -> dict:
    """Which registry alias does this sentence ask for, for this tier?

    Asked once per tier over the closed set of aliases in registry.tsv plus 'none'. The
    user still chose the models -- this only maps their words onto the aliases they
    already have, and the result is echoed back for confirmation rather than applied.
    """
    criteria = {
        alias: f'The sentence asks for {alias!r} for the {tier} tier.'
        for alias in aliases
    }
    criteria['none'] = (
        f'The sentence does not say which model should handle {tier} work. Pick this '
        'rather than guess: an unasked-for model spends the user\'s quota.'
    )
    return Choice(
        f'A user described how they want work routed. Which model alias, if any, are '
        f'they asking for on {tier}-difficulty tasks?', criteria)

def qa_effort() -> dict:
    """How much review effort does this diff warrant?

    Sizes the review; it can never skip one. Forge's premise is that an independent
    reviewer reads every diff, so the cheapest level here is still a real review.
    """
    instructions = (
        'A diff is about to be reviewed for correctness by an independent model that '
        'did not write it (the diff and the task are in state). How much reasoning '
        'effort does reviewing it well require?'
    )
    return Score(instructions, [
        'low -- short and mechanical; a careful read is enough to see whether it is right.',
        'medium -- ordinary logic to follow, with a few edge cases worth checking.',
        'high -- the correctness argument spans several places in the diff.',
        'xhigh -- interacting constraints, or behaviour whose failure mode is silent.',
        'max -- concurrency, protocol, migration or security surface where a '
        'plausible-looking diff is routinely wrong.',
    ])

