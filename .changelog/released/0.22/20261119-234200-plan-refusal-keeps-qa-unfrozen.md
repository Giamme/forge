---
type: fixed
breaking: false
audience: user
---
A `plan` that was refused because tasks were `UNASSIGNED` no longer leaves the fallback `opus` reviewer stuck on them, so a following `plan --qa ...` picks the reviewer you ask for. `plan` also tells you when `--qa` leaves reviewers you set explicitly unchanged.
