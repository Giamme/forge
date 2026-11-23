---
type: fixed
breaking: false
audience: user
---
After you resume or accept a `NEEDS_DECISION` task, its old decision is cleared, so a task that later ends `FAIL` no longer points you at a `--decide` that doesn't apply. Running `--decide` on a `FAIL` or `UNKNOWN` task now tells you to use `retry` without it.
