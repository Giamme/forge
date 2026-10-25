# Run dataset

Every forge run records its inputs and outcomes in one global store outside every repo,
so a decision model (Jev, Laya or any other) can later be trained or calibrated on what
forge actually did. Run and plan dirs hold nearly all of this, but they live in `/tmp`
and scratchpads and get deleted. `.forge/ledger.tsv` survives, but it keeps only spec
strings and verdicts. It has no inputs, no resolved models, no attempt numbers and no
tokens.

```
${FORGE_DATA_HOME:-${XDG_DATA_HOME:-~/.local/share}/forge}/dataset/
  dataset.sqlite        events (rid-keyed, append-only) and repos
  blobs/<aa>/<sha>.gz   prompts, diffs, messages, logs: capped, gzipped, deduplicated
  errors.log            capture failures
```

An empty or relative `XDG_DATA_HOME` counts as unset, as the XDG spec says.

Capture is on by default and costs a few milliseconds per execution. `FORGE_DATASET=off`
turns it off for one invocation.

## Raw events, not training examples

The store holds **what happened, when it happened**: the state each decision saw and the
outcomes that followed. It never holds (state, question, label) rows. Those are derived
per decision by an exporter, so a question nobody has asked yet can still be answered
from old runs without re-running anything. Labels come from folding a run's events in
time order: an attempt that PASSed, then a merge that CONFLICTed, then a human `accept`.

| kind | one per | what it holds |
|---|---|---|
| `start` | execution | a receipt written by `begin()`. A start with no matching end is an execution that was killed or is still running |
| `dispatch` | model call | role, alias, harness, **resolved** model, effort after clamping, timeout and its source, delivery (custom or native review), phase timings, exit, timeout, infra class, tokens, parsed verdict and findings, `FORGE_LEARNING` lines. Blobs: the delivered prompt and the final message |
| `attempt` | task attempt or solo run | status, attempt number (with refund and resume flags), trigger (`initial`, `auto`, `human`), dwarf and qa spec, declared vs changed files, numstat, drift, guard notes, gate line, patch key, the files this execution wrote and the ones it only carried. Blobs: the diff, retry findings, memory slices, capsule, Jev task files |
| `verification` / `verify_run` | verify, and each command execution | command and its source (flag, `.forge/verify`, Jev), status, flaky, Jev triage. One `verify_run` per execution, each with its own log |
| `plan` | `plan` | goal, original request, decompose level, exploratory routing decisions (`tasks[id].route`, with their odds), author (orchestrator or planner), per-task declared vs executed difficulty and who set it, pools, waves, Jev routing and coupling. Blobs: both task tables, prompts, approaches, excerpts of the declared files at plan time, the file inventory, `memory.md` |
| `run_end` | `run` | results per task, the accepted integration commit, `memory.md` |
| `invocation` | user-facing `forge-parallel.sh` subcommand | argv, exit, duration, plus final review, accept, integrate and split/combine payloads |
| `event` | point event | `merge` (MERGED, CONFLICT with the files, INVALIDATED and why), `integrate_advanced`, `integrate_conflict`, `combine_conflict`, `sched_terminal`, `subset_run` (every outcome), `memory_record`, `memory_prune` |
| `event` `escalation` | retry that changes the model | the model a retry escalated from and to, `after_attempt`, and `source` (`policy`, `retry` for an explicit `--dwarf`, or `pin`), recorded at the rewrite, before the retried attempt begins |
| `event` `control` | `forge control` change | a human's live change: `at`, `actor` (`cli`/`ui`), `action` (`set`, `pin`, `unpin`), `key`, `value`, `previous`; task events carry the task id. Also read back from `control.log` by `import` |
| `event` `run_delete_started` / `run_deleted` | `forge runs rm` | written strictly around a run's deletion: `path`, `run_id`, `archive`, and for `run_deleted` the bytes freed |
| `event` `shadow_review` | shadow review | how an opt-in second review ended (`done`, `invalid`, `overtime`, `orphaned`, ...), with the reviewed commit and both specs. Its dispatch is recorded with role `qa_shadow` |
| `jev` | Jev request | site, answers, usage, latency, mode. Blob: the **full request** (state and questions). Its original sha is `jev.jsonl`'s `key` |
| `fractal_step` / `fractal_node` | Fractal step, node state | decomposition decision, iteration, resolved model, cost, tokens, candidate diff |
| `ledger` | ledger row | imported `.forge/ledger.tsv` rows |
| `forecast` | forecast shown by `run` | the plan forecast printed before dispatch (see [plan forecast](#plan-forecast)), once per run and content. Also in the plan as `forecast.json` |
| `outcome` | integrated task, per horizon | written by `forge dataset outcomes`, never by a run: how many of the lines a task added survive N days after `integrate`, and what reverted it |

Each blob reference carries `sha` (the stored bytes), `original_sha`, `n` (the original
size), `kept`, and `cut`. A cut keeps the head and the tail, because a QA prompt ends with
the diff and a log ends with the failure. Caps: prompt 1 MiB, diff 2 MiB, message 256 KiB,
log 272 KiB. Transcripts (`*.log`) and `.cmd` (which some harnesses use to carry the
prompt in argv) are never stored. A harness without structured output has its final
message copied from the whole log, so for those harnesses only the tail is kept, flagged
`final_source: log_copy`.

## How it is captured

All capture goes through a handful of hooks. Every one of them is fail-open: errors go
to `errors.log`, nothing is printed, and nothing changes an exit code.

- **`forge-runtime.py begin()/finish()`.** Every dispatch, task attempt, solo run,
  verification and parallel run already passes through these. `finish()` captures from a
  `finally:` block, so an extraction error cannot lose the record. This is the only
  hook that sees every exit path: an empty diff, INVALIDATED, a timeout, an infra stop
  or a `die`.
- **Provenance travels with the execution.** `begin()` writes
  `attempts/<role>-<uuid>/dataset.json` with the run id, task, starting attempt counter,
  trigger and a signature (inode, mtime, size) of every task file. `finish()` credits a
  file to this execution only when its signature changed. A dwarf that fails returns
  before `changes.diff` is refreshed, and without this check it would inherit the
  previous attempt's diff and verdict.
- **Point events** (`forge_dataset_event`) record facts that disappear before any EXIT
  trap could read them: a merge that gets aborted, a combine that rolls back, a user
  branch that moves just before a cleanup that may still fail.
- **An EXIT trap in `forge-parallel.sh`** records every user-facing subcommand, `die`
  included.
- **`forge_jev/client.py`** keeps the canonical request bytes of every live Jev call,
  including calls made without a run dir. It skips fixture replays, the setup probe and
  backtests.

Run identity is a uuid in `<plan-or-run-dir>/dataset.uid`, created once (`O_EXCL`) by
`plan` or by the first `begin()`. Split and combine copy attempt dirs unchanged, so a
copied execution keeps its original identity and collapses to one event by rid.

## Which forge produced an event

Outcomes depend on the prompts, contracts and registry forge ran with, and the skill is
often a live dev checkout that changes between releases. So every event carries four
provenance columns:

| column | what it identifies |
|---|---|
| `forge_version` | the release in `VERSION` |
| `registry_sha` | the effective `registry.tsv` (or `FORGE_REGISTRY`) |
| `code_sha` | the content of every file that shapes a run: `scripts/`, `references/`, `SKILL.md`, `VERSION`, `forge` and the effective registry. Two runs share it only if they ran the same code, uncommitted edits included |
| `forge_rev` | the skill checkout's `HEAD` (`git rev-parse`, read-only), or null for a copied skill dir |

An execution is stamped with what `begin()` saw, because its prompt was built then.
`finish()` runs in another process; if the code changed in between, the event carries
`code_changed: true` and can be left out of any per-regime analysis.

An imported run was produced by whatever forge ran it, so the importer stamps nothing,
unless a live `begin()` left its provenance in the attempt dir. Rows imported before
schema 2 carry the importer's version by mistake, so readers treat `forge_version` and
`registry_sha` on `src = 'import'` rows without a `code_sha` as unknown. The exporter
gives those a `forge_rev_estimate`: the last forge commit before the run started, which is a
guess and is labeled as one. Schema 2 added `code_sha` and `forge_rev`. The first
capture after an upgrade adds them in one `BEGIN IMMEDIATE` transaction, and an older
forge still running keeps writing.

## Commands

```sh
./forge dataset status [-v] [--json]    # where, how much, and what looks incomplete
./forge dataset dump [--kind K] [--run R] [--task T] [--since UNIX]   # events as JSON lines
./forge dataset blob <sha>              # print one stored blob
./forge dataset import <dir|archive|legacy-dir>... [--registry] [--fractal] [--ledger REPO] [--dry-run]
./forge dataset export [--slice S]... [--limit N] [--seed N] [--out DIR]   # labeled decision cases
./forge dataset outcomes [--horizon-days 14] [--repo PATH] [--dry-run]     # what integrated work became
./forge dataset routing [--repo PATH] [--since DAYS] [--json]   # pass rate and runtime per tier and model
./forge dataset reviewers [--horizon-days 14] [--repo PATH] [--json]   # how each reviewer's PASSed work held up
./forge dataset forecast <plan> [--repo PATH] [--json]   # likely cost and first-try PASS rate per task
```

`status` runs a different completeness check for each mode, because each mode
legitimately lacks different things. A solo run has no separate verification, and a
final review's QA has no task attempt. The checks are:

- executions that started but never ended
- task QA dispatches with no task attempt
- parallel PASSes with no merge outcome in a finished run
- parallel runs with no end
- blob references with no stored blob

## Exporting labeled decisions

`export` folds the events into cases in the System One wire format (one JSON object per
line with `id`, `state`, `questions` and `expected`), the format decision-model
benchmarks and fine-tuning recipes take. Each case is a decision Jev makes in forge,
with its state built the way `forge_jev` builds it for that site and its label taken
from what happened afterwards:

| slice | question | label |
|---|---|---|
| `routing` | will the first attempt at this task pass review? (state adds the dwarf and reviewer models) | the final status of the first real attempt (see below) |
| `routing` | which difficulty tier is this task? | the tier the orchestrator or planner declared. This is agreement, not an outcome |
| `retry` | will this retry pass, given the review findings it was handed? (state adds the attempt number and the previous review) | the final status of that attempt (see below) |
| `review` | will the independent reviewer pass this diff? (state names the reviewer's model, effort and harness) | the QA verdict. A diff whose reviews disagreed is dropped |
| `review` | will most of the lines this diff adds stay in the code, unrewritten and unreverted? | from `outcomes`: at least 80% of the added lines still blamed to the task's commits at the horizon, and no revert. Only the review of the diff that landed gets it |
| `review` | what is the most severe problem the reviewer will find? (`none`, `P3` to `P0`, the contract's scale) | the worst CONFIRMED finding, only when every CONFIRMED finding carries a severity and none is UNSTATED. A review whose findings cannot be fully read gets no label rather than a wrong `none` |
| `drift` | is this undeclared file likely to need editing? | whether the passing attempt changed it. Negatives are undeclared files near the task, plus random ones |
| `citation` | do the places this review cites appear in this diff? | the review paired with its own diff, or with another task's diff from the same repo that shares no file |
| `memory` | is this recorded fact worth including for this task? | the fact's anchor is a file the task edited (true), or lies in an area it never touched (false). Anything in between is left out |

The reviewer is part of the review state because strictness differs by reviewer: the
same kind of diff passes far more often under one reviewer than another, so a label
means little without knowing who gave it. Routing and retry states also carry a `size`
block (`prompt_chars`, `declared_files`, `plan_tasks`, `deps`, `decompose_level`, from
`forge_jev/features.py`), since task size moves the first-pass rate more than anything
else in the history.

An attempt is labeled by the last execution that carries its number, because a QA-only
rerun keeps the number. Refunded and infra-stopped executions spent no attempt. PASS is
true and FAIL is false. TIMEOUT is false only when the implementer's own dispatch ran out
of time. Everything else says nothing about the implementer and is left out, counted
under `censored` in `manifest.json`:
- a reviewer's TIMEOUT or ERROR
- a harness crash
- forge's own failures between dispatches
- UNKNOWN, INVALIDATED, INTERRUPTED

A later success is never labeled as the first attempt's.

Decisions with no outcome to label them are left out rather than labeled by opinion.
These are QA effort sizing (recorded until 2026-10-17), the five routing rubrics, failure triage, and coupling, since
no merge has conflicted yet.

Routing cases of tasks routed with `plan --explore` add `route` to their metadata: the
candidates, the pool's own pick, the choice, its propensity, and `followed`, which says
whether attempt 1 really ran the chosen spec (an approval-gate edit or `retry --dwarf` can
replace it). `assignments.jsonl` lists every such decision with its first attempt's outcome
or censoring reason, including tasks no case was exported for. It is the randomized cohort,
free of the export's own filters.

Review cases whose attempt also had a shadow review (`plan --shadow-qa`) add `shadow` (its
verdict, reviewer and worst finding) and `consensus` to their metadata. Only a shadow that
finished on an untouched snapshot and reviewed the same commit counts. The primary
labels never change, because `qa_shadow` dispatches are not reviews of record.
`./forge dataset status` prints the agreement rate, overall and per model pair.

`meta.jsonl` records each case's slice, run, repo and label source. It also holds hosted
Jev's own answers where forge recorded them, as baselines that are never part of a case.
Routing and retry cases add:
- `decision_provenance` (the plan's) and `outcome_provenance` (the attempt's)
- `code_changed`
- the plan `options`
- `started`

Routing cases also add how the task went:
- `attempts_observed`
- `raw_verdict`
- `gate_pass`, a reviewer FAIL the severity gate accepted
- `first_raw_pass_attempt`
- `human_accept`
- `stop_reason`: `passed`, `accepted`, `noretry`, `interrupted` or `no_pass_observed`

A final FAIL is not proof a task cannot pass: the retry budget ended the observation.
The output defaults to `<data>/exports/forge-decisions/` (`0700`, files `0600`). It holds
prompts, diffs and reviews from your repos, so keep it out of public repos and pass it
only to local models. Builds are deterministic for a given store and `--seed`.

## Routing

`forge dataset routing` answers "which model should this tier use?" from the store, in two
tables kept apart because they answer different questions.

**Observational.** Every task's first attempt, by declared tier and the model and effort that
ran it (resolved, so `sol` and `gpt-6.1-sol:medium:codex` share a row):
- first-pass rate with a Wilson 95% interval
- the share that passed review at some attempt, or that a human accepted
- unfinished tasks
- attempts per task
- the implementer's median runtime

Routing chose who ran what, so a model sent the hard tasks looks worse here than it is.
Fractal runs are left out, since their dispatches have identities of their own.

**Randomized.** The `plan --explore` draws, one row per (tier, pool pick, neighbour) pair.
Round-robin pools make the pair differ between rows, and a model has no chance at all
outside its own pairs, so pairs are never pooled. For each arm:
- assigned tasks
- first-pass as a Hajek (self-normalized inverse-propensity) mean
- Kish's effective n
- attrition: tasks not run as assigned, and tasks with no outcome
- runtime

Every task stays in the arm it was *assigned* (intent to treat), whatever the approval gate
or a retry did afterwards. A task is bound to the last draw recorded before its first attempt
started, so a later re-plan never relabels it. Draws from before the two-candidate rule
(a pick among every spec of the plan) have no pair and are counted, not compared.

**The decision rule** is fixed in advance and only advises. A pair is judged once, at the
first of 30, 60 or 120 neighbour assignments it reaches, on exactly the assignments up to
that point. The interval is a run-cluster bootstrap, shown only with at least 10 neighbour
assignments across 3 runs.
- For a **low** pair the neighbour is the stronger model: switching is advised only when the
  interval is above 0.
- For **medium** and **high** pairs it is the cheaper one: it is called non-inferior when the
  interval's lower end is above -0.10 and its runtime is lower.

At the default `--explore 0.2` a row changes model 10% of the time, so a pair needs weeks of
normal use to reach its first window.

## Outcomes after merge

Every other label comes from a reviewer. `forge dataset outcomes` adds one that does not,
read from the repo's history after the fact. `integrate` merges the run branch with
`--no-ff` and fast-forwards your branch to that merge, never squashing, so each task's
attempt commits land as they are and `git blame` still attributes a task's lines to them.
For every integration, and every task it brought in, the harvester records:

- **Survival:** of the text lines the task added (`base_ref..reviewed_commit`), how many
  are still attributed to the task's commits at the horizon, per file. A deleted file
  counts as none surviving. Binary files are skipped, and a task that only deleted lines
  is `no_additions`.
- **Reverts:** later commits whose message says `This reverts commit <sha>` for one of the
  task's commits, its merge or the integration.
- **The horizon:** the last commit on your branch's first-parent chain, after the
  integration, dated before `integrate` + N days. The branch is resolved once, and the
  chain is walked by position, so commit dates out of order cannot skip a commit.

Two cases leave a task without a survival count:
- **Pending:** the horizon has not been reached and no later commit has passed it yet.
  Nothing is written, and the next harvest tries again.
- **Lost:** a rebase, squash or reset took the integration off the branch. This means
  unobservable, not failed.

The rid names the integration, the reviewed commit and the horizon, so harvesting again
adds nothing, and two horizons are two outcomes. Survival describes the diff that landed,
with every retry in it, not the first model the task was routed to. That is why the
exporter attaches it to that diff's review.

Each verification event also records the commit it verified (`verification.commit`, named
before any early return), so a FAIL or UNVERIFIED result can be joined to its code too.

## Reviewer report card

`forge dataset reviewers` joins the survival outcomes above to the reviews that let the work
through. For each reviewed commit with an outcome at the horizon (`--horizon-days`, default 14,
only outcomes recorded at exactly that horizon), every primary reviewer (role `qa`) that gave
that attempt a PASS is credited with one landed outcome. A row is keyed by the reviewer's alias,
or its model when no alias was recorded, as the forge-ui Models view keys it. A landed outcome
**did not hold** when a later commit reverted it, or when under 80% of its added lines were kept
(rewritten): the export's `survives` rule.

- The rate of work that did not hold has a Wilson 95% interval, shown only from 10 landed
  outcomes on. Below that the row reads `not enough data (n/10)`, and the JSON has
  `enough: false` with `rate` and `ci` null.
- A shadow review's PASS decided nothing, so it is not credited. Work that landed after a FAIL
  (the severity gate or a human accepted it) and work with no recorded review are counted as
  unattributed. `lost` and `no_additions` outcomes are skipped.
- Survival is only followed for parallel integrations, so solo runs and hand merges are not here.
- It only advises: nothing that routes a task or builds a prompt reads it.

forge-ui shows the rate as the Reviewers table's **Did not hold** column, through
`GET /api/models/reviewers`, which runs this command with `--json`.

## Plan forecast

`forge dataset forecast <plan>` reads the plan's `tasks.tsv` (the declared tier from
`tasks.declared.tsv` when there is one) and, for each task not yet MERGED, looks up past tasks
that ran at the same tier on the same resolved model. The effort must match too when the task's
spec names one. It uses the same task history as `forge dataset routing`, without the plan's own
run.

- Tokens are input plus output (cached input is not added again), summed over every implement
  and review dispatch of the task, retries included. Time is the sum of those dispatches'
  durations. Both show median and p90 (nearest rank).
- The first-try PASS rate has a Wilson 95% interval. Tasks below 50% are flagged as likely to
  need a retry. Under 5 past tasks the row reads `not enough history (n/5)` and the JSON has
  `enough: false` with no rate.
- Totals add up the per-task medians and p90s, the wall time by wave (the slowest task of each
  wave), and the expected number of first-try passes.
- It never creates the store, and prints nothing when the store is missing or has no history.

`forge-parallel.sh run` prints the same block before it dispatches. It runs with a time budget
(`FORGE_FORECAST_BUDGET_S`, default 3 seconds), prints one line and moves on if reading the
history takes longer, never prints errors and never changes the exit code. `--no-forecast` or
`FORGE_FORECAST=off` turn it off. The block is written to the plan as `forecast.json` and
recorded once as a `forecast` event, so it can later be compared to what the run did.

The history is observational: harder tasks tend to go to stronger models, so a rate describes
what happened to similar tasks, not what a model would do on this one. It only advises: nothing
that routes a task or builds a prompt reads it.

## Importing old runs

`import` reads run dirs that predate live capture through the same readers. An execution
seen both live and imported collapses to one event. Old dirs have no begin-time snapshot,
so the importer will not guess which execution wrote a mutable file:

- The **newest** task attempt gets the task dir's current files, flagged `legacy`.
- **Earlier** attempts get only what is immutable:
  - their own dispatch dirs
  - the diff QA was shown (the last `` ```diff `` block of the QA prompt)
  - the status that `attempts.tsv` logged, or failing that, the QA verdict, with
    `status_source` saying which
- Unfinished executions (still running, or killed) are skipped. The first write of a rid
  wins, so importing one now would block the real outcome that live capture records when
  it ends. Re-import the dir afterwards to fill anything live capture missed.
- Repos under temp dirs (test runs) are skipped unless `--include-temp` is given.
- Archives are extracted to a temp dir and mapped back to their original path. The legacy
  run id hashes that original path, so the archive and the original dir agree on it.

- **Import is strict when a caller must know.** `importer.import_run` is strict by default and
  `forge runs rm` relies on it: it stores through `record_strict`, which raises (and the deletion
  stops) if any event, blob included, was not stored, or any part of the run could not be read,
  instead of treating an unreadable file as absent. Live capture stays fail-open. Interventions import by content, not by time of capture:
  `tasks/<id>/escalations.tsv` (four or five columns; a missing `source` reads as `policy`) and
  `control.log` produce the same event ids as live capture, so a re-import fills gaps without
  duplicating. A line that does not parse is reported as failed.
- **Archives describe themselves.** `archive_run` writes `FORGE_ARCHIVE.json` first
  (`format`, `original_path`, `run_id`, `dataset_uid`, `created`), then `FORGE_ARCHIVE_EXCLUDED.txt`, then
  the run dir under its basename. Symlinks, files over 50 MB, `review-*` snapshots and what the
  importer never reads are left out and listed in the excluded file. Importing such an archive maps
  it back to `original_path`, so its run id and uid match the original dir's; an unreadable path
  during archiving is an error, not a silent omission. The archive is written to a temp name, fsynced
  and renamed.

`~/.local/share/forge/legacy/` holds archives of run dirs taken before live capture, with a
`MANIFEST.tsv`. Passing that dir to `import` reads every archive, plus the ledger copies
listed in `ledgers/REPOS.tsv`.

## Invariants

- **Never inside the repo.** Capture refuses, silently and before any write, when the store
  resolves inside the run's repo or a forge worktree. `integrate` refuses a dirty tree.
- **Never read by a prompt builder.** Memory inject, the capsule and Ripwire must not read
  the store. A reviewer that could see earlier runs' verdicts would not be independent.
  `tests/test_dataset.py` enforces that outside its own package, the dataset is only
  imported for capture. The one read is `run`'s [plan forecast](#plan-forecast), which goes to
  the operator's terminal and `forecast.json`, never into a prompt.
- **Read-only in your repos.** Capture and the harvester run only object-store git queries
  in a repo: `rev-parse`, `rev-list`, `log`, `show`, `ls-tree`, `cat-file`, `diff` between
  commits and `blame` at a commit. All run with `GIT_OPTIONAL_LOCKS=0`. None takes the
  working tree or refreshes the index. `outcomes` reads the store through a read-only
  connection and writes only what it found.
- **Private.** Prompts, diffs and logs can contain secrets. The dir is `0700` and its files
  `0600`. Do not sync it. Redaction belongs to whatever exports a training set.

## Not captured

- **Token counts for openclaude, opencode and antigravity.** Those harnesses expose none.
  They are recorded as null.
- **Dispatches refused before `begin()`** (an unknown alias, bad flags). No model ran.
- **The orchestrator's own reasoning.** Only its outputs are recorded: the task table,
  `request.md` and the approval edits visible between the planner's message and the
  declared table.
- **Counterfactuals.** Each task runs on one model, so nothing here says whether another
  model would have done better. That needs deliberate head-to-head runs.
