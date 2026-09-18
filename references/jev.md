# Optional Jev advisory judgments

Jev is TypeSafe's "System One" model: it answers narrow, typed questions (a
yes/no probability, a single choice, an ordered score) over supplied state in
about 100ms, with calibrated probabilities. Many questions per request evaluate
in parallel, so asking a few extra costs little additional latency. Forge uses
Jev to *advise*; Forge keeps every existing decision and gate. Jev never
replaces model routing logic, test selection, QA acceptance or repository
verification — it only offers a probability alongside them.

## Privacy

Off by default, and this is the main reason why. Each enabled capability
transmits a narrow slice of state to an external API:

- **routing** — task prompts and candidate model identifiers
- **tests** — diff hunks and test names
- **gates** — diff hunks and file excerpts relevant to the gate
- **memory** — memory entry text being considered for curation

`.forge/` and `.git` are excluded from every payload, in every capability, with
no exception. The API key lives at
`${XDG_CONFIG_HOME:-$HOME/.config}/forge/jev.json`, mode `0600`. It is never
written to a run directory, a prompt, or a log.

## Setup

```sh
./forge jev setup                        # store an API key
./forge jev status                       # resolved config, enabled capabilities
./forge jev doctor [--live]              # config/key checks; --live also probes the API
./forge jev enable [--capability NAME]   # persist enabled (optionally scoped)
./forge jev disable [--capability NAME]
```

`TYPESAFE_API_KEY` in the environment overrides the stored key for that
invocation only. Keys come from `console.typesafe.ai`.

## Commands

User-facing:

```
forge jev setup                       # store a key, validate it, enable Jev
forge jev status | doctor [--live]    # what is on; whether it works
forge jev enable | disable [--capability routing|tests|gates|memory]
forge jev backtest --repo <path> [--capability tests|drift] [--execute]
forge jev calibrate --repo <path> [--write]
```

`score-plan`, `verify-discover` and `verify-triage` are also subcommands, but they are
called by `forge-parallel.sh` rather than by hand. They are documented here only so that
a line in a run log is traceable to the code that produced it.

## Gating and precedence

Four steps, implemented identically by `scripts/forge-jev-options.sh` (shell)
and `scripts/forge_jev/` (Python) — keep them in sync:

1. `FORGE_JEV=off` wins over everything: everything off, full stop. This is
   the hard kill switch.
2. Any `FORGE_JEV_<CAP>=on` acts as an allowlist — only those capabilities
   run. `--jev-tests` alone works without `--jev`.
3. Otherwise `FORGE_JEV=on` (from `--jev`) or the stored config's `enabled`
   flag turns it on, and a capability explicitly set `off` is subtracted.
4. Default: off.

Flags: `--jev` / `--no-jev` (mutually exclusive), `--jev-act`, `--jev-shadow`,
and `--jev-<cap>` / `--no-jev-<cap>` for `routing`, `tests`, `gates`, `memory`.
A run directory freezes its resolved selection in `jev-selection.json`, the
same way Fractal freezes `fractal-selection.json`, so resume and explicit
retry make the same decisions as the original run.

## Advisory by default

`--jev-act` is required before any judgment is auto-applied. Even then, Jev
may only choose among models the user already authorized for the run — it
never selects a model on its own, never overturns a `FORGE_VERDICT`, and never
marks anything verified. `--jev-shadow` records judgments and changes nothing
at all; use it to observe calibration before trusting `--jev-act`.

## Fail-open

Any error, timeout, or missing key is logged as a skip reason and the run
proceeds exactly as it would without Jev. A Jev network call can never fail a
run.

## Observability

- `<run-dir>/jev.jsonl` — one row per request: site, question ids, answers,
  confidences, latency, tokens, and whether the judgment was applied.
- `<run-dir>/jev.skip` — present when a request was skipped, with the reason.

## Backtest

`forge jev backtest` measures whether Jev's `test_relevance` rubric (see
`scripts/forge_jev/questions.py`) picks the right tests for a change, before
that judgment is ever wired into a live decision.

The label comes from git history, not from a model: a commit where a human
changed source and test files together is treated as ground truth — "these
are the tests a person judged relevant." Non-circular because nothing Jev (or
any model) produced enters the label. `scripts/forge_jev/corpus.py` extracts
these (change, tests-touched) pairs from `git log`.

Candidates for a commit are the test files present in its **parent** tree, not
at HEAD. Scoring against HEAD would ask about tests that did not exist yet:
measured on one repo, a commit 300 back had 94 test files in tree against 416
at HEAD, which would inflate the suite-reduction metric more than fourfold.

```sh
./forge jev backtest --repo PATH --limit 50            # dry estimate, no API call
./forge jev backtest --repo PATH --execute --max-requests 200
./forge jev backtest --repo PATH --execute --capability drift --json
```

`--execute` is required before backtest makes a single API call; without it,
the command only estimates the run (commit count, request count) and says so.
`--max-requests` caps spend by bounding how many Jev requests the run is
allowed to make, regardless of `--limit`. Results land under `--out` (default:
a directory under the run's own temp/output location) as raw judgments plus
the printed summary — precision/recall of the rubric's "true" answers against
the commit's actual test changes.

The summary sweeps thresholds and reports micro/macro recall, precision,
suite reduction, and `records_with_full_recall` — the fraction of commits where
*every* labeled test was selected. That last one is the number that decides
whether the judgment is safe to act on: a selector averaging 95% recall by
missing one test in every commit is useless. For `--capability drift` the
headline is instead precision at the warn threshold, because a warning that
fires on files nobody edits trains people to ignore it.

Three honesty caveats, printed with every run so they travel with the numbers:

- Commit messages are written **after** the work and often name the very files
  or behaviour that changed; a Forge task prompt is written before. Backtest
  scores are therefore an **upper bound** on live performance.
- A commit's test changes are a **lower bound** on the tests that were
  actually relevant — humans miss tests too. High recall against this label
  is necessary to trust the rubric, but not sufficient; it cannot prove the
  rubric would catch tests no human thought to touch. (This one does not apply
  to `--capability drift`, whose label is the commit's complete file set.)
- Co-change encodes **one team's testing habits and repo layout**, not a
  universal property of the change. A number measured on one repo does not
  transfer to another's thresholds without re-measuring there. Measured here:
  labeled tests are 1.6% of the candidate suite in one repo and 13.2% in
  another, which are not the same problem.

## Verification (the first live capability)

Two judgments inside `verify_result`, the decomposed runner's verification step.
Both are off unless the `tests` capability is enabled and a key is configured; with
Jev inactive the function behaves exactly as it did before.

**Finding a command.** Only when neither `--verify` nor `.forge/verify` exists — the
case that reports `UNVERIFIED` today even when QA passed. Code enumerates commands
that *demonstrably exist* in the repo (`package.json` scripts, Makefile targets,
tox/nox, runner scripts under `tests/`, `scripts/`, `bin/`, a configured pytest,
`go.mod`, `Cargo.toml`, `run:` steps in GitHub workflows, and `verify |` lines already
in `.forge/memory.md`); Jev picks one, or `none`. It selects, never generates, so it
cannot propose `make test` for a repo with no Makefile. A non-executable runner is
offered with its interpreter — forge's own `tests/check.sh` is mode 644 and its README
says `bash tests/check.sh`.

The chosen command is announced through `note` before it runs, recorded with its
confidence and evidence in `<out>/verification.jev.json`, and then flows through the
existing run/fingerprint/status logic unchanged. `UNVERIFIED` remains the outcome
whenever nothing is chosen: Jev may find a command, never declare something verified.

**Re-running a corroborated flake.** On a non-zero exit, Jev classifies the failure as
`real_regression`, `known_flake`, `environment` or `generated_drift`. The command is
re-run **once**, and only when all of these hold:

- the exit was non-zero *and* the tree did not change — a tree change is never a flake;
- the verdict is `known_flake` at or above the `flake_act` threshold (0.90 by default);
- a `trap` already recorded in `.forge/memory.md` shares a distinctive token with this
  failure's output.

That last condition is deterministic and computed in code, not asked of the model. A
model alone calling a failure a flake is how a real regression ships, and a trap noted
months ago must not excuse an unrelated failure today. If the re-run also fails the
status stays `FAIL` and the first log is preserved. `real_regression` is never re-run
at any confidence.

## Calibration (measured 2026-09-18)

First measurements against the live API. Two repos for test selection, 18 for verify
discovery. Every number below is from this machine's repos and does not transfer —
re-measure before trusting a threshold elsewhere.

### `tests_act` = 0.70 is supported by evidence, not a guess

Discovery was run over every git repo on this machine (18 with candidates). The pick
was compared against the command each repo's own README or `package.json` documents.

| | |
|---|---|
| Fires at 0.70 | 10 of 18, **every pick correct** |
| Lowest correct pick that fires | 0.73 (`ctrlrisk-tools-redigo`) |
| Highest *incorrect* pick | 0.55 (`spankai` → `cargo test`) |
| Margin around the threshold | **0.18** |

`spankai` is the one confirmed wrong pick: a Rust + TypeScript monorepo whose README
says `npm run check`, where Jev chose `cargo test` — a real test command that covers
only the Rust half. The gate blocked it. Lowering the threshold to 0.58 would add four
correct picks but leave only a 0.03 margin above that wrong one, so 0.70 stays.

Confidence tracks how ambiguous the candidate set is, not how likely the pick is to be
wrong: repos with 2 candidates land at 0.73–0.81, repos with 11–25 land at 0.58–0.61.
This is why the gate is the Choice confidence and **not** `runs_tests`, which sat at
0.79–0.98 for every repo including the wrong `cargo test` at 0.97. `runs_tests` answers
"is this a test command" — true of `cargo test` — and is nearly useless as a gate.

Repeating the identical request three times gives a spread of 0.02–0.08 and never
changes the pick, so these numbers are reproducible rather than one lucky sample.

### Test selection: no single threshold works across repos

`backtest --capability tests` over 25 commits of `ctrlrisk-tools-enrich` (40 requests)
and all 44 of `spankai` (44 requests), sweeping the per-file relevance probability:

| repo | highest threshold holding recall ≥ 0.95 | suite reduction there |
|---|---|---|
| `ctrlrisk-tools-enrich` | 0.20 | 46% |
| `spankai` | 0.45 | 36% |

The safe threshold differs by 2.25× between two repos, which settles the question of
whether one global default could serve: it cannot. Any future test-subsetting capability
must calibrate per repo and store the result, not ship a constant. Note also that no
answer ever exceeded 0.90 — the top of the probability range is unused by this rubric,
so a threshold set there silently disables the capability.

### A rejected change, recorded so it is not retried

Feeding each repo's documented commands (README, CONTRIBUTING, AGENTS.md) into
discovery state looked obviously right and **measured worse**. It fixed `spankai`'s pick
but cut repos firing at 0.70 from 10 to 3, and it destabilised the judgment: against a
baseline spread of 0.02–0.08, `ctrlrisk-intelligence` fell from a steady 0.81/0.81/0.83
on the correct command to 0.30 on a narrow migration check. A README lists many
commands and nothing marks which one is the gate, so the evidence is mostly noise.
Half the damage was a prompt artifact — naming the state key when the list was empty
cost 0.12–0.21 confidence on every repo that documents nothing, because "no candidate
matches" reads as evidence against rather than absence of evidence. Fixing that
recovered those repos exactly, and the doc-rich repos stayed erratic. Reverted.

## Routing (shadow-first)

Five `Score` rubrics per task, all in one request, evaluated in parallel. **This code,
not the model, turns them into a tier** — `forge_jev/routing.py` holds the weights, so
re-tuning them against accrued outcomes replays recorded scores instead of paying for
inference again.

| Rubric | Weight | Why |
|---|---|---|
| design judgment | 0.35 | `decompose.md` defines difficulty by this first |
| state & concurrency subtlety | 0.25 | that table names it as what makes a ten-line diff `high` |
| blast radius | 0.20 | how far a mistake reaches |
| spec clarity | 0.15 | is the approach given, or must it be invented |
| existing test coverage | **−0.05** | inverted: good coverage lowers the tier |

Coverage is weighted lightly on purpose. It does not reduce the judgment a task demands,
only what a mistake costs, and letting it dominate would send a genuinely hard change to
a cheap model because the area happens to be well tested.

The planner's own difficulty rating is **not** sent. Jev is a second opinion on the same
evidence; showing it the answer first buys an agreement rate, not a judgment.

**Escalation.** A composite within 0.05 of a tier boundary, or a confidence below 0.55,
escalates one tier and records why (`boundary` / `low-confidence`). Paying for a stronger
model on an ambiguous task is the cheap error. An escalated judgment is never acted on —
the escalation exists precisely because the composite was not trustworthy there.

### Three modes, and why acting is currently impossible

| Mode | What happens |
|---|---|
| `--jev-shadow` | Scores every task, writes `jev-routing.tsv` and `tasks/<id>/jev.json`, **changes nothing else** — not the table, not one dispatch |
| `--jev` (default) | Adds an advisory line per task; the `difficulty` column is untouched |
| `--jev-act` | Writes the tier into `difficulty` — **only** where `routing.may_act` allows |

`may_act` requires all of: a stored calibration for **this repo**, covering **this tier**,
plus an unescalated judgment at or above `routing_act` (0.85). The calibration file is
written only by `forge jev calibrate --write`, and only for tiers that reached the sample
bar (default N=30). No repo has one yet, so **`--jev-act` is inert for routing today** and
stays advisory — by design, not by omission.

Calibration is per repo because the measurements above show the same rubric behaving
differently across repos; a threshold earned in one project says nothing about another.

### What was measured

Six real tasks from this repo, hand-labelled first, scored live:

- **5 of 6 matched.** The sixth was a boundary escalation `medium`→`high`, the intended
  conservative direction.
- Confidence is high on easy work (0.89–0.93) and lower on hard work (0.67–0.76). That
  asymmetry is the useful one: Jev is confident exactly where routing *down* saves money,
  and defers to the human where getting it wrong is expensive.
- End to end, a spelling fix declared `high` was routed to the cheap pool at 0.864 while a
  concurrency redesign stayed on the expensive one.

Composite confidence is the **weight-weighted** mean, not the minimum. The minimum was
tried first and measured wrong: on a real task `blast_radius` returned 0.0 while
`design_judgment` (weight 0.35) was 0.94, and the minimum reported 0.0 for a composite
that was mostly settled. The weakest dimension is still reported separately as `weakest`.

Six hand-labelled tasks is **not** a calibration. It is a smoke test that the rubrics
point the right way. The real numbers come from shadow runs.

### Accruing the data

```
/forge "<goal>" --decompose-level medium --jev --jev-shadow    # normal work, nothing changes
forge jev calibrate --repo <repo>                              # reports, refuses below N=30/tier
forge jev calibrate --repo <repo> --write                      # only then may routing act
```

`calibrate` joins `jev-routing.tsv` against `.forge/ledger.tsv` on `(run_id, task)`, taking
the QA verdict as the outcome. It prints the shortfall per tier rather than a
confident-looking guess, and `--write` is a separate step because that file is the only
thing standing between `--jev-act` and Jev changing which model spends your quota.

### What the bar actually is, and why N=30

A tier earns the right to be acted on when the tasks Jev judged at or above `routing_act`
passed at a rate whose **95% Wilson lower bound** clears `calibrate_floor` (0.80). Not the
observed rate: 5 of 5 and 30 of 30 are both 1.0 observed, and 0.57 versus 0.89 bounded.
The sample-size requirement falls out of that arithmetic instead of being a second magic
constant.

The question is deliberately **absolute** — "if Jev routes these, does the work still
pass?" — and not "does the confident subset beat the tier's own average". The relative
version is unanswerable by construction, because the subset is contained in the tier;
when confidence clusters tightly, as the measured low-tier composites do at 0.89–0.93,
the subset *is* most of the tier and cannot out-perform it by a detectable margin.
Simulated over 3000 trials, the relative criterion accepted a genuinely reliable tier
less than 1% of the time at every sample size tried.

With the absolute criterion, simulation puts N=30 in a reasonable place:

| true pass rate of Jev-confident tasks | n=30 | n=40 | n=50 | n=75 |
|---|---|---|---|---|
| 99% (safe to route down) | 84% | **97%** | 99% | 100% |
| 95% (good) | 41% | 59% | **75%** | 93% |
| 80% (marginal — should refuse) | 1.3% | 1.7% | 1.6% | 2.1% |
| 70% (bad — should refuse) | 0.1% | 0.1% | 0.0% | 0.0% |

Read it as: 30 is enough when routing-down genuinely works, ~50 when it merely works
well, and no sample size lets a marginal tier through. A tier that stays below the floor
as N grows is not short of data — it is telling you Jev's cheap-tier judgments are not
safe to act on in that repo.

The swept "suggested threshold" still appears in the report, marked informational. It is
not used to decide anything: searching ten candidate cuts for the best-looking one is how
a threshold ends up backed by three observations.

## Pre-dispatch gates

Three plan-time warnings. All advisory, all fail-open, none can block a dispatch — a gate
that can stop a dispatch is a gate that will eventually stop a good one.

**Prompt adequacy** and **independent verifiability** ride inside the routing request.
They need exactly the state routing already sends, and questions in one request evaluate
in parallel, so they cost **no extra round trip**.

**`files` drift prediction** needs a different universe — every tracked file, not the task
text — so it is the one extra request Phase 4 adds, and only when the `gates` capability
is on. `decompose.md` calls the `files` column "a promise, not a prediction"; Forge
already detects a broken promise in `drift.txt`, but only after the wave has run. Asking
at plan time turns a post-mortem into a warning while the plan can still change.

### Each rubric has its own threshold, because they are not on one scale

This is the same trap `tests_act` set earlier, and it was measured rather than guessed.

| Gate | Threshold | Evidence |
|---|---|---|
| drift | `gate_warn` 0.60 | 50 commits × 2 repos: precision **1.000** and **0.934** |
| prompt adequacy | `prompt_warn` 0.30 | vague prompts clustered at **0.04**, specific ones ran **0.55–0.87** |
| independent verifiability | `verifiable_warn` 0.38 | task fragments **0.11–0.24**, self-contained tasks **0.51–0.72** |

Using the single `gate_warn` of 0.60 for all three — which is what shipped first — warned
on 5 of 9 and 5 of 11 perfectly good tasks in the hand-labelled set. It was caught by a
live plan where a precisely specified task tripped both text gates. Precision is the only
number that matters for a warning: one that fires on good work teaches people to ignore
every future warning.

Drift is the strongest of the three and the only one measured at corpus scale. Its
threshold sits comfortably clear of the noise on real repos (non-edited files scored
≤0.23 in a live check where the true file scored 0.65), but note that 0.65 is not a wide
margin — a borderline file can fall either side between runs.

The verifiability rubric was nearly discarded on a first reading that showed it failing
to separate vague prompts from clear ones. That was a bad label, not a bad rubric:
vagueness is not the same property as needing a sibling task to land first. Against the
right labels it separates with a 0.27 margin.

## Status

Phase 0 added the configuration surface. Phase 1a added `backtest`, which scores the
rubrics against git history offline and changes no Forge decision. Phase 2 made
verification the first capability wired to a live decision. Phase 3 adds routing, which
can advise today and cannot act anywhere until that repo has been calibrated — which is
also what Phase 1b's shadow accrual exists to make possible.

Per-task test subsetting is **not** implemented. Running tests inside a task worktree
has no safe window: artifacts created before the diff capture reach QA as if the dwarf
wrote them, and artifacts created after are caught by the fingerprint re-checks in
`do_task` and `merge_task`, which write `INVALIDATED` and block the merge. Doing it
properly needs a disposable export with the dependency environment rebuilt, whose cost
may exceed the saving — a trade that needs `backtest` numbers to settle.

Routing is wired but shadow-first: it can advise today and cannot act until a repo
has been calibrated. Three of Phase 4's six gates are wired (prompt adequacy,
independent verifiability, files drift). Semantic wave coupling and the two
post-review QA gates — finding triage and effort sizing — are not, and neither is
memory curation.

Verification is calibrated against real judgments (see above). Routing is not: its
`routing_act` of 0.85 rests on six hand-labelled tasks, which is a smoke test and not a
calibration. Observed composite confidences ran 0.54–0.93, so 0.85 currently admits only
the clearest cases — deliberately, but the number itself is unearned until shadow runs
produce outcomes. The discovery result above, that confidence tracks option-set
ambiguity rather than correctness, is the standing warning that a threshold picked by
intuition can be wrong in either direction.
