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
`codex exec resume`. One task's confused turn can't poison another, and each task's context
stays small.

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

`integrate` refuses without `--approved`, and refuses on a dirty working tree.
For Fractal execution, pass tier pools to `plan` and backend/limit flags to `run`.
`--max-parallel` limits task pipelines; the shared `--fractal-concurrency` limit includes
all their child invocations and QA. See [decomposed Fractal execution](../references/decompose.md#fractal-within-decomposed-tasks).

## Resuming an interrupted run

Run `run` again to skip MERGED tasks, merge saved PASS results after fingerprint checks,
and dispatch eligible pending tasks. Tasks paused on an infrastructure failure (`INFRA`) are
dispatched again without any flag, since nothing was wrong with the work. Failed and
interrupted tasks need an explicit `retry` (or `run --retry-failed N` for rejected ones).
Their worktrees and pinned baselines remain available. Concurrent run/retry/integrate
operations for the same plan are excluded by an advisory lock.

For a Fractal-backed run, use `forge fractal resume RUN_ID` to recover recorded execution
and pipeline checkpoints. A task/subtree selector resumes only that execution scope.
Explicit `retry` retains the backend and frozen pools; a replacement dwarf must already
belong to that pool. See [recovery](../references/fractal.md#limits-and-lifecycle).

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
