---
type: added
breaking: false
audience: user
commits: e10427c d36dd86 eca3773
---
`forge control show|set|pin|unpin <plan-dir>` changes a plan while it runs: the QA threshold (read once per attempt, per task too, and `none` exempts a task), the timeout, `--escalate-after`, verify retries, and, while a scheduler is live, the retry and infrastructure-retry budgets. The budgets keep their per-invocation rule: a live scheduler publishes them for the length of the run and re-reads them at every decision. Every change is validated, logged in `control.log` and recorded in the dataset as a `control` event.
