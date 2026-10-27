---
type: added
breaking: false
audience: user
commits: 458ca6d, 9241f2e, cb960c8
---
`--escalate-effort` (on `run` and `retry`, or `forge control set <plan> escalate-effort on`): when a task has failed its escalation limit on the top tier's model, forge retries it once more at the next effort that model accepts before stopping it with a split suggestion. Turning it on also releases tasks the policy already stopped.
