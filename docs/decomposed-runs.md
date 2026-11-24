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
default and falls back to `opus` (at `high`). Sizing the reviewer to the task's risk costs less
for the same verdicts: the SWE-bench Pro benchmark (#89) saw no false PASS from Sonnet reviewers,
at about a seventh of an `opus:xhigh` review's cost. A set to start from:

```bash
--qa-low sonnet:medium --qa-medium sonnet:high --qa-high opus:high
```

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

## Groups in the approval table

When the plan has `groups.tsv`, the table adds a `groups:` block after the waves. One line
per group: id, `review=task` or `review=group`, the QA spec as `qa=<spec>` or `-`, the
members, and the goal (or `(no goal)`).

```
groups:
  g1  review=group  qa=opus:medium  a, b, c  — Every command can write JSON and TSV
  g2  review=group  qa=sonnet       d, e     — (no goal)
  w3  review=task   -               f, g     — (no goal)
```

A `review=task` group is a name only. It does not change the estimate. When any group has
`review=group`, the estimate counts one review for the group instead of one per member, and
prints both lines. Seven tasks and two review groups covering five members look like this:

```
tasks: 7   waves: 1   estimated dispatches: 11 (7 dwarf + 4 qa)
reviews: 4 (2 groups replace 5 task reviews)
```

The reviews line is `reviews: N (G groups replace M task reviews)`. The word stays `groups`
when there is one. The count is tasks, minus members of review groups, plus the number of
those groups. What the run then does is in
[Groups and group review](../references/decompose-opt-in.md#groups-and-group-review-opt-in). The
tradeoff is in [group review](reviews.md#group-review-opt-in).

## Fresh session per task, plus a capsule

Every dispatch is a **clean slate** — forge never passes `--continue`, `--resume`, or
`codex exec resume`, except the opt-in `plan --reviewer-session` experiment (it continues
a reviewer's session across rounds on claude, openclaude and codex) and the opt-in
`plan --implementer-session` (a retry continues its implementer's session on claude and
openclaude when the work is as the review left it). One task's confused
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

While it works, `run` prints a line when it dispatches a task (`forge: dispatched <task> -> <worktree>`),
when it retries one, and when a task stops for you while the others go on:

```
forge: needs you: auth NEEDS_DECISION (plan) -> /tmp/forge-auth/tasks/auth/decision.md
forge: needs you: docs NEEDS_INPUT (a question to answer) -> /tmp/forge-auth/tasks/docs/needs_input.md
forge: stopped: api INFRA (rate_limit, no attempt spent) -> /tmp/forge-auth/tasks/api/infra.txt
```

An agent that started the run in the background and cannot read its output as it comes can follow
it with `forge watch "$PLAN"` instead: one line per task status change, ending when the run does
(see [`forge watch`](commands.md#forge-watch)). Either way it learns of the stop at once and can put
the decision to you while the run continues. Re-reading the logs or the task statuses in a loop is
the polling the skill avoids; these two are not.

`integrate` refuses without `--approved`, and refuses on a dirty working tree.
A task that turns out to be several pieces can be split into subtasks without editing the plan:
see [Splitting a task into subtasks](#splitting-a-task-into-subtasks). `--max-parallel` bounds the
whole run, subtasks included.

A plan can start from a finished plan's integration branch. `A` and `B` are plan directories:

```
plan B --after A
run B
integrate B --approved
```

`plan` exits 2 unless `A` is a plan of the same repo, every task is `MERGED`, the integration
branch tip is `A`'s `accepted.integration`, and `A`'s scheduler is not live. It writes `after`
and `integration.base`, and creates `B`'s integration branch at that tip. `run` exits 3 if `A`
moved and `B` has not yet merged a task. `integrate` prints `this brings plans` and the run
ids, oldest first. `forge runs` shows `after <run>` on `B`. See
[Chained plans](../references/decompose-integration.md#chained-plans).

## Resuming an interrupted run

Ctrl-C (or a SIGTERM) on `run` stops the running tasks' process groups, waiting up to
`FORGE_INTERRUPT_GRACE` seconds (10 by default) before killing what is left, and stops any
harness that outlived its dispatch. A task that had already finished is reaped first (a PASS
still merges); only the tasks still running become `INTERRUPTED`, with their partial work kept
in the worktree. Verification is skipped, or stopped if it had started (`verification.status`
`INTERRUPTED`, not rerun). The summary lists each interrupted task with its `retry` command,
the plan lock is released, and `run` exits 130. A task stopped while its committed attempt was
under review is retried with that review only, when its worktree still matches the commit.

Run `run` again to skip MERGED tasks, merge saved PASS results after fingerprint checks,
and dispatch eligible pending tasks. Tasks paused on an infrastructure failure (`INFRA`) are
dispatched again without any flag, since nothing was wrong with the work. Failed and
interrupted tasks need an explicit `retry` (or `run --retry-failed N` for rejected ones).
Their worktrees and pinned baselines remain available. Concurrent run/retry/integrate
operations for the same plan are excluded by an advisory lock.

To pause a run on purpose, drain it instead of signalling it (see below): nothing is
interrupted, and `forge control resume` continues the same run.

A split task resumes the same way. Its subtasks are a plan of their own
(`<plan-dir>/tasks/<id>/sub`): `run <plan-dir>` enters the task again, runs the subtasks that
have not merged, and never makes a subtask twice. A failed subtask is retried in that plan
(`retry <plan-dir>/tasks/<id>/sub <subtask>`).

A plan made with Fractal, the backend forge 0.23 removed, can still be read (`forge runs`,
forge-ui) but `run`, `retry` and `forge handoff resume` refuse it (exit 3). Start a new run for
the work that is left.

### From another session or harness

Start with `forge handoff`. With no argument inside a checkout, or with `--repo DIR`, it
reports that repository's newest registered plan that is live or still has a task not
`MERGED` (run-dir mtime; a plan is one with `tasks.tsv`). `forge handoff [show] RUN_DIR
[--json]` reports the run you name and writes nothing: id, kind, the first five non-empty
lines of `request.md`, the plan dir, repo, worktree root and integration branch, plus the
recorded base and accepted commits; the option files and `run.flags.json`; whether the
scheduler is live; each task; what needs a person (a `NEEDS_INPUT` question verbatim, the
first line of a `NEEDS_DECISION`'s `decision.md`, an `INFRA` cause and hint); the last
`control.log` changes; tasks left `RUNNING` with no live scheduler; session notes, newest
first; the driver; and the exact next commands. A path that is not a run dir exits 2. No
matching run exits 3.

`forge handoff resume RUN_DIR` carries a decomposed run on. It claims the run first; a
refused claim exits 3 and changes nothing else. A live scheduler is followed and nothing is
started — leave that run to `forge control show`, and do not start `run` beside it. A
plan made with Fractal (removed in forge 0.23) is not restarted, and resume says so. When another forge
command holds the plan's lock (`schedule.lock`), resume says which and changes and starts
nothing; otherwise it holds that lock while it recovers, so a `run` started meanwhile is refused
instead of racing the reset. Each task still `RUNNING` is then recovered, and only when its worktree is exactly `<wt_root>/<id>`
on `forge/<run_id>/<id>` and any leftover `dwarf.harness` or `qa.harness` has been stopped:
the uncommitted work, untracked files included, is saved to
`tasks/<id>/interrupted-<epoch>.diff`, and only then is the worktree reset and cleaned, the
status set to `INTERRUPTED`, and the attempt counter rewound when `attempts.tsv` has no row
for that attempt, so the attempt is not counted twice. An attempt that had already committed
its work was stopped during its review: it keeps its number, and the retry re-runs only the
review (as for a reviewer that gave no verdict), so its unchanged code is not refused as
identical to the previous attempt. A guard that fails leaves that worktree untouched.
Every `INTERRUPTED` task, including one that was already interrupted,
is prepared with `retry --no-dispatch`. `run` then starts again, detached, from
`run.flags.json`. Output goes to `resume-<epoch>.log`; the exit code is written to
`resume-<epoch>.done` when the run ends. A launch that fails exits 1.

The same recovery is done one level down, in the sub-plan of every split task whose subtasks
have not merged into the task yet: a subtask left `RUNNING` is saved, reset and marked
`INTERRUPTED` under that sub-plan's own lock, and every `INTERRUPTED` subtask is prepared.
Those rows carry `plan`, the sub-plan's directory. A sub-plan another command holds is left
alone, with a warning.

`run` writes `run.flags.json` at each real start. A dry run and a `retry` do not, and a
split or rerun does not copy it. The fields are `retry_failed`, `infra_retries`, `sweep`,
`sweep_max`, `sweep_plausible`, `sweep_ahead`, `max_parallel`, `timeout`, `verify`,
`argv` and `at`. `retry_failed` and `infra_retries` are the budgets the
scheduler removes when it stops (`retry_budget`, `infra_budget`, and each task's
`retry_budget`), so resume can pass them again. Resume rebuilds the `run` command from
those fields; it does not replay `argv` as typed. A run from before 0.19 has no file:
resume still saves and prepares, then prints the `run` command to finish by hand and
starts nothing. An unreadable file does the same, and says the file cannot be read.

`forge handoff note RUN_DIR TEXT [--actor A]` appends one JSON line to
`handoff.notes.jsonl` (`at`, `actor`, `text`): a decision the user made, or a hand fix
under way, that is not in the plan. Whitespace collapses to single spaces. Empty text,
text over 2000 characters, and text that is not UTF-8 are refused. `--actor` defaults to
`cli`.

`forge handoff claim RUN_DIR --harness H [--session S]` writes `handoff.driver.json`
(`harness`, `session`, `since`, `last_seen`). `--harness` matches `[a-z0-9._-]{1,40}`.
`--session` is an id with no whitespace, at most 200 characters. `resume` claims the same
way, and its `--harness` defaults to `unknown`. The same harness and session keep `since`
and refresh `last_seen`. A different driver last seen under 12 hours ago is refused
(exit 3) unless `--take-over`. One last seen 12 hours ago or earlier is replaced without that flag.
Replacing a driver appends one `control.log` line (`action` `take-over`, `actor` `cli`,
`key` `driver`) and a dataset `control` event. When `control.log` is not a regular file
the replacement is refused before the driver file changes. Take over only when asked.

Running `run` again, and an explicit `retry` for a failed or interrupted task, stays
the way to finish by hand. That is also the command resume prints when
`run.flags.json` is missing.

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
forge control accept <plan-dir> docs --reason "the P2 is a test-only nit" --approved   # queue an accept
```

A stop skips verification and exits 130, unless every task had merged by then, and a drain
with nothing left to start lets the run end as usual: both act on work still to do.
`retry` and `add` only queue a request in `control.requests/`; the scheduler applies or refuses
it at its next pass and logs the outcome in `control.log` under the same request id (`forge
control show` lists the log). A retry is prepared exactly as `forge-parallel.sh retry` prepares one,
so a task the policy stopped needs `--dwarf`; it spends no automatic retry budget. An added row
may depend only on tasks the run already knows. Neither outlives the run that was live when it
was queued, and both are refused once the run is stopping.
`accept` is `forge-parallel.sh accept` for a live run, which holds the plan lock that command
needs. It takes a `FAIL`, `UNKNOWN` or `NEEDS_DECISION` task that is neither running nor queued,
with `--reason` and `--approved`: the findings become known issues, the task merges, and the tasks
waiting on it start in the same run. `control.log` records who asked (`--actor`) and why, and
`tasks/<id>/accepted.by` the same. With no live run it is refused and names the plain `accept`.
A forge command the plan lock refuses says which command holds it (pid and since when), and on a
live run which `forge control` command to use instead.

Editing a task's `prompt.md` or `approach.md` while a run is going is allowed. An attempt
already running is not reviewed against the new text: it is marked `STALE` and re-dispatched
with the new requirements, and the attempt is not spent. An edit made while the attempt is
being reviewed is treated the same way: that review's verdict does not count, and its findings go to
the next attempt for reference. `control.log` records `prompt-changed` with actor `scheduler` for
every brief change, whenever it lands: one made between attempts or while the task waits is logged
when its next attempt starts. See [failures that are the plan's](reviews.md#failures-that-are-the-plans).

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
  A MERGED task refuses one.
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

## Splitting a task into subtasks

A task can itself be split, and so can each of its subtasks: goal, plans, tasks and subtasks
follow one rule at every level. Nothing outside forge is needed.

A split task's subtasks are a plan of their own, in `<plan-dir>/tasks/<id>/sub`. They are routed
with the plan's difficulty pools, each owns paths inside the task's `files`, they depend only on
each other, and they run on the same scheduler under the run's one `--max-parallel`. Each
subtask is built in its own worktree and reviewed against its own prompt. When every subtask has
merged, their work is brought onto the task's branch and **the task is reviewed once more, on
the whole diff**, against the task's own prompt. Only then does the task merge into the run.

There are three ways a task gets split. None is forced: keeping a task whole is always a valid
answer, and it is recorded with its reason.

| Who | How | Recorded as decided by |
|---|---|---|
| the planner, when it plans | a task of its reply carries `subtasks` (and a one-line `reason`); `plan` makes the sub-plan and shows it under the table | `planner` |
| you, for a task that stopped | `retry <plan-dir> <id> --decide split --subtasks FILE --reason "…" --approved`, or `--planner SPEC` instead of the file to ask a planner | `user` |
| the run, when you asked for it | `run --auto-split [--split-planner SPEC]`: a task that stops for a decision with `split` among its reasons is split by the planner without waiting | `auto` |

Without `--auto-split`, a task that stops on `split` waits for your choice, as every
`NEEDS_DECISION` does. Its `decision.md` names the split among the choices.

```bash
# the subtasks, as a forge-plan block or its bare JSON (forge-planner.py node-prompt describes it)
cat > /tmp/halves.md <<'JSON'
{"reason": "the parser and the docs can be reviewed apart",
 "tasks": [
  {"id": "parser", "deps": [], "difficulty": "medium", "files": ["src/config.py"],
   "title": "Parse the limits section", "approach": "",
   "prompt": "…\n\n## Acceptance criteria\n\n- …"},
  {"id": "docs", "deps": ["parser"], "difficulty": "low", "files": ["docs/config.md"],
   "title": "Document the limits section", "approach": "",
   "prompt": "…\n\n## Acceptance criteria\n\n- …"}]}
JSON
bash scripts/forge-parallel.sh retry "$PLAN" config --decide split --subtasks /tmp/halves.md \
  --reason "two halves that can be reviewed apart" --approved
```

`retry --decide split` makes the sub-plan and runs it, then reviews the task. With
`--no-dispatch` it only makes it, and the next `run` runs it. On a live run, use
`forge control retry <plan-dir> <id> --decide split --subtasks FILE --reason "…" --approved`:
the scheduler makes the split and runs it. The split is refused, and nothing changes, when a
subtask's file is outside the task's `files`, a dependency is not a sibling, the ids repeat or
form a cycle, or the task is `MERGED`, `RUNNING` or already split. One task in the answer means
"keep it whole": that is recorded, and the task stays as it was.

**The task's existing work.** `--decide split` keeps it: the subtasks start from the task's
branch as it stands and are told what the last review found. `--decide split=fresh` starts over
from the task's base; the earlier work stays on `refs/forge/kept/<run>/<id>`.

**Limits.** Three limits bound every split. `plan` shows them under the task table, so they are
approved with the plan, and the first `run` freezes them: a later `plan` that would change one is
refused.

| Flag (on `plan`) | Default | Bounds |
|---|---|---|
| `--split-depth N` | 2 | levels of subtasks below a task of the plan; `0` turns splitting off |
| `--split-children N` | 6 | subtasks one split may make |
| `--split-nodes N` | 24 | subtasks in the whole run, over every level |

A split that would pass a limit is not made. The task is recorded as `bounded`, with the limit
it met, and runs as one task.

**When a subtask fails.** The task waits (`BLOCKED`, with the subtasks it waits for in
`tasks/<id>/sub.waiting`); no attempt of the task is spent. Retry the subtask in its own plan,
then run the plan again:

```bash
bash scripts/forge-parallel.sh retry "$PLAN/tasks/config/sub" docs
bash scripts/forge-parallel.sh run "$PLAN"
```

`run --retry-failed N` and `--infra-retries M` apply to the subtasks too. Retrying the task
itself continues its subtasks: accepted decisions are kept and no subtask is made twice.

**Reading the tree.** `forge runs tree <plan-dir>` prints every level with each node's status,
decision and reason, and where each sub-plan is. `forge control show`, `forge handoff` and
`forge watch` list subtasks by path (`config/docs`). forge-ui shows the tree on the task, with
the decision, who made it, its reason, the model, the tokens and the diff of every node. A
subtask is a task of its sub-plan, so every command that takes a plan dir and a task id works
on it: `forge control pin <plan-dir>/tasks/config/sub docs sol:high`.

The record is `decomposition.json` in each plan of the tree: one node per task with `decision`
(`atomic`, `split` or `bounded`), `reason`, `by` (`planner`, `user`, `auto` or `default`) and
the limits in force when it was decided. The root plan's file also holds `limits` and `frozen`.

## A goal larger than one plan

A planner that finds the goal too large for one plan plans the first coherent stage and lists
the stages that follow in `later` (a title and a goal each). `plan` shows them under the table.
When the plan has merged, plan the next stage:

```bash
bash scripts/forge-parallel.sh next "$PLAN"   # the new plan dir is <first plan dir>-<stage>, or name one
```

`next` refuses unless every task of `$PLAN` is `MERGED`. The new plan starts from `$PLAN`'s
integration branch (as `plan --after` does), takes its pools, options and limits, and is planned
by the same planner (`--planner SPEC` names another) from the code as it now is. It stops at the
table: each plan of a chain is approved on its own, then run. `integrate --approved` on the
last plan brings the whole chain.

## Splitting and combining plans

`run --retry-failed N` removes most of the reason to split a plan (a failed task no longer holds
the rest of a run). When you still want independent work in separate plans — separate locks,
separate integration branches, one verified at a time — these two make it a supported operation
instead of hand-built git plumbing:

- `split <plan> <new-plan> --tasks a,b` moves tasks **with their saved state** (attempts,
  findings, reviewed commit, status) into a new plan: their branches and worktrees are renamed to
  the new run (`forge/<new>/<id>`), the new integration branch starts at the source's accepted
  tip, dependencies on already-merged tasks are dropped, and the source forgets the moved rows
  (`split-<new>.txt` records them). It refuses — changing nothing — for a plan made with Fractal
  (removed in forge 0.23), a MERGED or RUNNING task, a selection that leaves a dependent behind, or a dependency that is
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
  auth           src/db.py docs/auth.md (named in the brief)
```

A path the task's brief names, as when you added it to `prompt.md` mid-run, is marked
`(named in the brief)`, so an approved scope change reads apart from drift nothing explains.

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
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins. `script` in `dwarf` runs `tasks/<id>/run` |
| `title` | one line, shown in the approval table and the capsule |

Each task also needs `tasks/<id>/prompt.md`, written the same way a single-task forge goal is.

`files` is a **promise, not a prediction**: it's what the wave planner trusts when deciding
what may run concurrently, and what the capsule tells other dwarves not to touch. An
understated `files` is how two dwarves end up in the same file.

`plan` writes resolved specs back into the `dwarf`/`qa` columns, so `run` never consults the
routing rules — and an explicit value survives a re-plan, which is what makes a gate override
durable. (`UNASSIGNED` is treated as a leftover marker and re-resolved.)

A `dwarf` value of `script` runs the executable `tasks/<id>/run` and is not given a model.
`plan` exits 2 when that file is missing or not executable, shows the row, and counts no
implementer dispatch. A non-zero exit or a timeout is `FAIL` with no review and no escalation.
A retry runs the command again from the base, so the way to change the diff is to edit `run`.
See [Scripted tasks](../references/decompose.md#scripted-tasks).
