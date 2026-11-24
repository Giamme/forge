# Decomposed runs: reviews and verdicts

How a decomposed task's review reaches a verdict, the opt-in review modes, and what the
threshold does with findings. Part of the decomposition reference; planning is in
[decompose.md](decompose.md).

## Contents

- [QA verdicts](#qa-verdicts)
- [Severity, thresholds and known issues](#severity-thresholds-and-known-issues)

## QA verdicts

Decompose-mode QA prompts require a final line:

```
FORGE_VERDICT: PASS   — no CONFIRMED finding at the blocking threshold (P2 or worse)
FORGE_VERDICT: FAIL   — at least one CONFIRMED finding at the blocking threshold (P2 or worse)
```

forge decides the verdict from the findings, at a blocking threshold: `P2` unless
`--qa-threshold` sets another. A CONFIRMED finding at the threshold or worse, or one with no
severity, blocks: a PASS that lists one becomes FAIL. A CONFIRMED finding below the threshold
never blocks: a FAIL made only of those is accepted, and either way they are recorded in
`known_issues.md`, which `forge issues` and the sweep pick up. PLAUSIBLE findings never block.
`--qa-threshold P3` (or `none`) makes every CONFIRMED finding block.

The runner accepts only a standalone verdict on the final nonempty line (the shared parser
also tolerates the dash gloss the prompt prints after each verdict, trailing whitespace and
CRLF). A missing verdict is `UNKNOWN` and is treated exactly
like a failure — excluded from integration and flagged. Merging a diff whose reviewer never
reached a conclusion would defeat the point of reviewing it, so the ambiguous case fails
safe rather than optimistically.

A failing task is excluded and its branch preserved; the rest of the run proceeds. There is
no automatic repair pass unless the run was started with `--retry-failed N`, consistent with
forge's rule that a dwarf → QA cycle does not loop on its own initiative.

The prompt contract (preamble, findings format, severity definitions and the verdict lines)
comes from one place, `scripts/forge-contract.py`, shared by solo, decomposed and the final
review; the implementer rules appended to every dwarf prompt come from the same module.
When the run verifies the repository (`--verify`, verification lanes or `.forge/verify`), those
rules tell the implementer never to run the whole suite, even when the task's acceptance
criteria name it, and to report it as left to forge: the suite runs once, after the merge.
Without verification, the implementer runs the whole suite only when the task asks for it.

### Fail-first evidence (opt-in)

`plan --fail-first` (and `forge-solo.sh --fail-first`) checks that the tests a change adds
or edits can fail. Before each review, forge exports the reviewed commit and, separately,
the task's base plus only the change's test-path files (tests, their fixtures and helpers),
then runs `.forge/verify-subset` (a per-file test command that contains `{files}`, such as
`pytest -q {files}` or `npm test -- {files}`; it is a separate file from `.forge/verify` because
there is no general way to restrict an arbitrary command to a file list) once per new or
edited test file in each. Every file gets one result in
`tasks/<id>/failfirst.json` (solo: `<run>/failfirst.json`):

- `proven`: passes with the change, fails without it;
- `passes_without_change`: passes both ways, so it does not prove the change;
- `inconclusive`: fails even with the change (a broken test or a missing dependency);
- `error`: could not run (export failure, or `FORGE_FAIL_FIRST_TIMEOUT`, default 600 s, per run).

The reviewer's prompt lists the results and asks for a `[P2][CONFIRMED]` finding for each
test that passes without the change, unless the reviewer explains why it need not fail
(a refactor kept green). It is advice: forge never changes a status or verdict from it, and
a P2 only blocks through the reviewer and the usual threshold gate (`P2` by default, see [QA verdicts](#qa-verdicts)). The exports are
throwaway directories outside the review snapshot, so no fingerprint changes. forge-ui
shows the evidence under the task's review findings, and each check is recorded in the
run dataset as a `fail_first` event with the JSON attached. Without
`.forge/verify-subset` the check records `skipped` and the prompt says nothing.
`plan --no-fail-first` turns it off again.

### Pre-review tests (opt-in)

`plan --pre-review-tests` runs **only the tests an attempt affects** before paying for its
review, never the whole suite (that stays the run's end-of-run verification). After an attempt is
committed, forge exports the reviewed commit and runs `.forge/verify-subset` on a selection:

1. the test files the change adds or edits;
2. for each changed source file, the tests from the first source that answers: `.forge/test-map`,
   then the naming convention (`foo.py` → `test_foo.py`, `foo_test.py`, `foo.test.js`, ...), then
   a reverse lookup (test files that name the changed module);
3. with `{changed}` in the command instead of `{files}`, the changed paths themselves, for a runner
   that finds the related tests on its own (`npx jest --findRelatedTests {changed}`).

`.forge/test-map` is the reliable source; one rule per line, `#` comments, globs where `*` also
matches `/`, and `-` for "no tests":

```
scripts/forge-issues.py   tests/test_issues.py tests/test_sweep.py
ui/forge_ui/*.py          ui/tests/test_*.py
docs/*                    -
```

A change to shared test infrastructure (a conftest, a test helper, a lockfile, a build file),
more than 20 selected files (`FORGE_PRE_REVIEW_MAX_FILES`) or a run past 120 s
(`FORGE_PRE_REVIEW_TIMEOUT`) makes the check `skipped` as too broad. A failing run is rerun file
by file (a file that passes alone is `flaky`), and each file that still fails is run on the task
base: one that fails there too is `pre-existing` and does not count against the attempt.

- **Red** (`failed`): the attempt is `FAIL` without a review. Its retry findings are the failing
  tests and the end of their output, labelled as forge's (`tasks/<id>/prereview.md`). It counts
  toward `--retry-failed` and `--escalate-after` like a rejected attempt.
- **Green**, `flaky` or `pre-existing`: the review runs, and the reviewer's prompt says what forge
  ran and found.
- **Skipped or broken** (no `.forge/verify-subset`, nothing selected, too broad, a command error):
  the review runs exactly as without the option. The check never blocks on its own failure.

The implementer is told forge will run them, so it need not repeat them exhaustively. Checks run
in throwaway exports, so neither the task worktree nor any fingerprint changes; at most 2 run at
once across the plan's tasks (`plan --pre-review-parallel N`, 1-64). A QA-only resume reuses the
check of the commit it re-reviews. `tasks/<id>/prereview.json` records the selection, every run
and the outcome; forge-ui shows it under the task's review findings, and the dataset records a
`pre_review` event. `forge control set <plan> pre-review-tests on|off` and `pre-review-parallel N`
change both while a run works; `plan --no-pre-review-tests` turns it off.

### Implementer session (opt-in)

`plan --implementer-session` makes a retry **continue the implementer's own session** instead of
starting one that re-reads the same files: the dwarf keeps what it learned, and the retry's prompt
(the task, the findings, the rules) arrives as the next turn, after a short note that its work is
as it left it. It applies to claude and openclaude. codex's `exec resume` takes no sandbox flag, so
a codex implementer could be resumed only by a config override nobody verifies, and it stays fresh.

A retry continues only when all of these hold; anything else is a fresh session, as without the
option:

- it carries findings from a review, or from pre-review tests, of a commit this session made;
- the worktree is still exactly that commit: no edits since (a timed-out or interrupted attempt
  leaves some), nothing committed on top;
- the dwarf resolves to the same harness, model and effort, so an escalation (`--escalate-after`,
  a pin, `retry --dwarf`) starts fresh.

A continue the harness refuses or that fails at once (dispatch exit **9**, nothing changed in the
worktree) is followed by a fresh dispatch of the same prompt, which decides the attempt. One that
fails after it already changed files is a failed implementer (`ERROR`), as a fresh one would be:
running another on top would pay for the attempt twice. One row per attempt goes to
`tasks/<id>/implementer_sessions.tsv`: row, attempt, `continued`, `fresh` or `failed`, the reason
(`same-spec`, `first-attempt`, `no-findings`, `work-changed`, `no-session`, `invalid-session`,
`spec-changed`, `harness-unsupported`, `continue-unsupported`, `continue-failed`; `fractal` only in rows from before 0.23) and
the session id it considered. The dataset tags a continued dwarf dispatch `session_continue`, and
`forge dataset status` counts implementer continues apart from reviewer ones.
`plan --no-implementer-session` turns it off; `split` and `combine` carry it (combine only when
every source has it).

### Shadow reviews (opt-in)

`plan --shadow-qa <spec>[,<spec>...] [--shadow-rate R]` sends a sampled share of
reviewed attempts (R, default 0.1) to a second, independent reviewer. Its verdict is
recorded in the run dataset and never read by the run. It exists to measure how often two
reviewers agree on one diff, which is the noise in every review-based label. The approval
table shows the reviewers, the rate and the expected extra reviews, and `--shadow-qa none`
turns shadow reviews off again.

- **Which reviewer:** the first listed spec whose model family (Claude, GPT, Gemini, ...)
  differs from the task's reviewer, so the two do not share one blind spot. If none
  differs, the first listed spec that does not resolve to the reviewer's own harness, model
  and effort: the same model at another effort (`same_model`) or another model of the
  family. Only when every listed spec is identical to the reviewer does it run as a
  `control`, two reviews with one setting, which measures noise and never counts as an
  effort comparison (a reviewer pool can hand a task the shadow's own spec). Forge only
  ever uses the specs you list. A spec that cannot run here (`doctor` fails) is dropped
  with a note and never fails the run.
- **Measuring an effort:** list only specs of the reviewers' own model at another effort,
  for example `--shadow-qa sol:medium` beside `sol:high` reviewers, with a rate high enough
  for at least 30 pairs per comparison. Each pair is one more review, so agree on its cost first.
- **Which attempts:** a deterministic sample per (run, task, attempt), so a QA-only
  resume decides the same way.
- **Same evidence, separate everything else.** The shadow reviews a byte-identical copy of
  the primary's prompt, in its own snapshot, from its own dir
  (`<plan>/shadow/<task>/<attempt>-<k>/`). Nothing reads that dir. It writes no status,
  memory, ledger, gate or retry findings, and a reviewer that edits its snapshot is
  recorded as `invalid`.
- **Bounded:** it runs beside the primary, and the task waits for it before writing any
  status. Once the primary is done, the shadow gets as long again as the primary took
  (at least 5 minutes), then it is stopped. It dies with its task, including on Ctrl-C of
  a manual `retry`.
- **Scope:** parallel mode only.

`./forge dataset status` reports the agreement rate, overall and per model pair, and
`./forge dataset pairs` splits it by what differs between the two reviewers: effort within one
model, another model of the family, another family, and controls. For each pair of settings it
gives verdict agreement with a 95% interval, the blocking findings only one side found, and the
token and time ratio (see [the dataset reference](dataset.md#paired-reviews)). The export
adds `shadow` and `consensus` to the matching review cases' metadata.

## Severity, thresholds and known issues

QA labels each finding `- [P0..P3][CONFIRMED|PLAUSIBLE] file:line — what breaks (input)`:
P0 data loss, security hole, crash or wrong result on the main path; P1 wrong behaviour on
realistic input or a broken existing contract; P2 edge-case bug, unusual-timing race, missing
hardening; P3 minor. Severity is impact; CONFIRMED/PLAUSIBLE is confidence.

`--qa-threshold Pn` on `plan`, `run`, `retry` or `review` (remembered in `<plan>/qa_threshold`; `none`
means P3, `default` removes the setting) changes the default `P2`, the **least severe level that
still blocks** (see [QA verdicts](#qa-verdicts)). `tasks/<id>/qa_threshold` overrides it per task (a
per-task `none` means P3, so it exempts the task from a looser plan threshold; `forge control set ... qa-threshold`
writes both and takes effect at the next attempt) —
the way to give a dev-tooling or investigation task a looser bar than production code,
which otherwise gets a new finding in a new corner of a non-default option every round and
only a human decision ends it. The orchestrator decides *deterministically* (`forge-contract.py gate`),
and does not take the reviewer's word in either direction: a `FAIL` becomes `PASS` with known
issues only when there is at least one CONFIRMED finding, every CONFIRMED finding carries a
`P0..P3` label, and all of them are strictly milder than the threshold; a `PASS` that lists a
blocking finding becomes `FAIL`. Anything unlabelled, any blocking finding, or a `FAIL` with no
CONFIRMED finding stays a failure. The task's status is then the literal `PASS`; `known_issues.md`,
`qa.gate` and the summary say what was tolerated.

### The finding sweep (opt-in)

Tolerated findings are recorded, not fixed. `run --sweep Pn` fixes them inside the run instead: once
every task has merged, and before the combined verification, the scheduler runs `forge-sweep.py plan`.
The sweep is once per finding, not once per plan. Round 1 is `<plan>/sweep/plan.json`; round N (N ≥ 2)
is `<plan>/sweep/rounds/<N>/plan.json`. A finding an earlier round attempted is not swept again, whatever
that attempt's outcome. The first round is planned once every task that is not a fix task has merged.
A later round starts when those tasks have merged, every fix task of the previous round is `MERGED`,
`FAIL`, `NEEDS_DECISION` or `INFRA`, and new eligible findings exist. At most one round starts per
scheduler pass. A later `plan` with nothing new writes and prints nothing. The scheduler records that
check in `<plan>/sweep/nothing.json`, and the run's summary prints
`sweep: nothing new to sweep (N finding(s) already swept or attempted, M left for forge issues)`.
It collects the open findings at `Pn` or worse (the ones the threshold tolerated, plus
PLAUSIBLE ones with `--sweep-plausible`), with `forge issues`' own collector, and leaves out:

- findings a person accepted with `accept` or kept with `adopt --approved` (listed as `accepted`), and
  findings of fix tasks (what a sweep review finds goes to `forge issues`, never into another sweep);
- dismissed ones (`forge issues --dismiss`), ones the finding record marks `fixed`, `filed` or
  `duplicate`, and ones an earlier round already attempted;
- ones with no file, and ones whose file is gone;
- ones whose line, as the reviewer saw it, a later merge changed (`changed`, naming those merges).

Every finding left out is listed in the plan with its reason. The rest become fix tasks, one per file,
worst first, at most 8 findings each and `--sweep-max N` (default 5) tasks. Each fix task is a row
`sweep-<n>` in `tasks.tsv` with `prompt.md` and `sweep.json`. Its implementer is the plan's low tier
(the first spec of `--dwarf-low`, else of `--dwarf`; checked at `run` start), else the implementer of
the task the worst finding came from. Its reviewer is that task's reviewer. What an adopted range's review
tolerated is swept too: its fix task's reviewer is the adoption's, and below the low tier its implementer
is the run's first task's. The plan (groups, findings,
dispatches) is printed and kept next to that round's `plan.json`, as `plan.md`. Round 1 lives in
`<plan>/sweep/`; each later round lives in `<plan>/sweep/rounds/<N>/`.

Fix tasks share the run's scheduling, controls and `--retry-failed` budget. Their review is narrow
(`forge-contract.py qa --kind sweep`): one `S<n>: FIXED` or `S<n>: NOT FIXED` line per finding, plus
regressions as findings. It passes when at least one finding is fixed and there is no blocking
regression. When a fix task merges, its review's lines go to `tasks/<id>/sweep.outcome.json`, and
`forge issues` leaves out what it fixed (`swept` in `--json`). Each finding that review marked fixed
also gets a `fixed` record, actor `sweep`, reason `sweep <task>`, and the merge commit when
`<plan>/accepted.integration` is 7 to 40 lowercase hex characters. A fix task that fails leaves its
findings for `forge issues` and does not fail the run. The run's summary says what each round fixed,
and the whole-run review that follows reads the sweep's changes like any other task's.

`run --sweep-ahead` (kept in `<plan>/sweep_ahead`; `--no-sweep-ahead` turns it off) is the cheaper
first step. A task dispatched after others merged is told the open findings in files it owns (at
most 3, picked once in `tasks/<id>/sweep.ahead.json`), to fix only on its way, and its reviewer is
told the same. It adds no dispatch. A later sweep counts the ones whose line changed as `changed`.
