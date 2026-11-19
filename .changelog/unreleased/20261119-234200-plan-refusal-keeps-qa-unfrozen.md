---
type: fixed
breaking: false
audience: user
---
Refused `forge-parallel.sh plan` runs (exit 2 when tasks are `UNASSIGNED`) now restore `tasks.tsv` and skip recording `routed.json` so a fallback `opus` reviewer is not frozen across a subsequent `plan --qa ...`, and `plan` now prints a note whenever `--qa` leaves existing explicit reviewers unchanged.
