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
  tasks/<id>/qa_threshold              optional per-task blocking threshold
  results.tsv                          id  status  branch
  timeout, qa_threshold, verify_retries  options remembered from run/retry flags
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

The worktree root is a **sibling of the repo, never `TMPDIR`**. Isolated worktrees are never
pushed, so a temp root that gets cleaned takes the only copy of that work with it.
