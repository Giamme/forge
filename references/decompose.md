# Decomposed runs

Mechanics behind `--decompose-level`: how a goal becomes tasks, how tasks are routed to
dwarves by difficulty, how waves are computed, and what happens when something fails.

Read this when writing a decomposition, when routing does something unexpected, or when a
run leaves branches behind.

## Contents

- [The two axes: decompose-level and difficulty](#the-two-axes-decompose-level-and-difficulty)
- [tasks.tsv](#taskstsv)
- [Routing](#routing)
- [Waves and disjointness](#waves-and-disjointness)
- [The capsule](#the-capsule)
- [Planning](#planning)
- [QA verdicts](#qa-verdicts)
- [Retrying, and resuming an interrupted run](#retrying-and-resuming-an-interrupted-run)
- [Branches, worktrees, cleanup](#branches-worktrees-cleanup)
- [Failure modes](#failure-modes)

## The two axes: decompose-level and difficulty

These are independent and easy to conflate.

**`--decompose-level`** controls how finely the goal is split:

| Level | Split |
|-------|-------|
| `low` | only where the goal contains obviously independent pieces; 2–3 coarse tasks |
| `medium` | one task per coherent unit (module, feature slice); typically 4–6 |
| `high` | finest split where each task is still independently reviewable *and* independently correct; typically 7–12 |

At every level, two rules override the target count: never split so fine that a task cannot
be verified on its own, and never emit two tasks that must edit the same file — make it one
task, or add a dep. The counts are guidance, not quotas; a goal with three natural seams
produces three tasks at `high` too.

**`difficulty`** is rated per task, by what the task demands of the model — **not** by how
much code it produces:

| Difficulty | Means |
|------------|-------|
| `low` | mechanical and local: rename, move, docs, a test for behaviour that already exists. Little design judgment; a mistake is obvious. |
| `medium` | self-contained implementation against a clear spec; some design choices, bounded blast radius. |
| `high` | needs design judgment, touches cross-cutting behaviour, has subtle edge cases or state/concurrency, or the right approach is not obvious from the goal. |

A five-hundred-line mechanical rename is `low`. A ten-line concurrency fix is `high`. Sizing
by diff length instead of by judgment required is the main way difficulty routing stops
paying off — it sends the cheap model at the subtle problem and the expensive one at the
boilerplate, which is worse than not routing at all.

## tasks.tsv

Tab-separated, one row per task, written by whoever decomposes the goal:

```
# id  deps  difficulty  files  dwarf  qa  title
mul	-	high	src/calc.py	-	-	Add multiply() to the calculator
docs	-	low	README.md	-	-	Document the calculator API
tests	mul	medium	tests/test_calc.py	-	-	Tests for multiply()
```

| Column | Meaning |
|--------|---------|
| `id` | short slug, unique; also the worktree and branch name |
| `deps` | comma-list of task ids that must land first, or `-` |
| `difficulty` | `low` \| `medium` \| `high` |
| `files` | expected touch set, comma-separated. Drives disjointness — see below |
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins |
| `title` | one line, shown in the approval table and the capsule |

Each task also needs `tasks/<id>/prompt.md` — the implementation instruction, written the
same way a single-task forge goal is. `tasks/<id>/approach.md` is optional: a few lines of
intended approach, written by the orchestrator or by `--planner`. When present it is shown
in the approval table, prepended to the dwarf's prompt, and given to qa as the intent to
review against — so a review can say "this works but abandons the planned shape", which
matters when a sibling task was planned against that shape.

`files` is a promise, not a prediction: it is what the wave planner trusts when deciding what
may run concurrently, and it is what the capsule tells other dwarves not to touch. An
understated `files` is how two dwarves end up in the same file and produce a conflict.

The promise is checked. After each dwarf finishes, the runner compares what the task
*declared* against what it actually touched (a declared directory covers the paths beneath
it, so `src/routes` covers `src/routes/api.py`). Undeclared paths go into
`tasks/<id>/drift.txt`, into the run summary, and into the QA prompt — which already asks
whether the diff went outside the task's scope and previously had no way to know. It is a
warning and never a failure: a dwarf that genuinely had to touch one more file did the right
thing. What it buys is that a merge `CONFLICT` two waves later arrives with its cause
already named, instead of leaving someone to diff two branches to find out.

`plan` resolves difficulty into concrete specs and writes them back into the `dwarf`/`qa`
columns, so `run` never consults the routing rules. This is also what makes a gate override
durable: an explicit value is preserved across a re-plan, while `UNASSIGNED` is treated as a
leftover marker and re-resolved.

## Routing

```
--dwarf <spec>          fallback for any tier without its own rule
--dwarf-high <spec>     used for high-difficulty tasks
--dwarf-medium <spec>
--dwarf-low <spec>
--qa, --qa-high, --qa-medium, --qa-low     identical grammar
```

Each flag takes an ordinary forge spec (`alias[:effort[:harness]]`), so a tier can name a
model *and* the harness it runs through. A comma-list pools several models within one tier
and round-robins across them — useful when a tier has more tasks than one provider should
carry, since distinct harnesses have independent quotas.

Resolution order per task: explicit column value → the tier's rule → plain `--dwarf` → and if
none of those exist, `UNASSIGNED`. An `UNASSIGNED` task makes `plan` exit 2 and `run` refuse
outright. Forge does not invent a model when the user has not said which one should spend
their quota.

QA has no tier rules by default and falls back to `opus`, matching single-task forge.

## Waves and disjointness

A wave is the largest set of tasks whose deps are all satisfied by earlier waves **and** whose
`files` sets are pairwise disjoint. Everything else waits.

Disjointness — not the task count — is what actually bounds parallelism. Two dwarves editing
one file produce a conflict no reviewer can untangle and no merge can resolve honestly, so
they are placed in different waves however the goal was decomposed. When this happens the
deferral is reported in the approval table:

```
deferrals (serialized to protect the merge):
  wave 1: lint deferred (file overlap with a task already in this wave)
```

That line matters: a decomposition that looks parallel but serializes at run time should say
so, rather than leaving the user to wonder why eight tasks took eight rounds.

Each wave branches from the integration branch **as it currently stands**, so a dependent
task sees its dependencies' merged code. Passing tasks merge `--no-ff` onto the integration
branch as their wave completes.

## The capsule

Every dispatch is a clean slate — forge never passes `--continue`, `--resume`, or
`codex exec resume`. That keeps one task's confused turn from poisoning another and keeps
each task's context small, but it also leaves each dwarf blind to the wider run.

`tasks/<id>/capsule.md` is the fix, regenerated before every dispatch because status changes
as waves land, and prepended to both the dwarf and QA prompts. It is written **per task**, not
once per run: tasks in a wave execute concurrently, so a single shared capsule file is a race
in which each task overwrites it and then reads back whichever sibling wrote last — handing a
dwarf someone else's task description. It carries the overall goal, this
task's identity and owned files, a status table of every task, and the ground rules.

It exists to prevent two specific failures that clean slates create:

- a dwarf reimplementing a helper an earlier task already merged, because nothing told it
  that task was done;
- a dwarf editing a file another dwarf currently owns — producing exactly the conflict the
  wave planner exists to avoid.

For QA the capsule additionally scopes the review: code from `MERGED` tasks is already in the
base and is not this task's bug. Without that, a wave-2 reviewer reports wave-1 code as
defects in the diff it was handed.

Keep it short. It is paid for on all 2N dispatches, so it is a status table and a few rules,
not a design document.

## Planning

The capsule stops two dwarves editing one *file*. It does nothing about two dwarves
inventing incompatible *interfaces* at a seam they share: both stay inside their own
`files`, both pass their own QA, and the mismatch only appears at integration.

Writing the approach down before dispatch is what closes that. By default the orchestrator
does it while decomposing, which costs nothing. `--planner <spec>` dispatches it instead —
**one dispatch for the whole run, never one per task**, because the value is that a single
mind designed both sides of every seam. N independent planners would recreate the problem
they were meant to solve.

The planner runs with qa's permission profile: it reads the repo and writes nothing to it.
Its output becomes `tasks.tsv` plus each `tasks/<id>/approach.md`, and the approval gate
renders both — the last moment the plan can be changed for free.

## QA verdicts

Decompose-mode QA prompts require a final line:

```
FORGE_VERDICT: PASS    no confirmed correctness bug (style nits are not failures)
FORGE_VERDICT: FAIL    at least one CONFIRMED correctness bug
```

The runner reads the last occurrence. A missing verdict is `UNKNOWN` and is treated exactly
like a failure — excluded from integration and flagged. Merging a diff whose reviewer never
reached a conclusion would defeat the point of reviewing it, so the ambiguous case fails
safe rather than optimistically.

A failing task is excluded and its branch preserved; the rest of the run proceeds. There is
no automatic repair pass, consistent with forge's rule that a dwarf → QA cycle does not loop
on its own.

## Retrying, and resuming an interrupted run

A failed task keeps its branch and its worktree. `retry` is what acts on them:

```bash
forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>]
```

It re-dispatches that one task **in the worktree it already has**, with the previous
reviewer's findings prepended to the prompt — that carry-forward is the whole difference
between a retry and simply running the task again. It then re-reviews, and merges if the
verdict is now `PASS`. `--dwarf` escalates to a stronger model and is written back into
`tasks.tsv`, so the table shows the model that will actually be spent and the escalation
survives a re-plan.

Two details that are load-bearing rather than incidental:

- **The base is pinned per task, not per run.** A retry diffs against the commit the task
  was originally branched from, so its reviewer sees the task's *cumulative* work. Reviewing
  only the fix would let the first attempt's code through unread, and re-basing onto the
  integration branch as it now stands would put other tasks' merged code into this task's
  diff.
- **An identical retry is not re-reviewed.** If the diff comes back byte-identical to the
  previous attempt, the task fails again without a QA dispatch. Paying a reviewer to read
  the same code twice buys nothing.

`retry` is a command a human types. Forge does not loop a dwarf against its own reviewer on
its own initiative, and that rule is unchanged.

**Resuming** needs no special command: run `run` again. Tasks already `MERGED` are skipped
rather than re-dispatched, each wave re-bases on the integration branch as it now stands, and
existing worktrees are reused. That is what makes an interrupted run — a killed process, a
hung harness, a closed laptop — continuable instead of a total loss.

## Branches, worktrees, cleanup

```
<plan-dir>/tasks/<id>/approach.md               intended approach, if planned
<repo>/.forge/                                  project memory (survives the run)
<repo>/../.forge-worktrees/<run-id>/<task-id>   task worktree
<repo>/../.forge-worktrees/<run-id>/_integration integration worktree
forge/<run-id>/<task-id>                        task branch
forge/<run-id>                                  integration branch
```

The worktree root is a sibling of the repo and **never** `TMPDIR`. Isolated worktrees are
never pushed, so a temp root that gets cleaned takes the only copy of that work with it —
this has already destroyed real work in this user's `linear-spanks` runs.

Cleanup rules:

- A successful task's worktree is removed only after `results.tsv` is written and read back.
- A failed, conflicted or unknown task keeps **both** its branch and its worktree, so there
  is something to inspect and retry from.
- The integration branch is left in place. Merging it into the user's branch is a separate
  `integrate --approved` step and never happens automatically.

The user's own branch and working tree are untouched for the whole run.

## Failure modes

| Symptom | Cause |
|---------|-------|
| task status `ERROR` | worktree creation or a dispatch failed; see `tasks/<id>/dwarf.out` / `qa.out` |
| task status `TIMEOUT` | that role exceeded the dispatch timeout and was killed. It has usually left a partial edit in the worktree; `retry` continues from it |
| task status `FAIL` with "produced no changes" | the dwarf ended without editing anything — usually an ambiguous prompt it could not resolve headlessly |
| task status `UNKNOWN` | QA never emitted a verdict line; read `tasks/<id>/qa.last` |
| task status `CONFLICT` | QA passed but the merge onto the integration branch conflicted — `files` was understated somewhere |
| every wave has one task | `files` sets overlap across most tasks; the decomposition is not actually parallel |
| dwarves ignore existing work | `goal.txt` or `files` missing, so the capsule carries no useful status |
| `.forge/` appears in a task's diff | the capture is missing `':(exclude).forge'` — see `memory.md` |
| dwarves build incompatible interfaces | no `approach.md`; the seam was never agreed. Plan it, or use `--planner` |
| "scope drift" in the summary | the task touched files it did not declare. Not a failure — but if a later task conflicts on one of those files, this is why |
| `retry` says "produced no new changes" | the dwarf returned a byte-identical diff. It has nothing to add; escalate with `--dwarf` or fix the prompt |
| a task will not re-run: "already exists" | a worktree directory was deleted by hand. `run`/`retry` prune stale registrations first, so re-run rather than deleting the branch |

A run where several tasks report "produced no changes" usually means the decomposition
produced tasks that were not independently actionable — the fix is a coarser
`--decompose-level`, not more retries.
