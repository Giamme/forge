# Decomposed runs

[← README](../README.md) · [All docs](README.md)

Split one goal into tasks, route each to a model by difficulty, and run them in parallel in isolated worktrees, merging only the tasks that pass review.

`--decompose-level low|medium|high` turns one dwarf into many. Without the flag, nothing
changes — one dwarf, one QA pass.

## Two independent axes

Easy to conflate, so: **decompose-level** is how finely the *goal* is split. **difficulty**
is how hard each resulting *task* is.

| `--decompose-level` | Split |
|---|---|
| `low` | only where the goal has obviously independent pieces; 2–3 coarse tasks |
| `medium` | one task per coherent unit (module, feature slice); typically 4–6 |
| `high` | finest split where each task is still independently reviewable *and* independently correct; typically 7–12 |

| `difficulty` | Means |
|---|---|
| `low` | mechanical and local: rename, move, docs, a test for behaviour that already exists |
| `medium` | self-contained implementation against a clear spec; bounded blast radius |
| `high` | needs design judgment, cross-cutting behaviour, subtle edge cases, state or concurrency |

Difficulty is about **judgment required, not diff size**. A five-hundred-line mechanical
rename is `low`. A ten-line concurrency fix is `high`. Getting this backwards sends the cheap
model at the subtle problem and the expensive one at the boilerplate — worse than not routing
at all.

## Routing dwarves to tasks

Per-task assignment can't be expressed when you type the command, because the tasks don't
exist yet. Naming a dwarf per task would mean guessing how the goal will split. So you map
**model tiers to difficulty tiers**, once, in advance — three layers, each optional:

```bash
# 1. one dwarf for everything
--dwarf sol:xhigh

# 2. per-tier routing, with plain --dwarf as the fallback for any tier not named
--dwarf-high sol:xhigh --dwarf-medium luna:high --dwarf-low gemini:low:antigravity
--dwarf sonnet:medium

# 3. pool several models within one tier — round-robined across tasks
--dwarf-high sol:xhigh,terra:xhigh
```

`--qa`, `--qa-high`, `--qa-medium`, `--qa-low` mirror it exactly. QA has no tier rules by
default and falls back to `opus`.

Resolution order per task: **explicit column value → the tier's rule → plain `--dwarf` →
`UNASSIGNED`**. An `UNASSIGNED` task makes `plan` exit `2` and `run` refuse outright. forge
does not invent a model when you haven't said which one should spend your quota.

Layer three is **the approval gate**: after decomposition, before any dispatch, you get the
task table with each task's difficulty and resolved dwarf/qa. Retier a task ("2 is actually
medium") and it re-routes per your rules, or override a single row's dwarf outright. This is
also the cost gate — `high` on a large goal is 20+ dispatches, which should be visible before
it's spent.

**Exploration is on by default** (`--explore 0.2`):
- **Which rows:** only rows left to forge, never one whose dwarf column you filled.
- **What changes:** now and then such a row gets its *neighbouring* tier's spec from your own
  pools: the next cheaper tier for medium and high rows, the next stronger for low rows.
  Pools that differ only in effort (`--dwarf-low sol --dwarf-high sol:xhigh`) compare the
  two efforts.
- **How often:** about one row in ten at the default.
- **What you see:** each explored row is marked `*` in the table, before anything is spent.
- **What it's for:** `forge dataset routing` reads the draws back as pass rates per model and
  effort. Pass `--explore 0` to turn it off.

See [decompose.md](../references/decompose.md#exploratory-routing-on-by-default).

## Waves, and what actually bounds parallelism

A **wave** is the largest set of tasks whose dependencies are all satisfied by earlier waves
**and** whose declared `files` sets are pairwise disjoint.

Disjointness — not the task count — is the real bound. Two dwarves editing one file produce a
conflict no reviewer can untangle and no merge can resolve honestly, so they land in different
waves however the goal was decomposed. When that happens forge says so:

```
deferrals (serialized to protect the merge):
  wave 1: lint deferred (file overlap with a task already in this wave)
```

That line matters. A decomposition that looks parallel but serializes at run time should
announce it, rather than leaving you wondering why eight tasks took eight rounds. When most
tasks overlap, the fix is a better split — not more parallelism.

Waves are a planning preview. A Python 3 standard-library coordinator starts eligible tasks
as prerequisites merge and capacity becomes available, while excluding overlapping active
paths. Merges are serialized. Each task pins the integration commit at dispatch; retries keep
that baseline, and QA sees only baseline membership recorded for that task.

## Fresh session per task, plus a capsule

Every dispatch is a **clean slate** — forge never passes `--continue`, `--resume`, or
`codex exec resume`, except the opt-in `plan --reviewer-session` experiment (it continues
a reviewer's session across rounds on claude, openclaude and codex). One task's confused
turn can't poison another, and each task's context stays small.

Clean slates alone would leave every dwarf blind to the wider run, so a short `capsule.md` is
generated from the task's pinned baseline and preserved for both implementation and QA:

```
# forge run <run-id>
Goal: <overall goal>
You are doing ONE task of <N>. Do not do the others' work.

## Your task
tokens — Add the token model   difficulty: medium
Files you own: src/auth/token.py

## Run status
id        diff     status       files
middleware high    MERGED       src/auth/middleware.py
tokens     medium  THIS ONE     src/auth/token.py
docs       low     NOT IN BASE  README.md

## Ground rules
- Your branch already contains every MERGED task's work — do not reimplement it.
- Files owned by other tasks are off limits; another dwarf is editing them now.
- If your task truly needs a change in someone else's file, say so in your final
  message instead of making it.
```

It's written **per task**, not once per run — tasks in a wave execute concurrently, so a
single shared capsule file is a race in which each task overwrites it and reads back whichever
sibling wrote last, handing a dwarf someone else's task description.

For QA the capsule additionally scopes the review: code from `MERGED` tasks is already in the
base and is not this task's bug. Without that, a wave-2 reviewer reports wave-1 code as
defects in the diff it was handed.

## Planning

A dwarf that is handed only an objective plans its own approach, silently, inside its own
run. Usually fine — but in a decomposed run the capsule prevents two dwarves editing one
*file* and does nothing about them inventing incompatible *interfaces* at a shared seam.
Both stay in their lane, both pass their own QA, and the mismatch surfaces at integration.

So forge plans before it dispatches. By default the orchestrator does it and writes the
approach into each task's prompt — no extra dispatch. `--planner <spec>` hands that stage to
a named model instead:

```bash
/forge "migrate the API layer to gRPC" --decompose-level medium \
  --planner sol:xhigh --dwarf-high sol:high --dwarf luna:high
```

**One planner dispatch per run, never one per task.** That is the whole point: a single mind
designs both sides of every seam, where N independent planners would recreate the problem.
Effort defaults to `xhigh`, since a bad plan is executed at full price by every dwarf
downstream of it. The planner runs with QA's permission profile — it reads the repo and
writes nothing to it.

The approach shows up in the approval table under each task, which is the last moment it can
be changed for free, and it is handed to QA as the *intended* approach — so a review can
finally say "this works, but it abandons the planned shape" rather than only judging against
the objective.

## Plan, run, integrate

```bash
PLAN=/tmp/forge-auth

# 1. plan and inspect the routing
bash scripts/forge-parallel.sh plan "$PLAN" --repo ~/dev/api \
  --dwarf-high sol:xhigh --dwarf-low gemini:low:antigravity --dwarf luna:high

# 2. see exactly what will be dispatched, without dispatching
bash scripts/forge-parallel.sh run "$PLAN" --dry-run

# 3. run it, four dwarves at a time, implementers unsandboxed
bash scripts/forge-parallel.sh run "$PLAN" --max-parallel 4 --yolo-dwarf

# 4. after reading the results, deliver the passing work
bash scripts/forge-parallel.sh integrate "$PLAN" --approved
```

`run` starts by printing a forecast for the plan from the dataset: likely tokens, time and
first-try PASS rate per task, with tasks unlikely to pass first time flagged. It is advisory only,
and `--no-forecast` turns it off. See [plan forecast](../references/dataset.md#plan-forecast).

`integrate` refuses without `--approved`, and refuses on a dirty working tree.
For Fractal execution, pass tier pools to `plan` and backend/limit flags to `run`.
`--max-parallel` limits task pipelines; the shared `--fractal-concurrency` limit includes
all their child invocations and QA. See [decomposed Fractal execution](../references/decompose.md#fractal-within-decomposed-tasks).

## Resuming an interrupted run

Ctrl-C (or a SIGTERM) on `run` stops the running tasks' process groups, waiting up to
`FORGE_INTERRUPT_GRACE` seconds (10 by default) before killing what is left, and stops any
harness that outlived its dispatch. A task that had already finished is reaped first (a PASS
still merges); only the tasks still running become `INTERRUPTED`, with their partial work kept
in the worktree. Verification is skipped, or stopped if it had started (`verification.status`
`INTERRUPTED`, not rerun). The summary lists each interrupted task with its `retry` command,
the plan lock is released, and `run` exits 130.

Run `run` again to skip MERGED tasks, merge saved PASS results after fingerprint checks,
and dispatch eligible pending tasks. Tasks paused on an infrastructure failure (`INFRA`) are
dispatched again without any flag, since nothing was wrong with the work. Failed and
interrupted tasks need an explicit `retry` (or `run --retry-failed N` for rejected ones).
Their worktrees and pinned baselines remain available. Concurrent run/retry/integrate
operations for the same plan are excluded by an advisory lock.

To pause a run on purpose, drain it instead of signalling it (see below): nothing is
interrupted, and `forge control resume` continues the same run.

For a Fractal-backed run, use `forge fractal resume RUN_ID` to recover recorded execution
and pipeline checkpoints. A task/subtree selector resumes only that execution scope.
Explicit `retry` retains the backend and frozen pools; a replacement dwarf must already
belong to that pool. See [recovery](../references/fractal.md#limits-and-lifecycle).

## Changing a run while it works

`forge control` changes a plan's knobs without stopping it (forge-ui calls the same command):

```bash
forge control show <plan-dir> [--json]                    # every control, per-task overrides, pins, recent changes
forge control set <plan-dir> qa-threshold P2              # also --task <id>; P0-P3, none (P3), default (plan only; P2) or inherit (task only)
forge control set <plan-dir> retry-budget 3 [--task <id>] # automatic retries; live scheduler only
forge control set <plan-dir> infra-retries 2              # infrastructure waits per scope; live scheduler only
forge control set <plan-dir> max-parallel 2               # tasks at once, 1-64; live scheduler only
forge control set <plan-dir> infra-backoff linear:60,60   # the next infrastructure wait; none = exp:60
forge control set <plan-dir> escalate-effort on           # the effort step before a policy stop
forge control set <plan-dir> timeout|escalate-after|verify-retries <n>
forge control pin <plan-dir> <task> sol:high:codex        # the model the next full retry uses, once
forge control unpin <plan-dir> <task>
```

And to steer the scheduler itself (each needs a live run, exit 3 otherwise):

```bash
forge control drain <plan-dir>                    # start nothing new; running tasks finish, the run waits for resume
forge control resume <plan-dir>                   # end the drain
forge control stop <plan-dir>                     # finish what runs, start nothing, end the run (exit 130)
forge control stop <plan-dir> --now               # interrupt the running tasks too, like Ctrl-C
forge control retry <plan-dir> docs --dwarf sol:high   # queue a retry of a stopped or failed task
forge control add <plan-dir> docs-fix             # queue a row you appended to tasks.tsv (with its prompt.md)
```

A stop skips verification and exits 130, unless every task had merged by then, and a drain
with nothing left to start lets the run end as usual: both act on work still to do.
`retry` and `add` only queue a request in `control.requests/`; the scheduler applies or refuses
it at its next pass and logs the outcome in `control.log` under the same request id (`forge
control show` lists the log). A retry is prepared exactly as `forge-parallel.sh retry` prepares one,
so a task the policy stopped needs `--dwarf`; it spends no automatic retry budget. An added row
may depend only on tasks the run already knows. Neither outlives the run that was live when it
was queued, and both are refused once the run is stopping.

Each change is validated, written under `control.lock`, and says when it applies:

- **The QA threshold is read once per attempt**, at the start of the task's next attempt; the
  attempt already running keeps the one it began with, so a verdict is never judged by a bar that
  moved under it. A `none` (P3) exempts that task from a looser plan threshold; `inherit` drops
  the override. A plan-level `default` removes the setting, so the threshold is P2 again.
- **Budgets stay "per invocation".** `run --retry-failed N` and `--infra-retries M` still apply
  only to the invocation that was given them and are not remembered; the next `run` starts
  from its own flags. A rule that came back from an old file would silently spend money nobody
  asked for. While a scheduler is live it mirrors them into the plan (`retry_budget`,
  `infra_budget`, `tasks/<id>/retry_budget`) and re-reads them at every decision, which is what
  `forge control` edits. When the scheduler stops it removes them; so a budget can only be changed
  while a run is live (exit 3 otherwise), and the change dies with the run.
  `tasks/<id>/retries.used` counts the retries spent and stays after the run for forge-ui.
- **A pin is one-shot.** `pin` makes the task's next *full* retry use that spec (checked with
  `forge-dispatch.sh doctor`, which spends no quota). A QA-only resume spends no implementer
  attempt and leaves the pin for later. The retry applies it, writes it into `tasks.tsv`, records
  an `escalations.tsv` row with `source` `pin`, clears any `split.suggested` stop and consumes
  it. That row is never written through a symlink: if `escalations.tsv`, or any directory
  from the plan down to it, is one, the pin stays and nothing is retried. The run summary
  says a pin waits (`pinned; after the QA-only re-run`) only when the next entry really
  re-runs QA: a `resume` marker, or an unreviewed verdict, that still matches the
  worktree. A marker the next retry will discard is reported as `pinned`.
  Fractal plans never apply pins, and a MERGED task refuses one.
- **Who decided an escalation.** The last column of `tasks/<id>/escalations.tsv` is `policy`
  (`--escalate-after`), `retry` (a typed `retry --dwarf`), `pin` or `fallback`
  (`--infra-fallback` moved a task off a model at capacity); rows from before it was added
  have four columns and read as `policy`.
- **Max parallel and the backoff apply at the next decision.** A lower `max-parallel` lets the
  running tasks finish and never stops one; `infra-backoff` shapes the next infrastructure wait,
  not the one already counting down.
- **The effort step re-decides stopped tasks.** Turning `escalate-effort` on clears the policy's
  stop markers, so a task stopped at the top tier is tried at its model's next effort on its
  next retry.

Every change is appended to `control.log` (one JSON line: `at`, `actor` `cli` or `ui`, `action`,
`task`, `key`, `value`, `previous`) and recorded in the dataset as a `control` event, so a run's
human interventions are on the record next to what the policy did. Setting a control to the
value it already has changes and logs nothing.

## Splitting and combining plans

`run --retry-failed N` removes most of the reason to split a plan (a failed task no longer holds
the rest of a run). When you still want independent work in separate plans — separate locks,
separate integration branches, one verified at a time — these two make it a supported operation
instead of hand-built git plumbing:

- `split <plan> <new-plan> --tasks a,b` moves tasks **with their saved state** (attempts,
  findings, reviewed commit, status) into a new plan: their branches and worktrees are renamed to
  the new run (`forge/<new>/<id>`), the new integration branch starts at the source's accepted
  tip, dependencies on already-merged tasks are dropped, and the source forgets the moved rows
  (`split-<new>.txt` records them). It refuses — changing nothing — for a Fractal-backed plan, a
  MERGED or RUNNING task, a selection that leaves a dependent behind, or a dependency that is
  neither moving nor merged. Copy first, delete last; an interrupted split rolls back.
- `combine <new-plan> <plan-a> <plan-b>…` joins plans whose tasks are **all `MERGED`** into one
  plan: a new integration branch merges each source's, the per-task records are copied, and
  `run <new-plan>` then only verifies (no model call) so `integrate` works unchanged. It refuses
  different repositories, locked or dirty sources, duplicate task ids, and any task left behind;
  a merge conflict exits `6` after removing everything it created. `--verify` sets the combined
  plan's check; `--dry-run` previews and predicts conflicts. Task branches keep their original
  names.

## Scope drift

After each dwarf, the runner compares what the task **declared** in `files` against what it
actually touched (a declared directory covers the paths beneath it). Undeclared paths appear
in the summary and in the QA prompt:

```
scope drift (touched files the task did not declare):
  auth           src/db.py
```

Never a failure — a dwarf that genuinely needed one more file did the right thing. What it
buys is that a merge `CONFLICT` two waves later arrives with its cause already named.

## `tasks.tsv`

Tab-separated, one row per task, written during decomposition:

```
# id  deps  difficulty  files  dwarf  qa  title
middleware	-	high	src/auth/middleware.py	-	-	Auth middleware
tokens	-	medium	src/auth/token.py	-	-	Token model
route	middleware,tokens	medium	src/routes/login.py	-	-	Login route
docs	-	low	README.md	-	-	Document the auth flow
```

| Column | Meaning |
|---|---|
| `id` | short unique slug; also the worktree and branch name |
| `deps` | comma-list of task ids that must land first, or `-` |
| `difficulty` | `low` \| `medium` \| `high` |
| `files` | expected touch set, comma-separated — **drives disjointness** |
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins |
| `title` | one line, shown in the approval table and the capsule |

Each task also needs `tasks/<id>/prompt.md`, written the same way a single-task forge goal is.

`files` is a **promise, not a prediction**: it's what the wave planner trusts when deciding
what may run concurrently, and what the capsule tells other dwarves not to touch. An
understated `files` is how two dwarves end up in the same file.

`plan` writes resolved specs back into the `dwarf`/`qa` columns, so `run` never consults the
routing rules — and an explicit value survives a re-plan, which is what makes a gate override
durable. (`UNASSIGNED` is treated as a leftover marker and re-resolved.)
