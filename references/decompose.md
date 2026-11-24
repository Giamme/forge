# Decomposed runs: planning

Mechanics behind `--decompose-level`: how a goal becomes tasks, how tasks are routed to
dwarves by difficulty, how waves are computed, the size and gates a plan must pass, and its shape.
Read this file to plan a decomposed run; it is the only one planning needs.

The rest of the decomposition reference is split by phase, so a run reads only what applies:

- [Reviews and verdicts](decompose-review.md): QA verdicts, fail-first evidence, pre-review tests,
  implementer sessions, shadow reviews, severity, thresholds, known issues and the finding sweep.
- [Retries, stops and recovery](decompose-recovery.md): retrying and resuming, `NEEDS_DECISION`
  and reviews that don't converge, `INFRA` stops, implementer guards, accept, split and combine.
- [Integration and verification](decompose-integration.md): chained plans, branches and worktrees,
  worktree setup, failure modes, execution checks and the integration verification.
- [Opt-in modes](decompose-opt-in.md): groups and group review, retro notes, brief mode and task
  summaries.

## Contents

- [The two axes: decompose-level and difficulty](#the-two-axes-decompose-level-and-difficulty)
- [tasks.tsv](#taskstsv)
- [Routing](#routing)
- [Subtasks: a task split at any level](#subtasks-a-task-split-at-any-level)
- [A goal larger than one plan](#a-goal-larger-than-one-plan)
- [Waves and disjointness](#waves-and-disjointness)
- [Task size and acceptance criteria](#task-size-and-acceptance-criteria)
- [Plan gates: sent back, and the block under the table](#plan-gates-sent-back-and-the-block-under-the-table)
- [The capsule](#the-capsule)
- [Planning](#planning)
- [Plan shape](#plan-shape)

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
task, or add a dep. A single-file deliverable (one HTML page, one script, one notebook)
still splits by module when it splits at all: each task owns its own source file (for
example `src/physics.js`), and one final low-difficulty task assembles them into the
required file. A pure chain is worse than one task: no two tasks run at once, and each
pays its own implementation and review. Prefer parallel branches; when the request allows
none, return one task. `plan` still accepts a chain; the approval table says when one
task would have run once. The counts are guidance, not quotas; a goal with three natural
seams produces three tasks at `high` too.

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

**`risk`** is a separate rating, recorded in shadow (#122): what a mistake that gets through
would cost — what breaks, for whom, and how hard it is to undo. It changes no model, reviewer
or verdict yet; `forge dataset risk` measures whether it predicts problems difficulty misses.

| Risk | Means |
|------|-------|
| `low` | local, and easy to see and revert: docs, a test, an internal helper. |
| `medium` | wrong behaviour users would notice, undone by a revert. |
| `high` | hard to undo or reaching everyone: release and CI files, migrations and persisted data, auth and permissions, public APIs. |

A one-line change to a release script is low difficulty and high risk. Write the rating as
`low`, `medium` or `high` on the first line of `tasks/<id>/risk` before `plan` (the planner
writes it from its reply). A plan without ratings behaves exactly as before, and `tasks.tsv`
keeps its columns. A collapsed chain gets the highest of its parts' ratings, or none when a
part was not rated.

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
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins. `script` in `dwarf` runs the executable `tasks/<id>/run` ([Scripted tasks](#scripted-tasks)) |
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
leftover marker and re-resolved. A `script` cell is one of those explicit values: `plan` leaves
it, and requires `tasks/<id>/run` to be executable.

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
their quota. The explicit value `script` is reserved for a command, and it is not drawn from a
pool. See [Scripted tasks](#scripted-tasks).

QA has no tier rules by default and falls back to `opus` at `high`, matching single-task forge.
When the user names no reviewers, offer reviewers sized to the task's difficulty, which is what
the `--qa-<tier>` rules key on (a task's [risk](#the-two-axes-decompose-level-and-difficulty) is
recorded but routes nothing yet; never add them unprompted): `--qa-low sonnet:medium --qa-medium sonnet:high --qa-high opus:high`. In the
SWE-bench Pro benchmark (#89) Sonnet reviewers gave no false PASS at about a seventh of an
`opus:xhigh` review's cost.

### Exploratory routing (on by default)

Every task in the run history ran on the model its tier was routed to, so the history
says how that model did and nothing about how another would have done on the same task
(only 9 tasks ever ran on more than one spec). Choosing models from measured pass rates
needs some randomized picks, so `plan` makes a few: `--explore E`, with E between 0 and
0.5, defaults to **0.2**.

- **Which rows:** only rows whose dwarf column is `-` (left to forge). The pool's own turn
  is still taken, so the round robin of every other row is unchanged.
- **Two candidates:** the pool's pick and its *neighbour*, the model one tier away in the
  pools given to this plan: the next cheaper tier for medium and high rows ("is a cheaper
  model enough?"), the next stronger for low rows ("did it need more?"). Within a tier the
  round robin already alternates between pool members; across tiers nothing else ever
  produces data. The neighbour keeps the pick's position in its pool, so
  `--dwarf-low luna,haiku --dwarf-medium sol,sonnet` pairs luna with sol and haiku with
  sonnet. A different effort is a different candidate, so pools that differ only in
  effort (`--dwarf-low sol --dwarf-high sol:xhigh`) compare the two efforts. A row whose
  tier has no neighbour is not explored.
- **How often:** with probability E the row is drawn uniformly between the two, so it
  actually changes model with probability E/2 (10% at the default).
- **Never a model you did not name.**
- **The table:** an explored row is marked `*` in the approval table, and a footer states
  the active setting, so the spend is visible before anything runs. To pin another model,
  put its spec in the dwarf column and re-plan.
- **The record:** each decision goes to `tasks/<id>/route.jsonl` (append-only) with its
  two candidates, the pool's own pick, the choice, and its probability:
  `(1 - E)·[choice = pool's pick] + E / 2`.
- **Turning it off:** `--explore 0`. The choice is kept in the plan dir (`explore`), so a
  later re-plan neither turns it back on nor changes rows already resolved. Precedence:
  the flag, then a profile frozen on this call, then the plan's earlier choice, then
  `FORGE_ROUTE_EXPLORE`, then 0.2. A kept frozen profile is not applied again, so it does
  not replace the rate the plan already stored.
- **Later changes:** a re-plan draws nothing for rows that are already resolved. An
  approval-gate edit or a `retry --dwarf` simply runs a different model; the analysis
  keeps such a task in the arm it was assigned and reports it as not followed.

`forge dataset routing` reads the draws back: per (tier, pool pick, neighbour) pair, each
arm's first-try pass rate (inverse-propensity weighted, every task in the arm it was assigned),
attrition, runtime and a run-clustered interval, judged at the first of 30, 60 or 120
neighbour assignments it reaches. See [Dataset](dataset.md#routing). At the default rate
that is weeks of normal use per pair; a pair that matters can be given more with
`--explore 0.5` on its plans.

### Profiles

A profile is a named bundle of routing, policy, gates and budget, so the pools and the
policies travel together instead of being retyped. One name resolves user, then repo, then
built-in: a user profile hides a repo profile of the same name, which hides a built-in.
The built-ins are `balanced` and `lite`, the [recommended routing](../docs/models.md#recommended-routing).
`forge config profile` keeps them; `plan` and `forge-solo.sh` take one with `--profile NAME`.

The chosen profile is frozen into `<dir>/profile.json`. A flag of that call replaces only
the key it names; the other keys are applied as those flags would have been: dwarf and qa
pools, the planner, shadow review, exploration and fast roles, plus the plan files for the
final reviewer, escalation, the QA threshold, the timeout and verify retries. A QA tier the
profile does not name uses `routing.qa.any` when that pool is set; only with no such pool
does QA fall back to `opus`, which dispatch runs at `high`. Gates and budget stay
in the frozen body. With neither `--profile` nor `--no-profile`, a plan keeps the copy it already
froze and does not apply it again; a plan with no copy gets the default profile, if one is
set. The runner does not select `balanced` on its own. `--no-profile` removes the frozen
copy. See [commands](../docs/commands.md#forge-config-profile).

### Scripted tasks

A dwarf cell of `script` runs the executable `tasks/<id>/run` in the task's worktree. Use it
when the change is already a command: a codemod, a move by a map, a generator. The command
should be atomic, and it should produce the same tree each time it runs from the same base.

`plan` leaves the cell as `script`. It does not draw a pool, does not explore the row, and
does not record `tasks/<id>/running.tier`. The `qa` cell is resolved as usual, and doctor
still checks that reviewer. Doctor is skipped for the implementer. The approval table shows
`script`. The dispatch estimate counts no implementer for the row, and prints
`scripted tasks: N (ids) run tasks/<id>/run; no implementer model`. The review is still
counted. If `tasks/<id>/run` is missing or not executable, `plan` exits 2
(`task '<id>' is a script task but tasks/<id>/run is missing or not executable`) and stops
before writing the resolved `tasks.tsv`. A `--dwarf` pool, a tier pool, or a profile pool that names `script` is
refused with exit 2: the word is reserved for a task that runs `tasks/<id>/run`.

Each attempt resets the worktree to the task's pinned base (the tracked tree, and untracked
files other than `.forge`), then runs the command in its own process group. The working
directory is the worktree. Standard input is empty. The environment is `FORGE_TASK_ID`,
`FORGE_PLAN`, `FORGE_WORKTREE` and `FORGE_BASE_REF`. A file that does not start with `#!`
runs under `/bin/bash`. The limit is the plan's `timeout` in seconds, `0` for none as for a
dispatch, and 2700 seconds when the plan has none. A timeout stops the process
group. Both streams go to `tasks/<id>/script.log`. The run is also recorded at
`tasks/<id>/attempts/script-<hex>/`, where `<hex>` is 16 hex digits: a copy of `script.log`,
the `run` file as it ran (the copy the review shows, even after `run` is edited), and `metrics.json` (`role` and `harness` are `script`; `exit_code` is null
when the run timed out).

Exit 0 with a new diff commits `forge(<id>) attempt N: <title> (script)` and reviews with
`forge-contract.py qa --kind script`. The head is the task head, plus an instruction to judge
the command and its result: whether the result is complete and correct for the requirements,
and whether the command did only what it should. A mechanical change can be sampled, and the
review says what it sampled. The prompt includes the command and the last 3000 characters of
`script.log`. The tail matches a task review. A member of a `review=group` group still waits
for that group's review.

A non-zero exit is `FAIL`. `tasks/<id>/qa.last` starts `The script exited N.` A timeout is
`FAIL`, and `qa.last` says `The script timed out after Ns.` Both quote the last 3000
characters of `script.log`. There is no review and no `noretry`, so a later retry can run.
The worktree is left as the command left it. Exit 0 that changes nothing is `FAIL` as well,
with `The script produced no changes.` and `noretry`, so it is not retried. A reset that
cannot reach the base is `ERROR` and is not reviewed (`tasks/<id>/script.reset.out`).

The task is never escalated, and its failed reviews are not counted as converging. The
scheduler, `forge-route.py`, dataset routing and forge-ui treat `script` as no model. The
plan forecast does not price the row. The reviewer is still a model, and a pause on that
reviewer holds the task.

A retry runs the same command again from the base. Editing `tasks/<id>/run` is how the diff
changes. An unchanged command repeats the previous diff, and that retry stops: `FAIL`,
`noretry`, and `The retry changed nothing: the diff is byte-identical to the previous
attempt.` It is not reviewed again.

## Subtasks: a task split at any level

Goal, plans, tasks and subtasks follow one rule: a node is either done as one piece or split
into children that own parts of it. forge does this itself; there is no other runtime.

A split task's subtasks are a plan of their own in `<plan>/tasks/<id>/sub` (its `run_id` is
`<run>--<id>`, its `parent.json` names the task). They are routed with the plan's difficulty
pools and reviewer pools, each owns paths **inside the task's `files`**, and `deps` name sibling
subtasks only. They run on the same scheduler, under the run's one `--max-parallel` for the whole
tree. Each subtask is reviewed against its own prompt (which carries the task's prompt, so the
requirement is not lost). When every subtask has merged, their work is merged onto the task's
branch and the task is reviewed once on the cumulative diff, against the task's prompt. A subtask
can be split again, the same way.

**Who splits.** Never force a split: one task is always a valid answer.

- **The planner, at plan time.** A task of its reply may carry `subtasks` and a one-line
  `reason` ([planner contract](planning.md#the-planner-contract-forge-plannerpy)). `plan` makes
  the sub-plan, and the table shows the tree under `subtasks`. A task the planner kept whole with
  a `reason` has that reason recorded.
- **The user, for a task that stopped.** A `NEEDS_DECISION` whose reasons include `split` offers
  it in `decision.md`. Relay the choice; on the user's word run
  `retry <plan> <id> --decide split --subtasks FILE --reason "<their reason>" --approved`
  (`--planner SPEC` instead of `--subtasks` dispatches one read-only planner about that task).
  You may write the subtasks file yourself when the user asked you to split the task:
  `forge-planner.py node-prompt --plan <plan> --task <id>` writes the rules and the format to
  `tasks/<id>/split.prompt`. On a live run, `forge control retry <plan> <id> --decide split
  --subtasks FILE --reason "…" --approved` queues the same decision.
- **The run, when the user opted in.** `run --auto-split [--split-planner SPEC]`: a task that
  stops on `split` is split by the planner once, without waiting. Off unless asked for. Offer it
  with the table when the plan has `high` tasks; do not turn it on yourself.

`--decide split` keeps the task's work: the subtasks start from its branch and get the last
review. `--decide split=fresh` starts from the task's base, and the earlier work stays on
`refs/forge/kept/<run>/<id>`. Ask which, when the user did not say; default to keeping it.

**Limits.** `plan --split-depth N` (default 2; `0` = no splitting), `--split-children N` (6) and
`--split-nodes N` (24, the whole run). They are printed under the task table as
`decomposition limits: …` and are part of what the user approves. The first `run` freezes them
(`decomposition.json`: `limits`, `frozen`); a `plan` that would change one afterwards exits 2. A
split past a limit is not made: the node is recorded `bounded` with the limit it met, and the
task runs as one task. Say so; do not retry the split.

**Outcomes of a split request.** `retry --decide split` exits 0 after running the task with its
subtasks, or with `--no-dispatch` after making them. It exits 2 and changes nothing when the
answer was one task (recorded as kept whole, with the reason), when a limit bounded it, or when
the subtasks were refused (a file outside the task's, a dep that is not a sibling, a cycle, a
duplicate id, a prompt without `## Acceptance criteria`). A task already split is never split
again; its subtasks are kept and `run` continues them.

**While it runs.** The task is `RUNNING` while its subtasks run. If one fails, stops or waits
for a person, the task becomes `BLOCKED` with `tasks/<id>/sub.waiting` listing them, no attempt
of the task is spent, and the summary prints `waiting for their subtasks:` with the sub-plan's
path. Work on the subtask in its own plan (`retry <plan>/tasks/<id>/sub <subtask>`, and every
`--answer`, `--decide`, `accept` and `forge control` form), then `run <plan>` again. `run
--retry-failed N` and `--infra-retries M` reach the subtasks. Resume and retry keep accepted
decisions and never make a subtask twice.

**Records.** `decomposition.json` in each plan of the tree: per task `decision` (`atomic`,
`split`, `bounded`), `reason`, `by` (`planner`, `user`, `auto`, `default`), `at` and the limits
in force. `tasks/<id>/decisions.tsv` gets `retry=1 split` or `retry=1 split=fresh`: the split
grants the one attempt that reviews the whole task. `forge runs tree <plan>` prints the tree;
`forge control show`, `forge handoff` and `forge watch` name subtasks by path (`<id>/<sub>`);
forge-ui shows each node's decision, reason, model, tokens and diff.

## A goal larger than one plan

A planner that judges the goal too large for one plan returns the first coherent stage as
`tasks` and the stages after it as `later` (title and goal each). `plan` prints them under the
table (`later stages`), and the run summary names them once every task has merged. Then:

```bash
bash <skill_dir>/scripts/forge-parallel.sh next "$PLAN" [<new-plan-dir>] [--planner <spec>] [plan flags...]
```

`next` needs every task of `$PLAN` `MERGED` (exit 3 otherwise) and a planner (the plan's, or
`--planner`; exit 2 with neither). It makes the next plan dir (`<first plan dir>-<stage>` unless
named), starts it from `$PLAN`'s integration branch (`--after`), copies the options, pools and
limits, dispatches the planner on the code as it now is, and stops at the table. Show that table
and wait for approval, as for any plan; `--approved` is refused on `next`. `integrate --approved`
on the last plan brings the whole chain ([chained plans](decompose-integration.md#chained-plans)).

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

Waves remain a planning preview. At execution time a Python 3 standard-library coordinator
starts a task as soon as every prerequisite is MERGED, capacity is available, and its paths
do not overlap an active task. Each passing task merges serially through the existing
fingerprint checks. A slow unrelated task no longer holds up eligible dependents.

Each task's baseline is pinned immediately before dispatch and stays fixed across retries.

## Task size and acceptance criteria

Size predicts a failed first attempt better than anything forge asks a model. In the run
history (269 first attempts, 2026-10-17), the share a reviewer passed:

| prompt.md | passed | | declared files | passed |
|---|---|---|---|---|
| under 3K chars | 62% (n=50) | | 1 | 83% (n=12) |
| 3–5K | 56% (n=48) | | 2 | 68% (n=25) |
| 5–7K | 51% (n=49) | | 3–5 | 52% (n=75) |
| 7–10K | 33% (n=49) | | 6–8 | 40% (n=50) |
| 10–14K | 28% (n=29) | | 9 or more | 29% (n=107) |
| 14K and over | 23% (n=44) | | | |

Size and difficulty are confounded (high tasks are longer), but splitting reduces both.
So:

- Split along independently verifiable behaviour and explicit interfaces, not to make more
  parallel tasks. A task should still be reviewable and correct on its own.
- End every `prompt.md` with an `## Acceptance criteria` section: observable checks the
  reviewer can verify (a behaviour, a command that passes, a file that exists), not
  restated goals. A command names the task's own tests, never the whole suite: the run's
  verification runs the suite after each merge, and naming it here makes every attempt run it.
- `plan` prints an advisory block under the table naming tasks at 7,000 characters or 9
  declared files and over, with the history for their size, tasks with no acceptance
  criteria, and tasks whose acceptance criteria ask for the whole suite (forge's
  `tests/check.sh`, a bare `unittest discover`, `npm test`, `make check`, `cargo test` and the
  like, or a command of the plan's `--verify` or the repository's `.forge/verify`). A heading
  inside a code fence, or with nothing under it, does not count. The block never changes the
  table or the exit code.

## Plan gates: sent back, and the block under the table

With the prompt or coverage gate `on` (`forge decide gate <gate> on`; see
[decision models](decision-models.md)), `plan` can **exit 4**. That means: the gates sent work
back, nothing was validated and no `waves.tsv` was written by this call. Read
`<plan>/plan.sendback.md`, which `plan` also prints:

- **Task prompts a reviewer could not judge** (prompt gate): rewrite each listed
  `tasks/<id>/prompt.md` so a reviewer can verify it, with checkable acceptance criteria.
- **Requirements no task covers** (coverage gate): extend a task's `## Acceptance criteria`
  or add a task for each listed requirement of `request.md`.

Then run `plan` again, **once**. Forge sends each task and requirement back only once: what
is still flagged on the second call is listed under the table instead, and `plan` goes on.
With a dispatched planner (`--planner`), re-dispatch it with `plan.sendback.md` appended to
its prompt rather than editing the tasks yourself. In `shadow` a gate records and sends
nothing back, and with the gates `off` or decision models off `plan` never exits 4.

Under the table, `plan` prints a `decision-model gates:` block from `plan-gates.json` when a
gate recorded anything:

- `drift added to <task>: <files>`: drift `on` appended files to the task's `files` column
  (`tasks.declared.tsv` keeps your table), so waves treat them as declared. Shadow lines say
  *would add* and change nothing.
- `forward added a dependency: <a> now waits for <b>`: forward `on` made `a` depend on `b`
  and recomputed the waves; a pair that would form a cycle is listed as skipped. Delete the
  dep from `tasks.tsv` to undo it.
- `prompt still below … after its rewrite` and `requirements no task covers`: flagged items
  that were already sent back once.

## The capsule

Every dispatch is a clean slate — forge never passes `--continue`, `--resume`, or
`codex exec resume`, except the opt-in `plan --reviewer-session` experiment (it continues
a reviewer's session across rounds on claude, openclaude and codex) and the opt-in
`plan --implementer-session` (a retry continues its implementer's session). That keeps one task's
confused turn from poisoning another and keeps each task's context small, but it also
leaves each dwarf blind to the wider run.

`tasks/<id>/capsule.md` records ownership and baseline context. `baseline.capsule` preserves
the implementation-time capsule for QA. MERGED means the reviewed commit is an ancestor of
this task's pinned baseline, not merely that another task finished while QA was preparing.
Complete requirements, approach, retry findings and distinct memory facts are assembled once
per role; implementation and review instructions are separate.

It exists to prevent two specific failures that clean slates create, which the opt-in
`--reviewer-session` and `--implementer-session` do not change:

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
renders both — the last moment the plan can be changed for free. The contract for that
reply is [Planning](planning.md).

`forge-parallel.sh auto` does this whole path in one blocking command. The orchestrator's
job is mechanical, so a small model can drive it. `auto` copies the goal verbatim to
`request.md` and writes `auto` to `orchestrator`, then:

1. **The gate** (`--plan`, default `auto`). `forge-planner.py gate` prints `one-task` when
   the request is at most 1500 characters and at most 6 requirements, otherwise `plan`, and
   records `plan-gate.json` (`decision`, `signals`, `thresholds`, `rule` `size`). `always`
   plans; `never` is one task. The gate is conservative: on the 16 plans of benchmark #86,
   size did not predict shape.
2. **One task** writes a single row, id `task`, files `.`, difficulty from
   `--one-task-difficulty` (default `medium`), and does not dispatch a planner. Its
   criterion is that every requirement of the request is met and the tests for the files
   changed pass.
3. **Planning** uses `--planner`, or else `routing.planner` of the profile the plan flags
   select. With neither, exit 2. A doctor preflight runs before any model call. The planner
   is dispatched into the plan dir, with the fast tier when `planner` is a fast role, and
   the reply is parsed strictly.
4. **`plan`** gets the flags before `--` that are not `auto`'s own, plus `--collapse-chains`
   unless `--no-collapse`. When the gates send work back (`plan` exits 4 with
   `plan.sendback.md`), `auto` re-dispatches the planner once with that file appended and
   plans again. A second send-back stops.
5. **Without `--approved`** it stops at the table, exits 0, and prints the `run` command.
   Nothing is dispatched after the planner. `--approved` is the user's explicit
   pre-approval: `auto` then runs, with the flags after `--`, and exits with `run`'s code.

A `--` splits the command: flags after it go to `run`. Flags before it go to `plan`, except
`--goal-file`, `--plan`, `--one-task-difficulty`, `--no-collapse` and `--approved`, which
`auto` consumes. `auto` starts a new plan dir. One that already has `tasks.tsv` is refused.

## Plan shape

After the last wave computation, `plan` records `plan-shape.json`: `shape`, `tasks`,
`waves` and `widest`. `one` is a single task. `chain` is two or more tasks whose every
wave has one. `branches` is a plan with a wave of two or more. Recording it is advisory:
a failure is a note, and the table is still printed. `one` and `branches` add nothing to
the table. A chain prints:

```
this plan runs sequentially: N tasks, at most 1 in parallel; each pays its own implementation and review. One task would run once: re-plan with --collapse-chains
```

`--collapse-chains` is this `plan` call only, and it is not kept. It runs after the gates
(drift may have added files) and after the table is validated, and before routing. A chain
becomes one task, id `combined`:

- deps `-`; difficulty the highest of the parts (`low` < `medium` < `high`); files the
  union; dwarf and qa the value they share, or `-` when they differ. Routing then fills a
  `-`. A spec every part already named is kept.
- title `N parts: FIRST TITLE`, cut at 100 characters.
- the prompt starts "This task combines the N parts of a plan that could only run one
  after another. Do them in order, in this one task; each part's requirements and
  acceptance criteria still hold." Then `## Part i: TITLE (id)`, with that part's headings
  demoted one level, then one `## Acceptance criteria` whose `### Part i: TITLE` holds
  that part's criteria, or `- (no criteria were written for this part)`.
- approaches that exist become `tasks/combined/approach.md`, each under `### Part i`.

The original `tasks.tsv` bytes are kept as `tasks.precollapse.tsv`. Each `tasks/<id>/`
moves to `tasks.precollapse/<id>/`. `collapsed.json` is `from` (the ids in wave order),
`into` `combined`, and `shape`. A `groups.tsv` is kept as `groups.precollapse.tsv` and
rewritten: the parts become `combined`, and a group made only of parts is dropped (the file
goes when no group is left); `collapsed.json` then also has `groups`, with `kept_as` and the
`dropped` group ids (#257). A one-task or branching plan is left as it is. A plan that
already has `tasks.precollapse.tsv` or `collapsed.json` is not an error. The table
announces a collapse only when this call did it:

```
collapsed a sequential plan of N tasks into one (tasks.precollapse.tsv keeps the original)
```

`auto` adds `--collapse-chains` unless `--no-collapse`, so the sequential line shows when
you planned by hand, or when `auto` was told not to collapse.
