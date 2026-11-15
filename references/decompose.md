# Decomposed runs

Mechanics behind `--decompose-level`: how a goal becomes tasks, how tasks are routed to
dwarves by difficulty, how waves are computed, and what happens when something fails.

Read this when writing a decomposition, when routing does something unexpected, or when a
run leaves branches behind.

## Contents

- [The two axes: decompose-level and difficulty](#the-two-axes-decompose-level-and-difficulty)
- [tasks.tsv](#taskstsv)
- [Routing](#routing)
- [Profiles](#profiles)
- [Scripted tasks](#scripted-tasks)
- [Fractal within decomposed tasks](#fractal-within-decomposed-tasks)
- [Waves and disjointness](#waves-and-disjointness)
- [Groups and group review (opt-in)](#groups-and-group-review-opt-in)
- [Task size and acceptance criteria](#task-size-and-acceptance-criteria)
- [Plan gates: sent back, and the block under the table](#plan-gates-sent-back-and-the-block-under-the-table)
- [The capsule](#the-capsule)
- [Planning](#planning)
- [Plan shape](#plan-shape)
- [QA verdicts](#qa-verdicts)
- [Retro notes (opt-in)](#retro-notes-opt-in)
- [Brief mode (opt-in)](#brief-mode-opt-in)
- [Task summaries (opt-in)](#task-summaries-opt-in)
- [Severity, thresholds and known issues](#severity-thresholds-and-known-issues)
- [Retrying, and resuming an interrupted run](#retrying-and-resuming-an-interrupted-run)
- [Infrastructure failures](#infrastructure-failures)
- [Implementer guards](#implementer-guards)
- [Accepting, splitting, combining and the whole-run review](#accepting-splitting-combining-and-the-whole-run-review)
- [Chained plans](#chained-plans)
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

## Fractal within decomposed tasks

Select optional Fractal execution on `run`; planning the task table does not activate it.
`--decompose-level` defines the top-level Forge tasks and approval table. Each such task
can then use a Fractal implementation node with bounded children inside its declared `files`.
Each top-level task still returns its combined candidate to Forge's independent QA and
existing integration checks. Child completion never marks the Forge task accepted.

Use `--fractal-auto-decompose` on `run` to require recursive split-or-atomic decisions
automatically. Add `--fractal-planner <spec>` to use a stronger decision model for all
nodes; otherwise the saved top-level planner or each task's initial root model is reused.
Show that choice and the bounds in the approval table. See the
[decision contract](fractal.md#automatic-decomposition-decisions) for recovery and limits.

```bash
# With tasks.tsv, goal.txt, request.md and per-task prompts already prepared:
bash <skill_dir>/scripts/forge-parallel.sh plan "$PLAN" --repo "$REPO" --decompose-level medium \
  --dwarf sol:high --dwarf-low luna:medium --dwarf-high sol:xhigh,terra:xhigh --qa opus

# Preview the selected backend, pools, bounds and workspace without provider calls.
bash <skill_dir>/scripts/forge-parallel.sh run "$PLAN" --fractal --dry-run

# After approval of the task/model table:
bash <skill_dir>/scripts/forge-parallel.sh run "$PLAN" --fractal \
  --max-parallel 3 --fractal-concurrency 3 --fractal-depth 2
```

Add `--ui` to `plan` to open the plan in [forge-ui](ui.md): the `forge-ui:` URL is printed
under the task table, so the user can watch the later `run`. Ask once when the plan is made,
not on `run`, `retry` or resume; it is fail-open and not remembered.

`plan` saves tier pools in `fractal-routing.json`. The first enabled execution freezes
eligible specs, canonical resolutions and limits with the run and preflights every
eligible model/harness. Children route by difficulty, then the configured general dwarf,
then their parent's dwarf. A retry cannot introduce a model outside that frozen pool.

`--max-parallel` bounds top-level task pipelines. `--fractal-concurrency` separately bounds
all active model invocations across their trees, including QA. Parents release their model
slot while waiting; nesting cannot multiply that limit. Child dependencies must be satisfied,
ownership must remain inside the parent, and overlapping children are serialized.

The choice is persisted in `fractal-selection.json`. Explicit `--fractal` or `--no-fractal`
skips the once-per-interactive-run question; unattended execution without either is off.
Installation never activates the backend. The [Fractal reference](fractal.md) documents
all limits, prerequisites and the child-request protocol.

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

## Groups and group review (opt-in)

Two opt-ins share `groups.tsv`. A planner may name groups, and those are identity only
(`review=task`): they do not change dispatch or the estimate, and they are fine under Fractal.
`plan --groups` with `--group-review` is the other opt-in: one QA review covers several tasks.
Offer either; never add them unprompted. The user-facing tradeoff is in
[group review](../docs/reviews.md#group-review-opt-in). The data layer is `scripts/forge_groups`
and `scripts/forge-groups.py` (`validate`, `derive`, `resolve`, `freeze`, `check`, `of`, `show`).

### `groups.tsv`

Six tab-separated columns. `#` lines and blank lines are ignored.

```
# id	review	qa	members	title	goal
```

- `id` matches `[a-z0-9][a-z0-9-]{0,39}`, is unique, and is not also a task id.
- `review` is `task` or `group`.
- `qa` is a spec, or `-`.
- `members` is a comma list of task ids, in order. A task is in at most one group.
- `title` and `goal` are one line each, or `-` when absent. A planner's title and goal are
  at most 200 characters.

Where a row comes from:

- A planner reply that includes `groups` writes `groups.tsv` with `review` always `task` and
  `qa` always `-`. A planner group may have one member. The planner never sets `review=group`.
- `plan --groups waves` makes one group per computed wave that has at least two tasks, named
  `w1`, `w2`, and so on. A wave larger than the max splits in `tasks.tsv` order into `w3a`,
  `w3b`, and so on. A leftover single task is dropped.
- `--group-review` is only valid with `--groups waves`. Without it, `plan` exits 2:
  `--group-review goes with --groups waves (it sets the review of the groups waves derives)`.
  The derived groups then have `review=group`. High-difficulty tasks stay out and keep their
  own review. A leftover single is dropped.
- `plan --groups FILE` copies that file. An existing `groups.tsv` and no `--groups` is used
  as it is.

`plan` computes waves, then validates, resolves each review group's `qa`, and freezes.

### Rules for `review=group`

These apply only when `review` is `group`. A `review=task` group is not checked this way.

- At least two members, and at most `--group-max-tasks` (default 6, plan file
  `group_max_tasks`). The flag accepts a positive integer. Deriving waves requires at
  least 2 (`max tasks must be at least 2`). Validating an existing file uses the same max,
  so 1 rejects every review group.
- No member depends on another member, directly or through a task outside the group.
- Declared `files` do not overlap. A directory covers the paths under it, `.` covers
  everything, and `-` declares nothing. Same rule as the scheduler.
- No sweep member (an id `sweep-N`, or `tasks/<id>/sweep.json`).
- `qa` of `-` becomes the QA spec of the highest-difficulty member (`high` over `medium`
  over `low`; a tie takes the first listed), as `tasks.tsv` has it after `plan`.

### The freeze, and what makes `run` refuse

`groups.frozen` is the sha256 of the freeze text: one line per group, all six columns, then
one line per member, `member<TAB>deps<TAB>files`. The dwarf column is not in it. No groups
removes `groups.frozen`. A `groups.frozen` with no `groups.tsv` fails the check.

`run` calls `forge-groups.py check` when `groups.tsv` or `groups.frozen` exists, a dry run
included, before any dispatch. A mismatch, a missing file, or groups that cannot be read
refuses with `groups.tsv or a member's deps/files changed since plan; run plan again`.

Group review and Fractal do not combine. `--fractal`, or a plan that is already fractal,
makes `plan` and `run` exit 2: `group review does not support Fractal yet`.

### The member lifecycle and `IMPLEMENTED`

A `review=group` member implements, writes `reviewed.commit` and `source.fingerprint`, and
stops at `IMPLEMENTED`. There is no per-task QA snapshot, no shadow review, no fail-first
check, and no QA-only resume (the `resume` marker is removed). `--pre-review-tests` still
runs, on a snapshot that exists only for that check. Red is `FAIL`, unreviewed, not
`IMPLEMENTED`.

`record_attempt` writes no `attempts.tsv` row while the status is `IMPLEMENTED`, and
`post_attempt` does nothing. The attempt cap counts `attempts.tsv` rows (an `INFRA` row
does not count), so that stop spends nothing. The `attempt` counter may already have moved.

`retry` and `accept` of an `IMPLEMENTED` task exit 3: `<id> is implemented and waits for
group <gid>'s review`. The scheduler does not dispatch that member again. A dependent waits
for `MERGED`, so it does not start.

### When a group is reviewed

The scheduler treats each `review=group` group as one unit, keyed `group:<gid>` in `active`.
It is ready when every member is `IMPLEMENTED`, nothing is running it, it has not merged, and
its status is empty or `WAITING`, or `FAIL` with `settled.round` equal to the current
`round`, or this run has marked it to review again. It starts as soon as the last member is
`IMPLEMENTED`. There is no wave barrier. It takes one slot, and it does not start while the
run is stopping, draining, or paused for infrastructure. The command is
`forge-parallel.sh _group`.

### The candidate, and the size bound

The candidate worktree is `<wt_root>/_group-<gid>`. It starts at the integration HEAD, then
merges each member's `reviewed.commit` with `--no-ff`, in member order.

- A member that is not `IMPLEMENTED`: the group is `WAITING`. Members stay as they are.
- A member whose branch tip is not `reviewed.commit`, or whose worktree fingerprint is not
  `source.fingerprint`: that member becomes `INVALIDATED` with `invalidated.reason`
  `group-candidate`, and the group is `WAITING`.
- A conflict between members: the group is `CONFLICT`, `conflict.files` lists the files, the
  merge is aborted, the members stay `IMPLEMENTED`, and nothing is dispatched.
- Any other merge failure, or no integration HEAD: `ERROR`.

The fingerprint is the sha256 of the sorted `task=commit` lines plus a final
`tree=<candidate tree>` line.

`combined.diff` is `git diff --binary --irreversible-delete` of the candidate against that
base, excluding `.forge`. Over `group_max_diff_kb` times 1024 bytes (default 256 when the
plan file is absent), the group is `NEEDS_DECISION` with reason `oversized`. Nothing is
dispatched, truncated, or sent to another reviewer. `decision.md` starts `reasons: oversized`
and offers two edits, then `plan` again: smaller groups in `groups.tsv`, or this group's
`review` set to `task`. `forge group retry` refuses an oversized group.
`--group-max-diff-kb N` is a positive integer, written only when given, so a re-plan keeps it.
The same is true of `--group-max-tasks`.

This is not a [whole-run review](../docs/reviews.md#whole-run-review-opt-in). That one reads
the combined result after the run and truncates. A group review that is over the bound never
builds a prompt.

### The prompt and `{tasks=…}`

One fresh QA dispatch. The prompt is the contract head for kind `group` (the task preamble,
plus that this review covers several tasks and must judge each against its own criteria and
judge the combined change), the group's title and goal, `Members, in merge order`, then any
findings from the previous round that named no task. Each member follows: title, `prompt.md`,
the declared files and the reviewed commit. When the task has them, it also has the intended
approach, that member's diff `--stat` against its base, pre-review text, guard notes and
drift. Findings the previous round attributed to the task are quoted, and the implementer's
reply is included when this attempt is not the one those findings judged. The tail is the task contract plus
the `{tasks=…}` instructions below, then the whole combined diff in a fence. The diff is not
truncated. An oversized group never gets here. The threshold is the plan's QA threshold.

`{tasks=…}` is its own brace. It is not the facts brace
`{reach=… origin=… evidence=…}`. The prompt says to write it immediately before the facts
brace, so the facts brace still ends the line. The last `{tasks=…}` on a finding wins. Ids
are separated by commas or spaces.

Attribution (`scripts/forge_groups/attribute.py`, written to `attribution.json` with the
round):

- A tag names those ids. An empty tag (`{tasks=}` or `{tasks= , }`) names nobody and does
  not fall back to the file.
- With no tag, the tasks are the members whose changed paths contain the finding's file, in
  member order. A leading `./` is stripped and nothing else is normalised: `src/./a.py` does
  not match `src/a.py`.
- Ambiguous when the tag names an id that is not a member, when the tag names nobody, when
  there is no tag and no file, or when no member changed that file.
- `blocking` is the gate's per-finding decision at the threshold (`none` is P3; unset is
  P2). `PLAUSIBLE` never blocks.
- `implicated` is the union, in member order, of the tasks on blocking findings that are not
  ambiguous. The top-level `ambiguous` list is the `n` of findings that are both blocking
  and ambiguous.

The facts line is `implicated<TAB>ambiguous<TAB>findings<TAB>blocking`. The two lists are
comma separated, or `-`. `findings` is the count. `blocking` is `1` or `0`.

### Each outcome, and what happens next

The round ends in `groups/<gid>/status`, and one `rounds.tsv` row: round, epoch, status,
implicated tasks or `-`, QA spec. Status writes are atomic.

- **`PASS`.** The scheduler runs `_merge_group`: one `--no-ff` merge of `candidate.commit`
  onto integration (`forge: merge group <gid> (ids)`). Every member is judged `PASS` (one
  `attempts.tsv` row), its status becomes `PASS`, `merged` is touched, and the group's
  `merged` is touched. A conflict sets the group and every member to `CONFLICT`, keeps
  `conflict.files`, and merges nothing. Integration moved (`accepted.integration` does not
  match) or dirty: the group is `INVALIDATED` with `integration-moved` or
  `integration-dirty`, and nothing merges. A member whose branch tip is not its reviewed
  commit, or whose worktree fingerprint is not `source.fingerprint`: those members are
  `INVALIDATED` with `group-candidate`, and the group is `INVALIDATED` with
  `candidate-moved`. A member that is no longer `IMPLEMENTED`, or a recomputed fingerprint
  that does not match, invalidates the group with `candidate-moved` and merges nothing;
  members are not marked `INVALIDATED` unless they moved. A member dependency that is not
  `MERGED` blocks the merge and leaves the status unchanged. `forge group retry` on `PASS`
  refuses; the next `run` merges it.
- **`FAIL`.** `_group_settle` writes each implicated member's `qa.last` (a header, the
  blocks attributed to it, `Also involves:` when a finding names other members too, then
  `FORGE_VERDICT: FAIL`), runs the QA gate, sets the status to `FAIL`, writes one judged
  attempt row, runs `post_attempt`, and writes `settled.group` (`gid round`). A member that
  is not implicated stays `IMPLEMENTED`: no `qa.last`, no attempt row, no gate. The group
  writes `settled.round`. The scheduler requeues the `FAIL` members when `--retry-failed`
  budget remains. Without it they stay `FAIL` and the others stay `IMPLEMENTED`. The next
  round starts when the implicated members are `IMPLEMENTED` again. Settling the same round
  twice does nothing. A `FAIL` that implicates no member settles nothing.
- **`NEEDS_DECISION`, ambiguous.** A blocking finding names no member. Members stay
  `IMPLEMENTED`. `decision.md` starts `reasons: ambiguous`. The user names the members with
  `forge group retry PLAN GID --tasks a,b`. That runs `_group_settle --tasks`, which fails
  those members, each with the ambiguous findings (marked as assigned by a decision), and
  also any member the review implicated by its own blocking finding, with only that. It moves
  `decision.md` to `decision.<round>.md`, and sets the group to `FAIL`. The next `run
  --retry-failed` retries those tasks. A live scheduler picks them up on its sweep when
  they have budget left. Do this only when the user says so.
- **`NEEDS_DECISION`, oversized.** Refused by `forge group retry`. Split `groups.tsv` and
  run `plan` again, or set that group's `review` to `task` and run `plan` again. The reason
  word is the first token after `reasons:`. Only `oversized` and `ambiguous` are handled.
  Any other reason is refused; read `decision.md`.
- **`UNKNOWN`.** No verdict. Members stay `IMPLEMENTED`. The scheduler reviews the group
  again within `--retry-failed`, counted in `groups/<gid>/retries.used`. No member attempt
  is spent. With the budget spent, the group stays `UNKNOWN`.
- **`INFRA`, `TIMEOUT`, `ERROR`, `INTERRUPTED`.** `INFRA` is dispatch exit 8: `infra.txt`
  has `stage=qa` and the class, and no attempt is spent. `TIMEOUT` is exit 7. Any other
  dispatch failure is `ERROR`. `forge group retry` on `UNKNOWN`, `INFRA`, `TIMEOUT`,
  `INTERRUPTED` or `ERROR` sets the group status to `INTERRUPTED` and touches no member
  file, so the next `run` reviews it again. `--tasks` is refused in those states. A review
  that dies while `REVIEWING` is marked `ERROR`. A scheduler killed mid-review leaves
  `REVIEWING`; the next `run` turns that into `INTERRUPTED` and reviews again, without
  re-implementing the members.
- **`INVALIDATED`.** A tracked change to the candidate or to the review snapshot during the
  review. An untracked file in the snapshot is not a change. Also the merge-time reasons
  above. The next `run` reviews it again, unless `invalidated.reason` is
  `integration-moved` or `integration-dirty`. `forge group retry` says the same and does
  not rearm it.
- **`CONFLICT`.** `forge group retry` refuses. Two paths differ. A conflict while building
  the candidate leaves the members `IMPLEMENTED`, and `retry` of an `IMPLEMENTED` task
  exits 3, so the printed `forge-parallel.sh retry` line does not apply until a member is
  no longer `IMPLEMENTED`. A conflict merging onto integration sets every member to
  `CONFLICT`, and a `CONFLICT` task can be retried.
- **`WAITING`.** A member is not `IMPLEMENTED`. **`REVIEWING`.** A review is running.
  Neither is retried with `forge group retry`.

### Attempt accounting

Only a judged attempt writes an `attempts.tsv` row, and the cap counts those rows. An
`IMPLEMENTED` member spends nothing. A member a `FAIL` does not implicate spends nothing.
A re-review after `UNKNOWN`, `INFRA`, `TIMEOUT`, `INTERRUPTED` or `ERROR` spends no member
attempt. On `PASS`, every member gets one judged `PASS` row, including members the findings
did not name. A later `FAIL` of only some members records one `FAIL` row for each of those,
at the attempt number they are on. Separately, a task attempt left `STALE` (`prompt.md` or
`approach.md` changed while it ran) is not judged. `attempts.tsv` records `STALE`, and the cap
skips that row, as it skips `INFRA`.

### Resume

`run` again does not re-implement a member left `IMPLEMENTED`. A `PASS` group is merged,
with no new QA dispatch; a stale member branch is not merged. An unsettled `FAIL` is
settled, then those members are requeued. `INTERRUPTED`, `INFRA` and `TIMEOUT` are reviewed
again. `INVALIDATED` is reviewed again unless the reason is `integration-moved` or
`integration-dirty`. `UNKNOWN` is reviewed again while the group's `--retry-failed` budget
remains; otherwise the status line is printed. `NEEDS_DECISION`, `CONFLICT` and `ERROR` wait
for a person. The summary says `forge: <id> is implemented, waiting for group <gid>
(<status>)` for a member still `IMPLEMENTED`. An empty group status is printed as
`not reviewed`. A dependent of an unmerged group stays `BLOCKED`.

### Files in `groups/<gid>/`

`status` (then `PASS`, `FAIL`, `NEEDS_DECISION`, `UNKNOWN`, `CONFLICT`, `INFRA`, `TIMEOUT`,
`ERROR`, `INVALIDATED`, `WAITING`, or `REVIEWING`; `INTERRUPTED` is written by the scheduler
or by `forge group retry`), `round`, `rounds.tsv`, `candidate.base`, `candidate.commit`,
`members.tsv` (round, task, reviewed commit, attempt; one row per member of a round that
built a candidate), `fingerprint`, `combined.diff`, `members.paths` (`task<TAB>path`),
`attribution.json`, `qa.input`, `qa.last`, `qa.out`, `qa.gate`, `qa.round-<n>.last`,
`known_issues.md`, `infra.txt`, `decision.md`, `conflict.files`, `review.base`, `merge.out`,
`group.out` (the scheduler's log). Settle writes `settled.round`, `settle.tasks` (until the
round is settled) and `decision.<round>.md`. Merge writes `merged` and, when it refuses,
`invalidated.reason` (`integration-moved`, `integration-dirty`, `candidate-moved`).
`retries.used` counts `UNKNOWN` re-reviews. The candidate worktree stays at
`<wt_root>/_group-<gid>`.

On the member: `reviewed.commit`, `source.fingerprint`, `settled.group`, and
`invalidated.reason` of `group-candidate` when the member moved. Plan files: `groups.tsv`,
`groups.frozen`, `group_max_tasks`, `group_max_diff_kb`.

The dataset records `group_review` and `group_merge`. The QA dispatch has `scope` `group`
and an empty task, and it is left out of the per-task reviewer statistics. A judged member
attempt is linked with `review_scope=group`, `group` and `round`, and does not keep a copy
of that review's `qa.last` or `qa.gate`. See
[the dataset](dataset.md#raw-events-not-training-examples).

`forge group show PLAN [--group GID] [--json]` reads this. `forge group retry` is the
decision and the rearm above. Exit codes and flags are in
[commands](../docs/commands.md#the-forge-command).

### Out of scope

- Shadow reviews of groups. A member skips shadow, and the group dispatch is one fresh QA.
- Fractal. `group review does not support Fractal yet`.
- A reviewer session across rounds. The group dispatch does not continue a session.
- Fail-first on a member. Pre-review tests still run, as above.

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
`into` `combined`, and `shape`. A one-task or branching plan is left as it is. A plan that
already has `tasks.precollapse.tsv` or `collapsed.json` is not an error. The table
announces a collapse only when this call did it:

```
collapsed a sequential plan of N tasks into one (tasks.precollapse.tsv keeps the original)
```

`auto` adds `--collapse-chains` unless `--no-collapse`, so the sequential line shows when
you planned by hand, or when `auto` was told not to collapse.

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
  a pin, `retry --dwarf`) starts fresh;
- the plan is not Fractal.

A continue the harness refuses or that fails at once (dispatch exit **9**, nothing changed in the
worktree) is followed by a fresh dispatch of the same prompt, which decides the attempt. One that
fails after it already changed files is a failed implementer (`ERROR`), as a fresh one would be:
running another on top would pay for the attempt twice. One row per attempt goes to
`tasks/<id>/implementer_sessions.tsv`: row, attempt, `continued`, `fresh` or `failed`, the reason
(`same-spec`, `first-attempt`, `no-findings`, `work-changed`, `no-session`, `invalid-session`,
`spec-changed`, `harness-unsupported`, `fractal`, `continue-unsupported`, `continue-failed`) and
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
- **Scope:** parallel mode only, and never under Fractal.

`./forge dataset status` reports the agreement rate, overall and per model pair, and
`./forge dataset pairs` splits it by what differs between the two reviewers: effort within one
model, another model of the family, another family, and controls. For each pair of settings it
gives verdict agreement with a 95% interval, the blocking findings only one side found, and the
token and time ratio (see [the dataset reference](dataset.md#paired-reviews)). The export
adds `shadow` and `consensus` to the matching review cases' metadata.

## Retro notes (opt-in)

`plan --retro` makes each agent record what got in its way: a sandbox that blocked a command, a
tool that is not installed, an instruction it could not follow, a forge fault. A note says what
happened and who can fix it. Without a flag a plan keeps its earlier choice, and a first plan
takes the user setting (`forge config set retro on`). `plan --no-retro` turns it off.

- **What an agent writes.** Most tasks have nothing to report, and a hard task is not a note.
  A note is one `FORGE_RETRO: {json}` line in the final message. The planner, QA and the
  whole-run review write it the same way. A dwarf blocked before it finishes also appends the
  same JSON to `.forge/retro-inbox.jsonl` in its worktree, which forge reads when the dispatch
  ends. The fields are `type` (`environment`, `sandbox`, `tooling`, `prompt`, `forge-bug` or
  `other`), `fixable_by` (`user`, `forge` or `unsure`), `summary`, `evidence` (the command, its
  exit code and the error, up to 2 KB), `blocking` and `tried`.
- **Checked against the log.** forge looks for the note's command and the first line of its
  error in that dispatch's log. When both are there the note is `verified`; otherwise `claimed`.
- **Orchestrator notes.** `forge retro note PLAN --type T --fixable-by F --summary S` records
  one note of the run's own, with `--command`, `--exit`, `--error`, `--blocking` and `--tried`
  as needed. It is always `claimed`, and it does not check the retro setting.
- **Where it goes.** `<plan>/retro/notes.jsonl`, `tasks/<id>/retro.json` for a task, and one
  `retro_note` dataset event per note. `forge retro report PLAN` writes `<plan>/retro.md`.
  Notes with the same type and the same normalised command form one pattern; a note with no
  command is matched on its normalised summary instead. Each pattern
  goes to the group its majority `fixable_by` names: **You can fix this on this machine**
  (`user`), **forge should fix this** (`forge`), or **Not sure who should fix this** (`unsure`,
  also on a tie). A run prints one line with the pattern count and the path when there are any
  patterns. The first plan with retro on says what is recorded, once per user.
- **Filing.** `forge retro issues PLAN` drafts one redacted issue per `forge` pattern, for
  forge's own repository, and posts nothing. The dry run prints the texts and their hash.
  `forge retro issues PLAN --apply --approve HASH` posts only when the hash matches those texts
  and the `--choice rN=new|comment:N|skip` choices. It needs an authenticated `gh`, and it
  refuses a never-send repository. Both refusals exit `3`.
- **Never memory.** Notes never go into `.forge/memory.md` or the ledger, and no prompt reads
  them back. A retro plan does add a short instruction block to each prompt, at its end.
- **Off.** With retro off (`--no-retro`, no setting, or `FORGE_RETRO=off`) no instruction is
  added to any prompt, the inbox is not read, and forge stores no note from a dispatch. Its
  prompts stay as they were without the feature. `forge retro note` still records a note when
  it is called, because it does not check the setting.

Files it adds: `retro_mode` (plan file), `retro/notes.jsonl`, `retro/.lock`, `retro.md`,
`tasks/<id>/retro.json`, and `.forge/retro-inbox.jsonl` in a worktree (removed once its notes
are stored). It costs no model call of its own.

## Brief mode (opt-in)

With `plan --brief advisory|gated|strict --brief-model SPEC`, a read-only side dispatch writes
the questions each task needs answered before it starts. Each question has a default. A task
with no answer follows the default. The user answers with `forge brief answer`.
A task-scoped answer goes into that task's prompt. A `wave` or `plan` answer stays parked
until an approved proposal applies its diff. forge never answers for the user.

- **Cost.** One dispatch per task that has no `brief.json`, at effort `low`, four at a time.
  A reply that is not valid JSON, or has the wrong shape, is retried once; a second failure
  stores no questions and the reason in `brief.json`'s `error`. Answers, stale flags and
  `reject` call no model. A proposal is one more side dispatch, kind `brief-change`. Each
  unstarted task whose questions are regenerated is one more, kind `brief`. Both are read-only,
  at effort `low`, on the brief model.
- **When.** `run` writes the missing briefs when it starts. `strict` waits for them; `advisory`
  and `gated` write them alongside the scheduler. `forge brief write PLAN [--task ID] [--jobs N]
  [--force]` does the same by hand, and `--force` redoes a task that already has a brief.
- **Levels.**
  - `advisory`: the run does not wait. Open questions reach the prompt under "Open questions:
    follow the stated default", with each default.
  - `gated`: a task with an unanswered must-answer question is held. The run prints
    `forge: <task> waits for your answer to <qid>` with the `forge brief answer` line. A task
    whose brief is still being written waits too.
  - `strict`: `run` exits `3` before any task starts while a question of an unstarted task is
    open. It prints each question, its default and the answer command. Answer them and run again.
- **Answers.** `forge brief answer PLAN TASK QID TEXT` (or `--file F`; `-` reads stdin). For a
  task-scoped question, an answer given before the task starts goes to `brief.answers.json` and
  into the prompt under "Decisions the user made", which overrides what the requirements say.
  After the task starts, that answer goes to `brief.late.json` and changes nothing in the run.
  The command then prints the `tasks.tsv` row, the `prompt.md` a follow-up needs, and the
  `forge control add` line that queues it.
- **Scope.** `--brief-scope` is `task` (the default), `wave` or `plan`. The question's own
  scope decides what an answer may change. `task` writes the answer into that task. `wave` and
  `plan` questions are parked in `brief.parked.json` whatever `--brief-scope` says, and they
  change the plan only through a proposal you approve. Both scopes use the plan's
  [groups](#groups-and-group-review-opt-in). A wave proposal needs the question's group in
  `groups.tsv`. When the task belongs to a group, the question
  writer is told that group's title, goal and the other tasks in it. It asks `wave` when the
  answer would change how several tasks of the group are done, and `plan` when the answer
  would add, remove or split tasks, or change their order. A wave question from a grouped
  task records `"group": "<gid>"`. A plan question from a grouped task records `"group": null`.
  A task in no group keeps the prompt it had before groups, and its parked questions have no
  `group` key. With `--brief-scope wave` or `plan` and no `groups.tsv`, forge prints one note
  and goes on. `brief.groups.noted` keeps that note to once per plan. The note says to run
  `plan --groups waves`, or to let the planner write groups, because a wave question with no
  group stays parked. `gated` and `strict` still read only the task questions in `brief.json`.
  A parked question does not hold a task.
- **Parked answers.** A parked question can be answered when the brief scope is at least as
  wide as the question. `wave` answers wave questions. `plan` answers wave and plan questions.
  `task` answers neither: the question id is unknown, and `answer` exits `2`. The answer goes
  to `tasks/<id>/brief.parked.answers.json` as `{qid: {text, actor, t}}`, including after the
  task has started. It is never a late answer, and it does not change a prompt. The command
  prints `forge brief propose PLAN TASK QID`. Nothing in the plan changes until that proposal
  is approved. `forge brief show` lists parked questions by group: `Group <gid> (<title>):`
  and the question, or `Group <gid>:` when the title is `-` or the group is gone, then
  `Plan:` for a plan question. A question with no `group` key stays under
  `Parked (wider scope):`.
- **Proposals.** `forge brief propose PLAN TASK QID` asks the brief model what that parked
  answer changes, in one read-only side dispatch of kind `brief-change`, and stores the result
  under `proposals/<pid>/` (`p1`, `p2`, and so on). Each directory holds `proposal.json`,
  `diff.patch` (a unified diff of the plan files it would change) and `hash`. `state` is
  `proposed`, `refused`, `approved` or `rejected`. A reply that breaks the rules below, or
  that changes nothing, is stored as `refused` with its reason and its diff, and `propose`
  exits `3`. `apply` leaves a refused proposal where it is. A failed dispatch stores nothing.
  The question's scope is the proposal's scope: under `--brief-scope plan`, a wave question
  is still a wave proposal. A plan question needs `--brief-scope plan`, or `propose` refuses
  it. `forge brief proposals PLAN` lists every proposal, oldest first, with its state, scope,
  group, summary and diff. `--json` prints the same records. A `proposed` one also prints the
  exact `forge brief apply` line. `forge brief reject PLAN PID [--reason TEXT]` sets
  `rejected`. It touches no plan file. Only a `proposed` or `refused` proposal can be rejected.
- **What each scope may change.**
  - `task`: the answer, as under Answers. There is no proposal.
  - `wave`: `prompt.md`, `approach.md` and the `files` column, and only for unstarted members
    of the question's group. The question must name a group that is in `groups.tsv`. A change
    that names another task, a started member, or any other field refuses the whole proposal.
    A wave question with no group cannot be proposed.
  - `plan`: the same three fields on any unstarted task, new tasks, removal of unstarted
    tasks, and new dependency lists for unstarted tasks. A split is a removal plus the added
    tasks, and every task that still depended on a removed one needs its deps rewritten in
    the same reply. An added task's group is null or an existing group id. Anything that
    would not be a plan `plan` could have written refuses the whole proposal. Between runs,
    an added row gets dwarf and qa `-`.
- **Approve by hash.** `hash` is the sha256 of `diff.patch` and the current contents of every
  target. For a wave `files` change the target is that task's `tasks.tsv` row. For a plan
  change the hash also covers `tasks.tsv`, `groups.tsv` and every edited prompt or approach.
  `forge brief apply PLAN PID --approve HASH` must quote the hash `proposals` shows. A
  different hash, a state other than `proposed`, a target that has started, or a target that
  changed since the proposal, refuses the apply and writes nothing. After a target changes,
  propose again.
- **Live and between runs.** While a scheduler is live, a wave proposal that changes only
  prompt and approach is written. The next attempt of an unstarted task reads those files.
  A wave proposal that changes `files` is refused while the run is live, and nothing is
  written: the scheduler keeps the files column it read at start. With no live run, a wave
  `files` change is written, `groups.frozen` is re-frozen (a stale marker left by a plan
  proposal stays stale), and `waves.tsv` is recomputed when that file already exists. A
  plan apply with no live run writes `tasks.tsv` and the task directories, and `groups.tsv`
  when membership changed. A removed task's directory moves to `tasks/.removed/<id>-<pid>/`.
  `groups.frozen` becomes the marker `stale: plan changed by proposal <pid>`, so `run`
  refuses until `forge-parallel.sh plan` validates, routes and freezes again. With a live
  run, a plan proposal that includes an edit, a removal or a dep change is refused, and
  nothing is written. A proposal that only adds tasks queues each one with
  `forge control add`, in dependency order. A task added that way joins no group. It needs
  dwarf and qa specs copied from a task that already has them. If a later add is refused,
  the tasks already queued stay, and the proposal is `approved` so it is not applied again.
- **Affected tasks.** After a successful apply, every task the proposal changed or added, and
  every task that depends on a changed, added or removed task, directly or further down, gets
  `tasks/<id>/brief.affected`. The marker is `{pid, reason, t}`. `reason` is `changed`,
  `added` or `dependent`. An unstarted task's questions are regenerated, one brief dispatch
  each (`write --force`). An answer whose question text is still in the new brief is kept.
  Any other answer moves to `brief.answers.prev.json`. A task that has already started gets
  the same marker with `flag_only`, and its questions stay. `forge brief show` lists a marked
  task as `affected by <pid>`, and adds `flagged (already started)` when `flag_only` is set.
  `apply --no-regenerate` writes the same markers and does not dispatch. Regenerating one
  task later is `forge brief write PLAN --task ID --force`. The next approved proposal that
  touches a task replaces that task's marker. Earlier proposals stay in `proposals/`.
- **Stale.** After a task merges, a merge that changed files outside that task's declared files
  flags the open questions of its dependents that have not started (`possibly_stale`). `forge
  brief stale PLAN TASK` does this by hand.
- **Model.** `--brief-model SPEC` is required with brief on, and it must resolve; otherwise
  `plan` exits `2`. forge never picks the model.
- **Privacy.** A never-send repository gets no brief dispatch (`never-send`), so its tasks have
  no questions and nothing holds them.
- **Off.** With `--brief off`, no setting, or `FORGE_BRIEF=off`, there is no writer, no hold and
  no prompt addition. `forge-prompt.py` adds nothing without a `brief` file. `forge brief write`,
  `forge brief show` and `forge brief propose` exit `3` for such a plan.

Files it adds: the plan files `brief`, `brief_scope` and `brief_model`; `brief.out` (the
writer's output), `brief.pid` (while the writer runs) and `brief.groups.noted` (once, when
wave or plan scope has no groups); `proposals/<pid>/proposal.json`, `diff.patch` and `hash`;
and per task `tasks/<id>/brief.json`, `brief.parked.json`, `brief.answers.json`,
`brief.late.json`, `brief.parked.answers.json`, `brief.affected` and `brief.answers.prev.json`.
A removed task's directory is moved to `tasks/.removed/<id>-<pid>/`. Its events are
`brief_question`, `brief_answer`, `brief_diff` and `side_dispatch` (kind `brief` or
`brief-change`). A `brief_diff` event is `{pid, scope, group, state, tasks}` and is written
when a proposal is proposed, refused, approved or rejected. The approved event from a
successful apply also carries `affected` and `flagged`, the task ids it marked.

## Task summaries (opt-in)

`plan --summaries on --summaries-model SPEC` writes a short summary of each task to
`tasks/<id>/summary.json`. The model sees only the task's facts: its status, its attempt trail
(attempt, dwarf spec, verdict, findings count, worst severity, retry reason), the latest review's
finding titles and severities, its known issues, and its dependencies and dependents. The facts
are capped at 8 KB. The model never sees the repository, the diff or the task prompt.

- **Cost.** One read-only dispatch per task, at effort `low`, when its facts change. Unchanged
  facts are not dispatched again, because `inputs_hash` matches the stored summary. Each kind
  of side dispatch is capped per run at `max(10, 3 × tasks)`.
- **When.** During `run`, the scheduler starts a detached `forge summarize PLAN --task ID` for
  each task whose status, dwarf reply, attempt trail, verdict or retry count changed since its
  last look. The first look at a task only remembers it. The scheduler never waits for a
  summary, and a failed one changes nothing in the run. On a finished run, `forge control set
  PLAN summaries on` starts `forge summarize` itself, with its output in `summaries.out`.
- **Output.** `summary.json` holds `text`, `sentence`, `model`, `generated_at`, `inputs_hash`
  and `sources` (the attempts and findings the facts came from). The prompt asks for 3 to 5
  short lines. A reply that is not a JSON object with `text` and `sentence`, or whose text runs
  past 8 lines, is rejected: the previous `summary.json` stays and `summary.error` says why.
- **Commands.** `forge summarize PLAN [--task ID] [--force] [--json]`. `--force` dispatches even
  when the facts did not change. `--json` prints one object per task: `task`, `status`, `reason`.
  `forge control set PLAN summaries on|off` sets the plan. `--task ID on|off|inherit` sets one
  task; its own `on` or `off` overrides the plan, and `inherit` drops that override.
- **Model.** `--summaries-model SPEC` is required with summaries on, and it must resolve;
  otherwise `plan` exits `2`. Set a user default with `forge config set summaries.model SPEC`.
- **Off.** With `--summaries off`, no setting, or `FORGE_SUMMARIES=off`, there is no dispatch and
  no `summary.json`.

Files it adds: the plan files `summaries` and `summaries_model`; `summaries.out`; and per task
`tasks/<id>/summary.json`, `summary.error` (after a failed reply) and `tasks/<id>/summaries`
(`on` or `off`, the task's override). Its events are `side_dispatch` (kind `summary`) and the
`side_summary` role in the dataset.

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
every task has merged, and before the combined verification, the scheduler runs `forge-sweep.py plan`
once. It collects the open findings at `Pn` or worse (the ones the threshold tolerated, plus
PLAUSIBLE ones with `--sweep-plausible`), with `forge issues`' own collector, and leaves out:

- findings a person accepted with `accept` or kept with `adopt --approved` (listed as `accepted`), and
  findings of fix tasks (one round: what a sweep review finds goes to `forge issues`, never into another
  sweep);
- dismissed ones (`forge issues --dismiss`), ones with no file, and ones whose file is gone;
- ones whose line, as the reviewer saw it, a later merge changed (`changed`, naming those merges).

Every finding left out is listed in the plan with its reason. The rest become fix tasks, one per file,
worst first, at most 8 findings each and `--sweep-max N` (default 5) tasks. Each fix task is a row
`sweep-<n>` in `tasks.tsv` with `prompt.md` and `sweep.json`. Its implementer is the plan's low tier
(the first spec of `--dwarf-low`, else of `--dwarf`; checked at `run` start), else the implementer of
the task the worst finding came from. Its reviewer is that task's reviewer. What an adopted range's review
tolerated is swept too: its fix task's reviewer is the adoption's, and below the low tier its implementer
is the run's first task's. The plan (groups, findings,
dispatches) is printed and kept in `<plan>/sweep/plan.md` and `plan.json`. A plan is swept at most once.

Fix tasks share the run's scheduling, controls and `--retry-failed` budget. Their review is narrow
(`forge-contract.py qa --kind sweep`): one `S<n>: FIXED` or `S<n>: NOT FIXED` line per finding, plus
regressions as findings. It passes when at least one finding is fixed and there is no blocking
regression. When a fix task merges, its review's lines go to `tasks/<id>/sweep.outcome.json`, and
`forge issues` leaves out what it fixed (`swept` in `--json`). A fix task that fails leaves its
findings for `forge issues` and does not fail the run. The run's summary says what the sweep fixed,
and the whole-run review that follows reads the sweep's changes like any other task's.

`run --sweep-ahead` (kept in `<plan>/sweep_ahead`; `--no-sweep-ahead` turns it off) is the cheaper
first step. A task dispatched after others merged is told the open findings in files it owns (at
most 3, picked once in `tasks/<id>/sweep.ahead.json`), to fix only on its way, and its reviewer is
told the same. It adds no dispatch. A later sweep counts the ones whose line changed as `changed`.

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
  the same code twice buys nothing. A retry that only rejects the findings, and leaves
  that diff unchanged, is still this identical retry.

`retry` is a command a human types. Forge does not loop a dwarf against its own reviewer on
its own initiative; the one exception is the opt-in `run --retry-failed N` below.

**`UNKNOWN` and `INFRA` resume at the review stage.** If the reviewer never gave a verdict (or
its dispatch stopped on an infrastructure failure after the dwarf finished), the work was not
rejected, so `retry` / `--retry-failed` re-runs **QA only** on the unchanged, already-committed
work: no dwarf, no new commit, no attempt spent. A `tasks/<id>/resume` marker records this and is
revalidated against the branch tip and the saved source fingerprint before it is trusted; if the
worktree changed, the full task runs. Findings are carried only from a real review.

On a retry whose diff changed, QA receives the previous round under **Findings from the
previous review round**, the diff since that round (`tasks/<id>/changes.since.diff`,
`changes.since.range` and `changes.since.stat`; omitted when the earlier commit is
missing or not an ancestor, or when nothing changed) and the implementer's reply. It is
told to restate only findings that are still open, as `- [Px][CONFIRMED|PLAUSIBLE]`
lines, because every labelled line is counted. That is the retry's review, not the
QA-only re-run above.

**In-run retries.** `run <plan> --retry-failed N` re-queues a task whose reviewer said
`FAIL`/`UNKNOWN`, or whose review was `INVALIDATED` by a change to the review copy, as soon as it ends, up to `N` more times in that run, preparing the retry exactly
as `retry` does (same findings carry-forward, same pinned base, same cumulative review). Other
tasks keep running; dependents wait for the task to merge. It applies per invocation (not
remembered), never to `TIMEOUT`/`ERROR`/`CONFLICT` or an `INVALIDATED` merge, and never after a retry that
changed nothing (`tasks/<id>/noretry`). Every entry is logged in `tasks/<id>/attempts.tsv`.
An attempt that carries retry findings also gets one row in `tasks/<id>/retry_sources.tsv`:
the attempt number, the source of those findings (`review`, `prereview`, `bounce`, `message`,
`answer` or `prompt`) and the epoch time. A row for the same attempt replaces the earlier one, because an
infrastructure stop refunds the attempt and its restart runs the same number again. forge-ui's
attempt trail reads this file.

**A prompt edited during the attempt.** When `dwarf.input` is built, `prompt.md` and
`approach.md` are copied to `prompt.dispatched` and `approach.dispatched`. A missing
`approach.md` is an empty copy, and `prompt.dispatched` is written last: its presence is what
says the pair is complete. If the copy fails, `prompt.dispatched` is removed and a later edit
is not detected. An attempt from before those copies is not checked. Before review,
if either file differs, the attempt is `STALE` and no review runs. `stale.diff` is the unified
diff, `control.log` gets action `prompt-changed` with actor `scheduler`, and the console names
the file and says no attempt was spent. The attempt counter is given back, as a dwarf-stage
infrastructure stop is. The cap skips the `STALE` row, the escalation streak treats `STALE` as
neutral, and no decision is written. Unless the run is stopping, the scheduler re-dispatches at
once with `retry_findings.source` `prompt`. `retry_findings.md` is the new requirements plus
that diff, and the next entry reuses the attempt number. `changes.diff` and
`changes.prev.diff` are removed, so a re-dispatch that keeps the committed work is reviewed
rather than refused as identical (`stale.diff` and `committed.diff` stay). A `STALE` task still
waiting at the next `run` is prepared the same way, with no `--retry-failed` required and no
retry counted. `forge control retry` accepts `STALE`; `--dwarf` is refused, because the same
implementer gets the new text. A plain `forge-parallel.sh retry` does not take this path. The
dataset attempt event has `prompt_changed` (true when the status is `STALE`). `forge dataset
routing`, tiers and the forecast leave those attempts out,
the same way they leave out a refunded or infrastructure stop. The `prompt` blob is
`prompt.dispatched`, the text the implementer was given; an attempt from before the copy falls
back to `prompt.md`. An empty `approach.dispatched` stores no approach blob.

**Escalation after repeated failures.** In forge's history the first three attempts at a task
pass review at about the same rate (43%, 42%, 47%), and the fourth and later at 29%, whether
or not the model changed. So a retry does not repeat a dispatch that has already failed three
times on one model (`--escalate-after N` on `run` or `retry`, kept in the plan, default 3, `0`
turns it off):

- **Counting:** the count is the task's failed attempts (FAIL, or an implementer TIMEOUT) on its current model,
  from the fifth column of `attempts.tsv`, after the latest row of `tasks/<id>/decisions.tsv`.
  Infrastructure stops, missing verdicts, `STALE` attempts and forge's own errors judge nothing and are skipped; a pass
  or a change of model resets it. A lateral move does not: the streak carries across `tasks/<id>/lateral.tsv`.
- **Escalation:** at the threshold, the next attempt goes to a different model in the next tier
  up of the pools given to `plan` (same position in the pool, as with exploration),
  recorded in `escalations.tsv` and `tasks.tsv` like a `retry --dwarf`.
- **Stop:** when nothing is above (the model is in the top tier, or in no tier pool at
  all), the task stops as `NEEDS_DECISION` with reason `split` (see [When reviews don't converge](#when-reviews-dont-converge)).
  `tasks/<id>/split.suggested` still says to split it or name a model. `run --retry-failed` does not retry it,
  and a plain `retry` refuses (exit 2).
- **Effort step** (`--escalate-effort` on `run` or `retry`, kept in the plan as `escalate_effort`;
  `--no-escalate-effort` removes it; off by default): when no higher tier has another model, the
  same alias and harness is tried once more at the next effort its pairing accepts
  (`forge-dispatch.sh efforts --spec <spec>` lists them, weakest first), counted from the effort
  the last dispatch really ran after any clamp; a spec that names no effort counts as `medium`.
  The streak then counts per effort, so the new effort starts from zero. At the pairing's highest
  effort the task still stops, and `split.suggested` says that effort was tried too. Turning the
  step on (flag or `forge control set escalate-effort on`) clears the stop markers, so tasks the
  policy already stopped are decided again at their next retry.
- **Overrides:** an explicit `retry --dwarf <spec>` is the human's choice and skips the policy, even
  with the same model. The dwarf column itself cannot tell a pin from a pool pick, so it does
  not; `--escalate-after 0` does.
- **Fractal plans** keep their frozen eligible set and are not escalated; neither is a
  QA-only resume, which spends no implementer attempt.
- **Reporting:** the policy's state is decided after every attempt, so a run whose retry budget ran out
  still reports `its next retry goes to <spec>` or the split suggestion under the results.
  A pending pin is reported ahead of both, as `its next retry goes to <spec> (pinned)`, or
  `(pinned; after the QA-only re-run)` only when the next entry really re-runs QA (the
  `resume` marker, or an unreviewed verdict, still matches the worktree). A marker the
  next retry will discard is not that wait.

**A budget per model.** `plan --retry-per-model N` writes `retry_per_model` (`N` from 1 to 99).
`--no-retry-per-model` removes it; a re-plan with neither keeps the file. A task may fail N
times on the model it is on, then the next attempt moves one rung up the pools and the count
starts again. With `escalate_effort` on, the same N applies to each effort of that model before
the next effort. `forge-route.py escalation` uses `retry_per_model` as the trigger when the
file is a number from 1 to 99, instead of `escalate_after`. When both files exist, `run` notes
that retry-per-model replaces `--escalate-after` for this plan; the `escalate_after` file stays.
Automatic retries still need `--retry-failed` above 0 (or a live `retry_budget` above 0): any
positive budget turns the ladder on, and that number is not the limit. A person's
`decided.left` replaces the ladder: a positive count retries even when `forge-route.py per-model`
says no, and 0 does not. `retry=N` with `--dwarf` keeps the task on that model until the larger of N
and the per-model count. `max_attempts`, the converging stop, `noretry`
and `escalation.stop` still apply. `forge-route.py per-model` prints whether one task may be
attempted again (`retry`, `rung`, `failed_on_rung`, `rungs_left`, `per_model`; exit 2 when the
plan has no valid file). `worst-case` is the unmerged tasks' dispatches (implementer, reviewer,
and shadow reviews when those are on) and the tasks whose `N` times rungs exceed
`max_attempts`. The plan table prints `retry per model: N`, `worst case with retry-per-model N:
X dispatches`, and `max-attempts … stops these tasks before the top of their ladder` for that
last list. Pools `low`, `medium` and `high` with N=3 give a task that starts on `low` up to 9
implementer attempts, bounded by `--max-attempts` (default 8). A `script` implementer has no
rungs above it; its worst-case implementer count is 0.

History cannot yet say whether escalating rescues a task (31% of the 29 later attempts on a
changed spec passed, mostly effort bumps); the attempts this policy makes are what will.

## When reviews don't converge

A task can fail review after review, each round with new findings. forge keeps a findings ledger
at `tasks/<id>/ledger.tsv`, one row per review: epoch, attempt, review, verdict, gate, worst,
confirmed, new, carried, diff_bytes, delta_bytes. A CONFIRMED finding is carried when the previous
review named the same problem; the rest are new. The diff sizes are `changes.diff` and
`changes.since.diff` as they stand when the row is written.

The signal looks at the last reviews. The count is the plan file `converge_window` when that file
holds a number (`plan --converge-window N`, or `forge control set converge-window N` later), and
**4** otherwise. It fires when every review in that window failed (verdict
FAIL, or the gate turned a PASS into a fail) with at least one CONFIRMED finding, at least half of
the findings in the later reviews are new, and the worst severity is no better in the last review
than in the first. A window below 2 never fires. On past runs, a window of 4 stopped no task that
merged by attempt 4. Rows from before the latest `tasks/<id>/decisions.tsv` entry do not count: a
decision restarts the window, and the escalation streak with it.

Task and solo reviews are asked to list every CONFIRMED finding, and to say so plainly when the
diff is correct, so a round is the whole list and not only the worst few.

The task then stops with status **`NEEDS_DECISION`**. So does a task the retry policy cannot
escalate further (reason `split`; `split.suggested` still names the suggestion), a task at its
attempt cap (reason `cap`), and a `FAIL` whose every blocking finding needs a file the task does
not declare (reason `plan`). The cap is `plan --max-attempts N`, default **8** when `max_attempts`
is absent, counted across runs: an infrastructure stop spends nothing, a `STALE` attempt spends
nothing, and each `retry=N` a person grants adds N to the cap. `0` is no cap, and a grant does not create one. `forge control set max-attempts N --confirm`
changes it; `--confirm` applies only to that key. A `NEEDS_INPUT` task at the cap keeps its question:
raise the plan's cap before the answer can start an attempt.

**Reason `plan`.** After a judged `FAIL`, and before the escalation check, the task stops when
`qa.last` holds a real `PASS` or `FAIL` verdict, at least one finding blocks at the task's
threshold, every blocking finding carries `{outside=…}`, and `forge_owner.py` says none of
those paths is covered by this task's declared files (a declaration of `.` covers every path;
`files` of `-`, or an unknown task, leaves every path outside). A `PLAUSIBLE` finding never
blocks, and a finding milder than the threshold does not. The check writes
`tasks/<id>/outside.json`: a JSON list of `{path, owner, owners, owner_status}`. `owner` is the
task that declares the path, or null when none does; `owners` is every such task; `owner_status`
is `MERGED` when that task has `merged`, otherwise the text of its `status`, or `PENDING` when
it has none, and null when there is no owner. If that does not hold, `outside.json` is removed
and escalation runs as usual. `decision.md` lists each path as `path — declared by <id> (<status>)`
or `path — declared by no task`, then the plan choices, which edit `tasks.tsv`: add the path to
this task's `files`, make this task depend on the owner (edit its `deps`), or merge the two
rows into one task. Then `forge-parallel.sh plan <plan>` again when no run is live, and
`forge-parallel.sh retry <plan> <id> --decide retry=1 --reason "…" --approved`. The usual
choices (more attempts, a threshold, a named model, accept, split) stay listed. This `FAIL`
counts toward the cap. It does not escalate, and `--retry-failed` does not re-queue
`NEEDS_DECISION`, so it spends no further attempt on the model. The reviewer's `{outside=…}`
is what stops the task. The implementer is told not to edit a file outside its `files`, and to
end with `FORGE_BLOCKED: <path>: <why>`, one line per path. Those lines are copied into
`qa.input` as a claim ("The implementer says these fixes need files outside the task"), for the
reviewer to confirm with `{outside=<path>}` or refute. Task, solo and group reviews are taught
the brace; sweep, adopt and final reviews are not. The brace is its own, immediately before the
facts brace: `- [P1][CONFIRMED] path/file.py:123 — {outside=a.py,b.py} {reach=main origin=new evidence=ran}`.

`tasks/<id>/decision.md` gives the reasons and the choices. Nothing else resumes the task: not a
re-run, not `--retry-failed`, not a retry budget and not a pin. A plain `retry` exits 2.

`retry` holds the plan lock until its attempts end, so to resume several tasks side by side add
`--no-dispatch` to each. It records the decision, carries the findings forward and starts nothing;
the task is then `PENDING`, and one `forge-parallel.sh run <plan>` dispatches them all under
`--max-parallel`, with dependents starting as their dependencies merge. It works on any `retry`,
not only a decision, but takes no `--retry-failed` or `--infra-retries`: give those to the `run`. A
second `--decide` on a prepared task is refused; a `--dwarf` changes its model and keeps its count.

The choices:

- `forge-parallel.sh retry <plan> <id> --decide retry=N --reason "…" --approved` gives N more
  attempts, N from 1 to 20. At the cap this raises the cap by N, and it is the only choice that
  runs the task again while the attempts already spent sit on the cap.
- `forge-parallel.sh retry <plan> <id> --decide threshold=Pn --reason "…" --approved` sets this
  task's blocking threshold (`P0`, `P1`, `P2` or `P3`) and then retries once.
- `forge-parallel.sh retry <plan> <id> --dwarf <spec> --reason "…" --approved` names a model for
  one attempt. Add `--decide retry=N` for up to N attempts: the retry policy keeps the task on that
  model until they are used, and they end early the way any retry does, on a pass or when the reviews
  stop converging. Or add `--decide threshold=Pn` to set the threshold too. `decisions.tsv` records it
  as one row, such as `retry=2 dwarf=sol:high`.
- `forge-parallel.sh accept <plan> <id> --reason "…" --approved` merges it; the findings become
  known issues.
- Split it by hand: `tasks/<id>/decision.md` and the run summary say to replace its row in
  `tasks.tsv` with smaller tasks, then `forge-parallel.sh run <plan>`, which reads that file
  again and recomputes `waves.tsv`. `forge control drain` and `add` do not do it: they need a
  scheduler that is still live (exit 3 once `scheduler.live` is gone, which it is by the time
  the summary is printed), and `add` queues a new row without replacing the stopped one, which
  stays `NEEDS_DECISION` for that invocation (its dependents keep the `deps` loaded at the
  start, and the run exits 5 while the row is unmerged).

  Take the stopped task's row out of `tasks.tsv`. Leave `tasks/<id>/` where it is; its diff
  is not on the integration branch. A row left in place is not dispatched. Append one row
  per smaller task, seven tab-separated columns: `id`, `deps`, `difficulty` (`low`, `medium`
  or `high`), `files` (non-empty), `dwarf`, `qa`, `title`. Copy the stopped task's `dwarf`
  and `qa` (explicit specs; `forge control add` refuses `-` and `UNASSIGNED`, and `run`
  preflights every spec). `deps` is `-`, a task that is already `MERGED`, or another new
  task. On every remaining row whose `deps` named the stopped task, put those new ids in
  its place: a dependency that is no longer a row makes `run` die with `dependency cycle or
  unsatisfiable deps`, and one that never merges leaves the dependent `BLOCKED`. Write a
  non-empty `tasks/<id>/prompt.md` for each new id. The next `run` skips `MERGED` work and
  dispatches the new rows from the integration branch as it stands, not from the stopped
  task's worktree. While a scheduler is still live, `forge control add` can queue a row
  written this way whose dependencies that run already knows; the row that replaces the
  stopped task takes effect on the `run` after the scheduler has exited.
  `forge-parallel.sh split` moves whole tasks into a new plan
  ([below](#accepting-splitting-combining-and-the-whole-run-review)).

A live scheduler takes the same decision as `forge control retry <plan> <id> --decide …` (or
`--dwarf`), and an accept as `forge control accept <plan> <id>`, each with `--reason` and
`--approved`; that only queues the request. An accepted task merges and its dependents start in
the same run (#138). The answer is appended
to `tasks/<id>/decisions.tsv` (epoch, attempt, choice, reason). The run summary lists these tasks
under `waiting for your decision:`. A run that ends with such a task unmerged exits 5.

**Live controls.** `forge control show|set|pin|unpin <plan>` changes a run while it works:
`qa-threshold` (read once per attempt, so an attempt keeps the bar it started with; per task, `none`
(P3) exempts it from a looser plan threshold and `inherit` drops the override; at plan level, `default` restores P2), `timeout`, `escalate-after`, `escalate-effort`
(`on|off`), `verify-retries`, `infra-backoff` (a shape as for `--infra-backoff`; `none` drops it,
read at the next infrastructure wait), `infra-fallback` (`on|off`, at the next infrastructure stop), `max-attempts` (a non-negative integer, `0` = no cap, and only with `--confirm`), `converge-window` (a non-negative integer), and, only while a scheduler is live, `retry-budget` (plan
or `--task`), `infra-retries` and `max-parallel` (1 to 64, at the next dispatch decision; a lower
limit lets running tasks finish and never stops one). The budgets
keep the **per invocation** rule: the flags of `run` are not remembered, and a live scheduler only
mirrors them into `retry_budget`, `infra_budget` and `tasks/<id>/retry_budget` (re-read at every
decision, removed when it stops; `tasks/<id>/retries.used` stays), so a change dies with the
run. `pin <task> <spec>` sets the model the task's next full retry uses, once (checked with
`forge-dispatch.sh doctor`, no quota): `prepare_retry` claims `escalation.pin` as
`escalation.pin.applying`, rewrites `tasks.tsv`, appends the `escalations.tsv` row with
`source=pin`, clears `escalation.stop`/`split.suggested`, then consumes the pin. A symlinked
`escalations.tsv`, or a symlink in any directory from the plan down to it, is not written:
the pin stays and nothing is retried. A QA-only resume leaves it for the next full retry;
Fractal plans never apply pins; a MERGED task refuses one.
The last column of `escalations.tsv` says who decided: `policy`, `retry` (an explicit
`--dwarf`) or `pin`; four-column rows read as `policy`. Older rows may say `fallback`.
A sideways move is recorded in `lateral.tsv`, not here (see [Infrastructure failures](#infrastructure-failures)).
Every change goes to `control.log` (JSON lines: `at`, `actor` `cli|ui`, `action`, `task`, `key`,
`value`, `previous`) and to the dataset as a `control` event. Exit 2 invalid input, 3 refused (not
live, MERGED, Fractal, failing doctor, lock busy over 5 s). Report them; never set one yourself
unless asked.

Five more commands steer a live scheduler (exit 3 when none is live):

- `drain <plan>` starts nothing new: running tasks finish, the scheduler waits (it prints
  `drained — N task(s) waiting` once) and infrastructure waits keep counting down. `resume <plan>`
  ends the drain. This is the supported way to pause a run; nothing has to be signalled. A drain
  holds only tasks still to start: when none is left (or waiting on infrastructure), the run
  ends as it would without the drain, verification included.
- `stop <plan>` starts nothing new and ends the run once the running tasks are done: a waiting
  infrastructure pause ends too, unstarted tasks stay `PENDING`, verification is skipped and `run`
  exits 130. When every task has merged by then, nothing was cut short: the run ends as usual,
  verifies and exits 0. `stop --now` interrupts the running tasks
  as SIGTERM would.
- `retry <plan> <task> [--dwarf <spec>]` and `add <plan> <task>` only queue a request, one JSON
  file in `control.requests/`. At its next pass the scheduler applies or refuses it and appends
  an outcome line to `control.log` with the same `request` id (`actor` `scheduler`, `outcome`
  `applied|refused`, `reason`). `retry` takes a `FAIL`, `UNKNOWN`, `TIMEOUT`, `ERROR`,
  `BLOCKED`, `INTERRUPTED` or `STALE` task that is neither running nor waiting. A `STALE`
  task is re-dispatched with the changed prompt on the same implementer, and `--dwarf` is
  refused. The others are prepared exactly as `forge-parallel.sh retry` does. A
  `NEEDS_DECISION` task is resumed only by `--decide` or `--dwarf`, each with `--reason` and
  `--approved`. It spends no automatic retry budget; an `INFRA` task is the scheduler's own
  to resume. `add` takes a row the human appended to `tasks.tsv` (with its
  `tasks/<id>/prompt.md`) whose dependencies this run already knows, and refreshes `waves.tsv`.
  A request never outlives the scheduler that was live when it was queued; one left behind is
  refused, and every request is refused once the run is stopping.

**Ordinary execution resume:** run `run` again. Tasks already `MERGED` are skipped. A saved PASS is fingerprint-checked and merged without
redispatch. PENDING, `INFRA` (paused by an infrastructure failure) and newly unblocked tasks run
when eligible. A `STALE` task still waiting is prepared with the new prompt and spends nothing.
A `NEEDS_DECISION` task is not among them: only a decision resumes it. Failed or interrupted tasks
require an explicit `retry` (or `--retry-failed N`); `run` never silently pays for them again. Worktrees and pinned
baselines are retained. A per-plan advisory lock excludes concurrent run/retry/integrate
operations. A `review=group` member left `IMPLEMENTED` is not implemented again; its group's
review resumes as in [Groups and group review](#groups-and-group-review-opt-in).

From another session or harness, `forge handoff resume <plan>` follows a live scheduler and restarts a stopped run with its recorded flags; see [From another session or harness](../docs/decomposed-runs.md#from-another-session-or-harness). The `run` and per-task `retry` above remain the way to finish by hand.

**Interrupts.** A SIGINT or SIGTERM to `run` (or to its scheduler), and `forge control stop
--now`, first reap the tasks that already ended (a PASS still merges), then send TERM to each
running task's process group, wait up to `FORGE_INTERRUPT_GRACE` seconds (default 10) for the
group to empty and KILL what is left. Only those live tasks become `INTERRUPTED`; their worktree
keeps the partial work. Each guarded dispatch records its harness in `tasks/<id>/<role>.harness`
(pid and start time), and `forge-guard.py stop` ends a harness that outlived its dispatch, at
the interrupt and again at the next scheduler start (a scheduler killed outright leaves tasks
`RUNNING`; the next run stops their harnesses and marks them `INTERRUPTED`). An interrupted
`run` skips verification and its rerun, prints `forge-parallel.sh retry <plan> <id>` for each
interrupted task, releases the plan lock and exits **130**. A signal that arrives during
verification stops the verify command's whole process tree (TERM, then KILL after a few
seconds) and records `verification.status` `INTERRUPTED`; that verification is not rerun.

**Fractal resume:** discover the managed ID with `<skill_dir>/forge fractal runs --repo
"$REPO" --json`, then use `<skill_dir>/forge fractal resume RUN_ID`. Run-level resume
recovers execution and the recorded Forge pipeline, retaining accepted stages and pinned
baselines. A control scoped with `--task` or `--task ... --node ...` only resumes that
execution scope; it does not relaunch the full pipeline. Use `retry "$PLAN" TASK_ID` for
an explicitly requested new attempt, with the existing backend, bounds and eligible pool.
Pause, stop and inspection preserve both the work and diagnostics.

## Infrastructure failures

A usage window, a spent budget, an auth problem, a rate limit, a provider outage, a network error,
an empty answer or a cancelled dispatch says nothing about the work. The status stays **`INFRA`**.
`forge-dispatch.sh` classifies the stop from the dispatch log (strong phrases and structured
`is_error`/`turn.failed` results only; a reply that otherwise succeeded is never reclassified) and
exits **8**, writing `<role>.infra` (`class=`, `retry_after=`, `reset_at=` when the wait is known,
`detail=`, `rc=`, `role=`, `harness=`, `model=`). `do_task` copies that file to
`tasks/<id>/infra.txt` and adds `stage=dwarf|qa`. The class is a symptom. What happens next is the
cause, from `CAUSES` in `scripts/forge_infra.py`. A run recorded before the split may say `quota`;
that name is recovered as `usage_limit`, and the summary still prints the stored word.

- a **dwarf-stage** stop refunds the attempt counter;
- a **QA-stage** stop keeps the attempt (the dwarf's work is committed) and writes the `resume=qa`
  marker, so the next entry re-reviews without running the dwarf.

| Cause | Scope | Move | Without a peer | Hint |
|---|---|---|---|---|
| `capacity` | model | lateral | wait | — |
| `rate_limit` | model | wait | wait | — |
| `usage_limit` | harness | lateral | wait_if_reset | wait for the reset, or add another harness to this tier's pool |
| `budget` | harness | lateral | ask | add credits, or check the billing of {harness} |
| `auth` | harness | lateral | ask | log in to {harness} again |
| `network` | all | wait | wait | — |
| `provider_down` | harness | lateral | wait | — |
| `empty` | model | retry | wait | — |
| `cancelled` | model | retry | ask | check {harness}'s approval and permission settings |

**Scope** is what the pause holds back. `model` pauses that model on that harness
(`model:<harness>/<model>`); `harness` pauses that harness's account (`harness:<harness>`);
`all` pauses everything, so running tasks drain and nothing new starts. A model or harness cause
with no harness, or a model cause with no model, pauses everything. Other tasks keep running. A
pending task whose next dispatch needs a paused pair waits; unstarted tasks stay `PENDING`, never
`BLOCKED`.

**Move** is what happens first. **lateral** sends the work to another model of the same tier —
another harness or account when the scope is a harness, another model when the scope is one model —
and never a stronger tier. **wait** pauses the scope until the cause clears and spends one of that
scope's waits. **retry** dispatches the same model again at once and spends no wait; the next stop
of that same cause is treated as lateral.

**Without a peer** is the recovery when no model can take the work. **wait** is the pause above.
**wait_if_reset** waits when the reset is known and inside six hours, and asks otherwise; if this
task did move sideways, the scope waits rather than asking. **ask** pauses the scope, marks it
**needs you** with the hint (`{harness}` filled in), and spends no wait. Nothing on that scope is
retried until a person acts. Raising the infra-retry budget above the value it had when it asked
starts a wait after all. "Needs you" is that end of the recovery: no peer, and the cause will not
clear by itself.

**Lateral recovery** is on for a new plan. `plan` writes `infra_fallback` as `on`;
`plan --no-infra-fallback` writes `off`, and a re-plan keeps the file as it is. A plan with no file stays off. `run --infra-fallback` and
`run --no-infra-fallback` write the file, and `forge control set infra-fallback on|off` applies at
the next infrastructure stop. It runs at the dwarf stage and at the QA stage, and not on a Fractal
plan:

- a **dwarf-stage** stop takes the next healthy model of the tier the task is running in: the tier
  recorded at its newest assignment (the plan's pick, explored or not, an escalation, a pin, a
  `retry --dwarf`, an earlier move), as long as it still runs that model; otherwise its difficulty.
  A re-plan that keeps a row's model keeps its tier. A pinned implementer is not moved. No attempt
  is spent.
- a **QA-stage** stop takes a reviewer of the same strength from `qa-routing.json`: the next member
  of the task's difficulty pool, then of `any`, on a pair nobody paused. Never the stopped
  reviewer's model, and never the implementer's. The attempt and the `resume` marker stay, so the
  next entry is QA only. A reviewer no such pool lists, or a plan with no `qa-routing.json`, has
  no peer. A pin waits for the next full retry.

Each move is a row of `tasks/<id>/lateral.tsv` (epoch, attempt, role `dwarf` or `qa`, from, to,
cause) and a `lateral` dataset event. It is not an escalation: it is not written to
`escalations.tsv`, and the escalation streak carries across the dwarf moves. A pool with no healthy
member keeps the ordinary pause.

Without `--infra-retries`, `run` ends once nothing else can run, prints what to fix and exits `8`;
verification is skipped, and running the same command again resumes. `run --infra-retries M` waits
out each scope instead, up to `M` waits **per scope** (a stop in a scope already paused joins its
wait and is not counted again). A scope whose waits are used up stays paused. An asking scope
spends none of those waits.

Each wait is the provider's retry-after when the log carried one (capped at 6 h), otherwise the
backoff shape for that wait number. `--infra-backoff <shape>` on `run` (kept as `infra_backoff`,
read at every wait, so `forge control set infra-backoff` applies to the next one) takes
`exp:<base>[,<cap>]` (base doubling, capped at `<cap>`, default the larger of the base and 30 min),
`linear:<start>,<step>[,<cap>]`, or `list:<a>,<b>,...` (a bare `<a>,<b>,...` is a list; the last
value repeats), all in positive seconds. The default is `exp:60`; `FORGE_INFRA_BACKOFF` changes
that base. The wait message names the source, for example `waiting 120s before resuming (backoff
exp:60, wait 2 of 3)`. While anything is paused, `infra.paused` (JSON `{"scopes": [...]}` with
`scope`, `class`, `harness`, `model`, `tasks`, `cycle`, `until`, `exhausted`, `needs_you`, `hint`,
`reset_at`) says what waits and until when. It is removed when nothing is paused, and again when
the scheduler exits, so a summary printed after the run reads `infra.txt` alone.

The run summary and `forge control show` name the cause, the reset when it is known
(`resets HH:MM`, with the date when it is not today), and **needs you** with the hint. The
summary's line is `  <task> <cause> on <harness>/<model> (<stage> stage)`, then the reset and
`— needs you: <hint>`, and a move is `moved sideways: <task> <role> <from> → <to> (<cause>)`.
`forge control show` prints the cause on the task's status (`INFRA (cause)`) and, while the
scheduler is live, on each pause line. forge-ui shows the same facts; the words it uses are in
[forge-ui](ui.md).

A dwarf that exits 0 with an empty final message *and* an empty diff is `class=empty`: it never
really ran. Under Fractal a dwarf-side stop still ends as Fractal's own `failed`; QA-side stops
behave as above, and `scripts/forge_fractal/pipeline.py` resets `INFRA` to `PENDING` on resume.

A planned pause (a provider maintenance window, a quota reset) is `forge control drain`, not a
signal: running tasks finish, nothing new starts, and `forge control resume` continues the same
run.

## Implementer guards

forge records what it observed about how each dwarf behaved, never what the dwarf claimed:
HEAD or the checked-out ref moved (**self-commit / branch switch**), processes left alive in the
harness's session after it exited, or started by it in a session of their own (**orphans**, listed in `dwarf.orphans` and stopped; disabled by
`FORGE_ORPHAN_GUARD=off` or under Fractal), and a final message that **promises later work**.
They are written to `tasks/<id>/guard.txt`, added to the QA prompt as "Implementer guard notes" and
shown in the run summary. They never change a status. The dwarf prompt itself tells the
implementer not to commit, not to start background processes, to finish every measurement before
its last message, and to say plainly what it could not run.

## Accepting, splitting, combining and the whole-run review

Four subcommands act on plans after tasks have run. Full usage and refusal conditions are in the
[README](../docs/reviews.md#accepting-a-task-despite-its-reviewer); the mechanics:

- **`adopt <plan> <commit|a..b> --reason …`**, or **`adopt <plan> --head --reason …`** (each with
  `--qa <spec>`, `--yolo-qa`, `--approved`), brings a commit
  made outside forge into the run instead of leaving `review` refusing. The first cherry-picks onto the
  integration branch. `--head` takes what is already on it past `accepted.integration`, and it is what
  `review`'s refusal names. The run's QA (`--qa`, then `final_qa`, then the first task's) reviews that
  range alone against the reason, and no implementer runs. PASS, or a verdict the threshold accepts, moves
  `accepted.integration`, adds a row to `adoptions.tsv` (epoch, base, tip, verdict, approved, spec, review,
  reason) and to `control.log`, writes an `adopt` dataset event, and verifies as a retry does. A FAIL or
  UNKNOWN puts the branch back and exits 5, unless `--approved` keeps it, which writes the findings to the
  adoption's `known_issues.md` as `accept` does; `forge issues` and the run's report read that file (as
  accepted findings, the reason with them), and the sweep lists them as `accepted` without fixing them;
  what the review tolerated under the threshold the sweep fixes like a task's. The reviewer is told the
  run's verification command (or its lanes' commands), which runs only after the review. A review that concluded nothing always puts it back: a crash or timeout exits 5, an
  infrastructure stop exits 8, a signal exits 130 or 143. If the worktree changed during the review it is
  left as it is, with the commands to go on. A range that lived only on the branch stays at
  `refs/forge/adopt-refused/<dir>`. A merge commit is refused, and a range that does not apply exits 6. A
  diff over 400 KB is read in the snapshot rather than inlined, as in a final review.
  There is no task row, because no model implemented it; the final review reads the whole range and is told
  about it, `attribute` treats it as a step of its own, and forge-ui lists it above the final review, each
  row opening its review, its findings and the range's diff with the findings pinned.
  `adopt-<epoch>-<pid>/` keeps the review.
- **`accept <plan> <task> --reason … --approved`** overrides a `FAIL`/`UNKNOWN` reviewer only
  when the reviewed commit is still the branch tip and the worktree still matches
  `source.fingerprint`. It records `known_issues.md` and `accepted.by`, merges through `merge_task`,
  and restores the prior state if the merge fails. The orchestrator never runs it unprompted.
- **`review <plan> [--qa <spec>]`** and **`integrate --final-review <spec>`** run one QA dispatch
  over the combined result (`integration.base..tip`, or `parent..candidate`) in a disposable
  checkout with the goal, every task's requirements, known issues, drift, guard notes and the
  verification result in front of the reviewer. Same verdict/severity contract and
  threshold gate as per-task QA; a mutation during the review is `INVALIDATED`; a `FAIL`
  or missing verdict blocks `integrate` with exit `5`, an infrastructure stop with `8`, and the
  user's branch is never touched. Nothing runs unless one of them is invoked.
- **`split <plan> <new-plan> --tasks a,b`** copies the selected tasks' saved state to a new plan,
  renames their branches and worktrees to the new run id, starts a new integration branch at the
  source's accepted tip and removes them from the source (refuses Fractal-backed plans, `MERGED`
  or `RUNNING` tasks, and selections that strand a dependent).
- **`combine <new-plan> <plan>…`** merges the integration branches of fully merged plans into one
  new plan whose `run` only verifies. Task branches keep their original names.

`split`/`combine` hold the plans' advisory locks for their whole duration, and every failure path
removes what it created.

## Chained plans

`plan <dir> --after <prev>` starts `<dir>` from an earlier plan's integration branch. `<prev>`
is that plan's directory. See the [plan flags](../docs/commands.md#scriptsforge-parallelsh).

`plan` writes `<prev>`'s absolute path to `after` and the starting commit to
`integration.base`, and creates `forge/<run>-integration` at `<prev>`'s
`accepted.integration`. A branch that already points at that commit is kept. The approval
table prints `after: <run> (<base>)`, with the first 12 characters of that commit. A later `plan` of the same directory without `--after` keeps the
files and the branch. A chained plan skips the warning about uncommitted changes in the
repository: its worktrees start from the previous plan's accepted tip.

`<prev>` has to be a plan of the same repository. Every refusal below is exit 2, and it leaves
the plan directory and the branches as they were:

- `<prev>` is not a directory, `--after` was given no directory, or the directory has no
  `tasks.tsv`, `run_id` and `repo`
- the plan would come after itself
- the two plans belong to different repositories
- a task of `<prev>` is not `MERGED`
- `forge/<prev-run>-integration` is missing, `<prev>` has no `accepted.integration`, or the
  branch tip is not that accepted commit. Extra commits on the branch are adopted into
  `<prev>` first (`forge-parallel.sh adopt <prev> --head --reason "…"`)
- `<prev>`'s scheduler is live (`scheduler.live` names a pid that is still running)
- this plan's own `forge/<run>-integration` is at another commit: it is checked out, it is
  not still at the recorded `integration.base`, or this plan already has merged work

Before any merge, a different `--after` moves the branch when it is still at its old
`integration.base` and is not checked out. After a task has merged, a different `--after`
is the same exit 2, and the recorded base stays.

`run` checks again while no task of this plan has merged. If `<prev>` has no
`accepted.integration`, `run` notes that and goes on. Otherwise that commit must be an
ancestor of this plan's `integration.base`. If `<prev>` moved, `run` exits 3, dispatches
nothing, and tells you to run `plan <dir> --after <prev>` again. Planning again with the
same `--after` records the new tip. A plan split from a chained plan starts later than
`<prev>` ended; the old tip is still an ancestor, so the check passes. Once a task of this
plan has merged, `run` does not check again.

`integrate` merges this plan's integration branch, which already holds every plan it follows,
and prints `this brings plans A → B → C` with the run ids, oldest first. A predecessor
that cannot be read, or a chain longer than 64, makes that line start with `…`. A plan
with no `after` file prints no such line.

`forge runs` puts `after <run>` on the plan's summary line. `<run>` is the predecessor's run
id, or its key when that directory is gone. forge-ui shows After, a link to the plan this run
follows, and Followed by, the plans whose `after` names this run.

## Branches, worktrees, cleanup

```
<plan-dir>/tasks/<id>/approach.md               intended approach, if planned
<plan-dir>/retry_per_model                      failures on each model before the next rung (1-99); absent leaves escalate_after
<plan-dir>/tasks/<id>/prompt.dispatched         prompt.md as given to the implementer; written last at dispatch
<plan-dir>/tasks/<id>/approach.dispatched       approach.md as given; empty when there was none
<plan-dir>/tasks/<id>/stale.diff                unified diff when prompt.md or approach.md changed during the attempt
<plan-dir>/tasks/<id>/outside.json              paths a plan stop named, with owner, owners and owner_status
<plan-dir>/tasks/<id>/risk                      low|medium|high, recorded only (#122), if rated
<plan-dir>/groups.tsv                           id, review, qa, members, title, goal; absent unless groups are on
<plan-dir>/groups.frozen                        sha256 of the six columns, then each member's deps and files
<plan-dir>/group_max_tasks, group_max_diff_kb   review-group bounds; defaults 6 and 256 when the file is absent
<plan-dir>/groups/<gid>/                        one review=group group's review; see Groups and group review
<plan-dir>/tasks/<id>/reviewed.commit           commit a review=group member stopped at (also a task review's)
<plan-dir>/tasks/<id>/source.fingerprint        worktree fingerprint beside that commit
<plan-dir>/tasks/<id>/settled.group             gid and round, once a group FAIL judged this member
<plan-dir>/tasks/<id>/retry_sources.tsv         attempt, source, epoch: why each retried attempt ran (#133)
<plan-dir>/run.flags.json                       flags of this run of the plan, rewritten at each real start; not copied by split or rerun
<plan-dir>/handoff.notes.jsonl                  session notes of what is not on disk: at, actor, text; newest first in forge handoff
<plan-dir>/handoff.driver.json                  harness, session, since, last_seen of the session driving the run
<plan-dir>/tasks/<id>/interrupted-<epoch>.diff  uncommitted work saved before forge handoff resume resets a stale RUNNING worktree
<plan-dir>/resume-<epoch>.{log,done}            log of the run forge handoff resume started, and its exit code once that run ends
<repo>/.forge/                                  project memory (survives the run)
<repo>/../.forge-worktrees/<run-id>/<task-id>   task worktree
<repo>/../.forge-worktrees/<run-id>/_integration integration worktree
<repo>/../.forge-worktrees/<run-id>/_group-<gid> group-review candidate; kept for the merge
forge/<run-id>/<task-id>                        task branch
forge/<run-id>-integration                      integration branch
```

Fractal also keeps control repositories, isolated product candidates and per-step logs
under `${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/runs/<managed-run-id>/tasks/`.
These are separate from the Forge task worktrees listed above. Candidates begin at each
task's pinned baseline and return to its ordinary review/integration pipeline; Fractal
initialization metadata stays outside the product diff. Managed run/task IDs are shown by
`fractal runs` and `fractal tree`, and differ from the human-readable `tasks.tsv` IDs.

### Worktree setup

A fresh worktree is a bare checkout — no `node_modules`, no venv, no build output. Without help,
every dwarf in the run rediscovers that the same way (run the tests, watch them fail, work out the
install) and pays it again, which is both slow and a source of near-identical "this worktree has no
dependencies" entries in project memory.

Tell forge what a worktree needs and it runs it verbatim, once per worktree, from the worktree
root, at creation time only:

- `.forge/setup` in the repo — executable, or any file whose contents are a shell command;
- `--setup <command>` on `run` or `retry`, which overrides it.

A resumed run or a `retry` reuses the existing worktree and does not pay it again. The integration
worktree gets the same treatment, since that is where a merged result is validated — without it the
one checkout holding every task's merged work is the one checkout that cannot build it.

Failure is reported and non-fatal: the dwarf gets a worktree it can still install into itself,
which is the status quo this replaces. Output lands in `tasks/<id>/setup.out`.

**Submodules.** `git worktree add` leaves every submodule directory empty. Before the setup step,
forge fills them offline from the source checkout (no fetch, no `git submodule`), so `.forge/setup`
sees a complete tree. Each submodule gets one state in `tasks/<id>/submodules.txt`:

- `exported` — the recorded commit was extracted from the source's submodule repository;
- `copied` — that commit was not available, so the source checkout's files were copied;
- `kept` — the directory already held files (a resume), left as they are;
- `missing` — nothing was available; the directory stays empty and the runner prints a note.

The files carry no `.git`, so git ignores them: they never appear in `changes.diff`, a commit, a
review snapshot or a fingerprint, and the worktree still moves and is removed normally. The capsule
tells the dwarf which submodules are present. Integration worktrees (`run`, `integrate`, `combine`,
`split`) get the same treatment. Out of scope: a task that moves a gitlink's recorded commit (its
worktree keeps the old content) and nested submodules (left empty).

**Reviewers get the same dependencies.** A review snapshot holds committed files only, so forge
links the directories package managers install into (`node_modules`, `bower_components`, `.venv`,
`venv`, `vendor`, `.bundle`, `Pods`, Mix's `deps` and `_build`, `.dart_tool`, `elm-stuff` and
SwiftPM's `.build`) from the checkout under review (the task's
worktree, or the integration worktree for a final review) into the snapshot, at the root and in
workspace packages down to five levels, unless the snapshot already holds that path or lacks its
parent. These are the directories this step filled, so they match the attempt. The links count for
no fingerprint and stay out of the reviewer's `git status`. The prompt names what was linked and what
is committed, or says there is none, says when forge ran no tests of its own, and tells the reviewer
to report rather than search or install. A directory that is only partly committed (`vendor/.gitkeep`)
keeps its committed files and gets no link. Nothing guards a write
through a link: forge commits whatever git does not ignore, so a linked directory is an ignored one
(a directory the attempt committed is already in the snapshot and gets no link), and a write there
changes nothing that merges. A link dangles once its worktree is removed, which harms nothing. Each
reviewer ends with `FORGE_TESTS: RAN`, `NOT NEEDED — why` or `COULD NOT RUN — why`; the last prints a note, and
forge-ui and the dataset show it.

Forge does not guess this step, and the obvious guess is worth naming because it looks right and
is not: symlinking the source repo's `node_modules` into the worktree. In a workspace monorepo the
workspace links inside it point back at the source checkout, so the worktree builds against the
source repo's packages rather than its own — producing type errors naming files the task never
touched. Copying is correct but can be gigabytes per task. The repo knows which of those it wants;
forge does not.

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
| task status `UNKNOWN` | QA never emitted a verdict line; read `tasks/<id>/qa.last`. `retry` re-runs QA only, on the unchanged work |
| task status `INFRA`, `run` exits `8` | an infrastructure stop; the cause is in the summary and `tasks/<id>/infra.txt`. No attempt spent. Fix a "needs you" line before re-running; otherwise re-run, or wait with `--infra-retries M`. See [Infrastructure failures](#infrastructure-failures) |
| task status `NEEDS_DECISION` | reviews are not converging, the retry policy has no stronger model, every blocking finding needs a file the task does not declare (reason `plan`), or the attempt cap is spent. Read `tasks/<id>/decision.md`. Only a decision resumes it |
| task status `STALE` | `prompt.md` or `approach.md` changed during the attempt. Not reviewed, and no attempt spent. The scheduler re-dispatches it with the new text; so does `forge control retry`. See [Retrying, and resuming an interrupted run](#retrying-and-resuming-an-interrupted-run) |
| task status `IMPLEMENTED` | a `review=group` member finished implementing and waits for its group's review. `retry` and `accept` exit 3. See [Groups and group review](#groups-and-group-review-opt-in) |
| group status `NEEDS_DECISION` | the combined diff is over `group_max_diff_kb`, or a blocking finding names no member. Read `groups/<gid>/decision.md`. `forge group show` prints it; `forge group retry --tasks` only on the user's word, and only for an ambiguous decision |
| a group left `INFRA`, `TIMEOUT` or `INTERRUPTED` | the review did not finish. The next `run` reviews it again. `forge group retry` rearms it to `INTERRUPTED`. No member attempt was spent |
| a group left `UNKNOWN` | no verdict. The next `run` reviews it again while `--retry-failed` budget remains; otherwise it stays. `forge group retry` rearms it. No member attempt was spent |
| a group left `ERROR` | the dispatch failed, or the review ended while still `REVIEWING`. The next `run` leaves it. `forge group retry` rearms it to `INTERRUPTED`. No member attempt was spent |
| task status `INTERRUPTED`, `run` exits `130` | the run was signalled or stopped with `forge control stop --now`; verification was skipped. The summary prints `forge-parallel.sh retry <plan> <id>` for each one; a plain `run` does not dispatch them |
| a run that sits idle with tasks `PENDING` | `forge control drain` is in effect (`forge control show` says `drained`; `forge control resume` continues), every scope a task needs is paused on infrastructure (`infra.paused`), or `max-parallel` is lower than you think |
| a `FAIL` task merged anyway | the threshold (`P2` by default) tolerated its findings, or a person ran `accept`; see `known_issues.md` and `qa.gate` |
| a task `FAIL`ed although its reviewer said `PASS` | forge's gate: a CONFIRMED finding at or above the threshold was listed (`qa.gate` decision `fail`); the retry starts with a note saying so |
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

## Execution checks and integration verification

Before dispatch, `run` preflights every task's dwarf and QA plus a configured planner.
`retry` repeats preflight and dependency checks. A dependency must be MERGED, not merely
planned or QA-passing; otherwise its consumer is BLOCKED without spending a dispatch.
Directory/child ownership paths overlap and cannot run concurrently. A blocked task can be
retried after its prerequisites merge. A fresh task then branches from current integration.

QA receives complete implementation input, approach, capsule and cumulative binary diff.
It runs in a disposable repository. Both source and review fingerprints must remain unchanged,
and the task branch must still point at the reviewed commit when merged; the review copy is a
commit, so only its tracked content counts (a test run's untracked `__pycache__/` there changes
nothing). INVALIDATED results never merge; `retry` re-runs only QA while the work is unchanged. Disposable snapshots detect mutation; they do not confine unrestricted shell access.

Configure combined executable checks with `run <plan> --verify '<command>'` (also accepted by
`retry`), or a shell command file at `<repo>/.forge/verify`. Commands execute under Bash from
the integration checkout. Forge records `verification.command`, `.log`, `.exit`, `.fingerprint`
and `.status` in the plan directory. Failed checks or source changes fail the run. With no
command, the result is explicitly UNVERIFIED. A `retry` that leaves tasks still to run
(PENDING or BLOCKED) skips its verification and says so: the `run` that dispatches them verifies
the integration branch at its end, and `integrate` verifies its candidate.

A failed command whose source fingerprint is unchanged is rerun (`--verify-retries N`, default
`1`, `0` = strict single run). When the repository has `.forge/verify-subset` (a test command
with `{files}` or `{changed}`) and the log names failing tracked test files (unittest
`FAIL:`/`ERROR:` lines, pytest `FAILED path::`, node's test runner `test at file:L:C` or TAP
`location:`), those files run alone first (`verification.failing.log`, run kind `failing`): if
they fail again the regression is confirmed and the whole command is not rerun. If they pass,
or nothing can be named, the whole command reruns as before, so a pass always comes from the
whole command. A rerun that passes leaves status `PASS` but writes
`verification.flaky` (both exit codes), keeps the first run in `verification.log` and the second
in `verification.retry.log`, and prints a loud `FLAKY` line in `run`/`retry`/`integrate` output;
a command that changes the tree is never retried.

**Verification lanes.** Instead of one command, verification can be several named **lanes**
that run side by side, each rerun on its own. A lane is a file whose name is the lane's name
(letters, digits, `_` and `-`, at most 64) and whose text is a Bash command, run from the
integration checkout like the single command; leading `#forge:` lines give it options. Declare
them in the repository as `.forge/verify.d/<name>` files, or for one plan with
`run --verify-lane 'name[opt,opt]=command'` (also `retry`; repeatable, written to
`<plan>/verify.d/`; the first one of an invocation replaces the plan's earlier lanes and its
`--verify`, as `--verify` replaces the plan's lanes). The first source that exists wins: the
plan's lanes, the plan's `--verify`, the repository's `.forge/verify.d/`, then
`.forge/verify` (ignored, with a note, when the repository also has lanes). `run` checks the
lanes before any model is paid for and exits 2 on an invalid one (an unknown or repeated
option, an empty command, a path outside the checkout, a missing baseline).

| Option | Means |
|---|---|
| `group=<name>` | lanes of one group never run at the same time (a shared database, a port) |
| `first` | runs before every other lane; if a `first` lane fails (after its reruns) or changes the tree, the rest are `skipped` |
| `retries=<0-9>` | reruns of this lane when it fails; otherwise `--verify-retries` (default `1`) |
| `reports=<dir>` | JUnit `*.xml` and TAP `*.tap` reports the lane writes under `<dir>` (inside the checkout). Only reports written during that run count; they give the lane's test counts and failing tests, and are re-read every 10 s while it runs |
| `baseline=<file>` | known failing test names, one per line (`#` comments), inside the checkout. A lane whose every failing test is in it ends `known`, which passes. A report that could not be read, or a failure no report names, is judged by the exit code |

Up to `--verify-parallel N` lanes run at once (`run`, `retry` and `integrate`, kept as
`verify_parallel`, 1 to 32, default 3), started in name order. A lane that fails is rerun alone,
after the round, up to its budget; a pass on a rerun makes it `flaky`, which passes but is named
in the run output (`FLAKY: lane <name> failed once and passed on rerun`), in
`verification.flaky` and in the whole-run review. Nothing reruns once the tree changed: the
checkout's fingerprint is compared after every round, and a change fails the verification
(`mutated`; with lanes in parallel it cannot be pinned to one of them). A lane's states are
`waiting`, `running`, `pass`, `fail`, `flaky`, `known`, `skipped` and `interrupted`. Lanes mode
replaces the whole-command rerun above; the single-command mode is unchanged.

A lane may print `::forge step name=<step> state=start` and `::forge step name=<step>
state=end [exit=<n>]`, each on a line of its own, to say which step it is in; forge-ui shows the
open step live. A signal stops the running lanes' process groups (TERM, then KILL after 5 s),
marks them and the verification `INTERRUPTED`, and exits 130 like a single command.

The runner is `scripts/forge-verify.py` (`lanes` lists and checks a directory's lanes; `run`
runs them). In lanes mode `verification.command` holds one `<lane>: <first line>` per lane,
`verification.log` the runner's summary plus the last 200 lines of each lane that failed, and
`verification.lanes.json` the live state of every lane; each lane writes
`verification.lane.<name>.log` and keeps each finished run as `verification.lane.<name>.run-<k>.log`.

```
.forge/verify.d/lint     #forge: first
                         npm run lint && npm run typecheck
.forge/verify.d/unit     #forge: reports=reports/unit
                         npm test -- --reporter=junit --outputFile=reports/unit/junit.xml
.forge/verify.d/db       #forge: group=postgres retries=2 reports=reports/db baseline=.forge/known-db-failures.txt
                         docker compose up -d postgres && npm run test:db
```

Here `lint` runs alone first; `unit` and `db` then run together, another lane in the
`postgres` group would wait for `db`, and a `db` run whose only failures are listed in the
baseline ends `known`.

`scripts/forge-verify-coverage.py` reads the project's own test configuration and **warns**
(never fails) about suites the command does not run — at `run` start before any model is paid
for, again in `verify_result`, and at `integrate` — recording the lines in
`verification.coverage.txt`. Without a verify command it lists what the project defines.

`integrate <plan> --approved` prepares a separate candidate combining the current user branch
and integration revision, runs worktree setup and the configured check there, then updates the
user branch only if the candidate passed and the source branch stayed unchanged. Logs live in
`<plan>/integrate-*/`. A failed candidate remains available for inspection. With no verification
command, integration retains the existing approval boundary and reports UNVERIFIED.

Optional [Ripwire context](ripwire.md) is enabled by default. Use `--no-ripwire`
or `FORGE_RIPWIRE=off` to disable preparation and installation offers.
