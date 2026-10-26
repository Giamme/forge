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
  <role>.last         the final message — read this first
  <role>.infra        only after an infrastructure failure: class, retry_after, detail
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
  tasks/<id>/guard.txt                 what forge observed about the implementer (self-commit, orphans, promises)
  tasks/<id>/infra.txt, resume, noretry  infrastructure stop record; QA-only resume marker; no-progress marker
  tasks/<id>/qa_threshold              optional per-task blocking threshold: P0-P3, or none (exempt from the plan's)
  tasks/<id>/escalations.tsv           epoch, attempt, from-spec, to-spec, source (policy|retry|pin); older rows have 4 columns
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
  reviewer_session                     holds on; present while plan --reviewer-session is on
  <final-review-dir>/review.meta       final-review-<epoch>-<pid>/ or integrate-*/final-review/; base=, tip=, source=, spec=, threshold=, integration=, previous=, since=; session= and session_reason= while reviewer_session is set
  control.log                          one JSON line per human change made with forge control
  scheduler.live                       only while a scheduler runs: {"pid", "started"}
  control.lock                         the lock file forge control creates on its first change and leaves in place
  retry_budget, infra_budget           only while a scheduler runs: its live budgets (a number each)
  integration.base                     commit the integration branch started from
  verification.{status,exit,log,command,fingerprint}   the combined verification
  verification.{flaky,retry.log}       only when a failing command passed on rerun
  verification.coverage.txt            suites the verify command does not run (advisory)
```

with worktrees and branches at:

```
<repo>/../.forge-worktrees/<run-id>/<task-id>    worktree
forge/<run-id>/<task-id>                         task branch
forge/<run-id>-integration                       integration branch
```

`control.log` lines look like
`{"at": 1761200000.5, "actor": "ui", "action": "set", "task": null, "key": "qa-threshold", "value": "P2", "previous": "P1"}`
(`actor` is `cli` or `ui`; `action` is `set`, `pin` or `unpin`; `key` is the control's name, or `pin`; `task` is null for a plan-wide
control). `scheduler.live`, `retry_budget`, `infra_budget` and the per-task `retry_budget` exist
only while the scheduler is live: it publishes the flags it was given when it starts, re-reads them
at every decision and removes them when it stops, which is why `forge control` refuses a budget
change outside a live run. See [live controls](decomposed-runs.md#changing-a-run-while-it-works).

The worktree root is a **sibling of the repo, never `TMPDIR`**. Isolated worktrees are never
pushed, so a temp root that gets cleaned takes the only copy of that work with it.
