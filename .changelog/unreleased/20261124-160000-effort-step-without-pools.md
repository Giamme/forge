---
type: fixed
breaking: false
audience: user
commits: 
---
With `--retry-per-model` and `--escalate-effort`, a task on a plan without tier pools now moves to the next effort of its model once it has failed N times there, as the escalation policy says, instead of stopping as `FAIL` with retry budget left (#222).
