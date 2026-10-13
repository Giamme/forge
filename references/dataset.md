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
| `plan` | `plan` | goal, original request, decompose level, author (orchestrator or planner), per-task declared vs executed difficulty and who set it, pools, waves, Jev routing and coupling. Blobs: both task tables, prompts, approaches, excerpts of the declared files at plan time, the file inventory, `memory.md` |
| `run_end` | `run` | results per task, the accepted integration commit, `memory.md` |
| `invocation` | user-facing `forge-parallel.sh` subcommand | argv, exit, duration, plus final review, accept, integrate and split/combine payloads |
| `event` | point event | `merge` (MERGED, CONFLICT with the files, INVALIDATED and why), `integrate_advanced`, `integrate_conflict`, `combine_conflict`, `sched_terminal`, `subset_run` (every outcome), `memory_record`, `memory_prune` |
| `jev` | Jev request | site, answers, usage, latency, mode. Blob: the **full request** (state and questions). Its original sha is `jev.jsonl`'s `key` |
| `fractal_step` / `fractal_node` | Fractal step, node state | decomposition decision, iteration, resolved model, cost, tokens, candidate diff |
| `ledger` | ledger row | imported `.forge/ledger.tsv` rows |

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

## Commands

```sh
./forge dataset status [-v] [--json]    # where, how much, and what looks incomplete
./forge dataset dump [--kind K] [--run R] [--task T] [--since UNIX]   # events as JSON lines
./forge dataset blob <sha>              # print one stored blob
./forge dataset import <dir|archive|legacy-dir>... [--registry] [--fractal] [--ledger REPO] [--dry-run]
```

`status` runs a different completeness check for each mode, because each mode
legitimately lacks different things. A solo run has no separate verification, and a
final review's QA has no task attempt. The checks are:

- executions that started but never ended
- task QA dispatches with no task attempt
- parallel PASSes with no merge outcome in a finished run
- parallel runs with no end
- blob references with no stored blob

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

`~/.local/share/forge/legacy/` holds archives of run dirs taken before live capture, with a
`MANIFEST.tsv`. Passing that dir to `import` reads every archive, plus the ledger copies
listed in `ledgers/REPOS.tsv`.

## Invariants

- **Never inside the repo.** Capture refuses, silently and before any write, when the store
  resolves inside the run's repo or a forge worktree. `integrate` refuses a dirty tree.
- **Never read by a prompt builder.** Memory inject, the capsule and Ripwire must not read
  the store. A reviewer that could see earlier runs' verdicts would not be independent.
  `tests/test_dataset.py` enforces that outside its own package, the dataset is only
  imported for capture.
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
