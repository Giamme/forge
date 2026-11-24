# Decomposed runs: retries, stops and recovery

What happens when a task fails, stops or needs the user, and the commands that carry a run on.
Part of the decomposition reference; planning is in [decompose.md](decompose.md).

## Contents

- [Retrying, and resuming an interrupted run](#retrying-and-resuming-an-interrupted-run)
- [When reviews don't converge](#when-reviews-dont-converge)
- [Infrastructure failures](#infrastructure-failures)
- [Implementer guards](#implementer-guards)
- [Accepting, splitting, combining and the whole-run review](#accepting-splitting-combining-and-the-whole-run-review)

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
review resumes as in [Groups and group review](decompose-opt-in.md#groups-and-group-review-opt-in).

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
  A range that extends one a review refused, or one named with `--resubmit <adopt-dir>`, is a resubmission
  (#155): its reviewer gets the previous findings and the commits and diff since, and settles each earlier
  finding and its siblings first; after `--escalate-after` refusals of the same work (3 by default) the next
  one waits for the user (`--decide resubmit`, `--approved`, or adopting it in parts), and each review
  records its number in `adopt-*/submission`.
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
