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
