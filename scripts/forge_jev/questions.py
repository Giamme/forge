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
