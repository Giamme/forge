---
type: fixed
breaking: false
audience: user
---
Resuming or accepting a `NEEDS_DECISION` task now removes the resolved `decision.md` (and `decision.from`) so a task that later ends `FAIL` after using its decided attempts never leaves stale `--decide` instructions on disk, and `--decide` on a `FAIL`/`UNKNOWN` task now tells you to run `retry` without `--decide`.
