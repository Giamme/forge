# Run artifacts

[← README](../README.md) · [All docs](README.md)

What a run leaves on disk: the run directory, a decomposed plan's files, and where worktrees and branches live.

Each stage writes into the run directory, which lives **outside** the repo — anything forge
writes inside the working tree would show up in the dwarf's own diff and land in front of qa
as if the dwarf had written it.

```
<run-dir>/
  <role>.resolved     what the spec resolved to (model, effort, harness, any clamp)
  <role>.prompt       the exact prompt sent
  <role>.cmd          the exact command line executed
  <role>.log          full transcript
  <role>.clock        when the log grew: one `<epoch> <bytes>` line per second it grew (forge-ui times codex and grok events by it; FORGE_LOG_CLOCK=off)
  <role>.last         the final message — read this first
  <role>.infra        only after an infrastructure failure: class, retry_after, detail, harness, model
  <role>.orphans      only when processes were left running after the harness exited
  <role>.session      session id reported by claude, openclaude or codex; absent when the dispatch published none
  <role>.continue     only when the dispatch was asked to continue a reviewer session: requested=, mode=continued|failed, reason=
  changes.diff        the dwarf's real diff, which is qa's input
```

A solo run also records, for forge-ui and for you:

```
<run-dir>/
  status              RUNNING, then PASS|FAIL|UNKNOWN|NOCHANGES|INVALIDATED|TIMEOUT|INFRA|INTERRUPTED|ERROR
  dwarf.spec, qa.spec the specs the run was given
  attempts.tsv        epoch, attempt number, status, infrastructure class, dwarf spec; one row per invocation
```

Plus, in the repo itself and surviving the run:

```
.forge/memory.md    what forge learned here, injected into future prompts
.forge/ledger.tsv   one row per dispatch, ever; never injected
```

A decomposed run adds:

```
<plan-dir>/
  goal.txt
  tasks.tsv
  waves.tsv, deferrals.txt
  tasks/<id>/{capsule.md,prompt.md,dwarf.*,qa.*,changes.diff,status}
  tasks/<id>/attempt, attempts.tsv     attempts spent; one audit row per entry (incl. refunded stops)
  tasks/<id>/known_issues.md, qa.gate  findings tolerated by --qa-threshold, and the gate's decision
  sweep/plan.json, plan.md             run --sweep: the fix tasks, their findings, and every finding left out with its reason
  tasks/<id>/sweep.json                a fix task's findings (S1, S2, ...), each with its source task and key
  tasks/<id>/sweep.outcome.json        what the fix task's review said of each, written when it merged; forge issues reads it
  tasks/<id>/sweep.ahead.json          run --sweep-ahead: the open findings this task was handed
  sweep_ahead                          run --sweep-ahead is on for this plan
  tasks/<id>/guard.txt                 what forge observed about the implementer (self-commit, orphans, promises)
  tasks/<id>/infra.txt, resume, noretry  infrastructure stop record (<role>.infra plus stage=); QA-only resume marker; no-progress marker
  tasks/<id>/<role>.harness            only while a guarded dispatch runs: its harness's pid and start time, for forge-guard.py stop
  tasks/<id>/qa_threshold              optional per-task blocking threshold: P0-P3, or none (exempt from the plan's)
  tasks/<id>/escalations.tsv           epoch, attempt, from-spec, to-spec, source (policy|retry|pin|fallback); older rows have 4 columns
  tasks/<id>/escalation.pin            a human's one-shot model for the next full retry: one dwarf spec on one line
  tasks/<id>/escalation.pin.applying   the pin while a retry has claimed it; removed once the retry is recorded
  tasks/<id>/retry_findings.commit     commit the carried review judged
  tasks/<id>/retry_findings.session    session id of that review, when it recorded one
  tasks/<id>/retry_findings.resolved   that review's resolution (harness=, model=, effort=)
  tasks/<id>/changes.since.diff        diff from that commit to the one under review; omitted when there is no such range
  tasks/<id>/changes.since.range       the two commits, full hashes
  tasks/<id>/changes.since.stat        diff --stat of that range
  tasks/<id>/review_sessions.tsv       one row per review round while plan --reviewer-session is on: round, attempt, mode, reason, session id
  tasks/<id>/retry_budget, retries.used   live per-task retry budget (a number); retries spent (kept after the run)
  results.tsv                          id  status  branch
  timeout, qa_threshold, verify_retries  options remembered from run/retry flags (also escalate_after)
  escalate_effort                      on: the retry policy's effort step (--escalate-effort; absent = off)
  infra_backoff, infra_fallback        --infra-backoff's shape; on while --infra-fallback is set
  max_parallel                         the concurrency limit: written by each run from --max-parallel, changed live by forge control
  reviewer_session                     holds on; present while plan --reviewer-session is on
  <final-review-dir>/review.meta       final-review-<epoch>-<pid>/ or integrate-*/final-review/; base=, tip=, source=, spec=, threshold=, integration=, previous=, since=; session= and session_reason= while reviewer_session is set
  control.log                          one JSON line per human change made with forge control, and per request outcome
  control.requests/                    retry and add requests queued by forge control: one JSON file each, removed once settled
  scheduler.live                       only while a scheduler runs: {"pid", "started"}
  drain, stop                          only while a scheduler runs: forge control drain (empty) and stop (graceful|now)
  infra.paused                         only while something is paused on infrastructure: {"scopes": [...]}
  control.lock                         the lock that serialises changes to the plan's control files, created by whichever takes it first (forge control, the scheduler or an escalation record) and left in place
  retry_budget, infra_budget           only while a scheduler runs: its live budgets (a number each)
  integration.base                     commit the integration branch started from
  verification.{status,exit,log,command,fingerprint}   the combined verification; status PASS, FAIL or INTERRUPTED (a signal stopped it)
  verification.{flaky,retry.log}       only when a failing command passed on rerun (lanes: flaky alone, per lane)
  verification.commit                  the integration commit that was verified
  verification.runs.tsv                one row per run of the verify command or of a lane (below)
  verification.run-<n>.log             single command: each run's own copy of its log
  verification.lanes.json              lanes only: every lane's state, runs, step and tests, rewritten as they change
  verification.lane.<name>.log         lanes only: the lane's run in progress
  verification.lane.<name>.run-<k>.log lanes only: each finished run of the lane
  verification.coverage.txt            suites the verify command does not run (advisory)
  verify_cmd, verify.d/                the plan's --verify command, or its --verify-lane lanes (one file per lane)
  verify_parallel                      lanes run at once (--verify-parallel; default 3)
```

`verification.runs.tsv` is tab-separated, one row per run: `n`, `kind` (`initial`, `rerun`, or
`jev_retry` for a single command), `exit`, `tree_changed` (`0`/`1`), `duration_s` and `log`. With
lanes each row has two more columns, `lane` and `started` (epoch seconds); lanes run side by
side, so `tree_changed` is `1` only when the whole verification was a single lane run that
changed the tree (`verification.lanes.json`'s `mutated` says whether it changed at all). In lanes
mode `verification.flaky` holds `lanes=<names>` and, per flaky lane, `<lane>.first_exit=` and
`<lane>.retries_used=`.

with worktrees and branches at:

```
<repo>/../.forge-worktrees/<run-id>/<task-id>    worktree
forge/<run-id>/<task-id>                         task branch
forge/<run-id>-integration                       integration branch
```

`control.log` lines look like
`{"at": 1761200000.5, "actor": "ui", "action": "set", "task": null, "key": "qa-threshold", "value": "P2", "previous": "P1"}`
(`actor` is `cli` or `ui`; `action` is `set`, `pin`, `unpin`, `drain`, `resume`, `stop`, `retry` or
`add`; `key` is the control's name, or `pin`; `task` is null for a plan-wide control). A queued
`retry` or `add` also carries its `request` id, and the scheduler later appends the outcome under
the same id:
`{"at": 1761200030.1, "actor": "scheduler", "action": "retry", "task": "docs", "key": null, "value": "sol:high", "previous": null, "request": "…", "outcome": "refused", "reason": "the task is running"}`
(`outcome` is `applied` or `refused`). `scheduler.live`, `retry_budget`, `infra_budget`, the
per-task `retry_budget`, `drain`, `stop` and `infra.paused` exist only while the scheduler is live:
it publishes the flags it was given when it starts, re-reads them at every decision and removes
them when it stops, which is why `forge control` refuses a budget change, a drain or a stop outside
a live run. A request still queued then is refused (`run ended`, or `left by an earlier run` at the
next start). See [live controls](decomposed-runs.md#changing-a-run-while-it-works).

`infra.paused` holds one entry per paused scope: `scope` (`all`, `harness:<h>` or
`model:<h>/<m>`), `class`, `harness`, `model`, the `tasks` it holds, `cycle` (which wait this is),
`until` (epoch seconds, null while not waiting) and `exhausted` (its waits are used up).

The worktree root is a **sibling of the repo, never `TMPDIR`**. Isolated worktrees are never
pushed, so a temp root that gets cleaned takes the only copy of that work with it.
