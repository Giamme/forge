---
type: added
breaking: false
audience: user
highlight: true
commits: 
---
`run --sweep Pn` fixes the review findings a run tolerated while its context is fresh. Once every task has merged, and before the combined verification, the open findings at Pn or worse become a few extra fix tasks, one per file (at most 8 findings each, `--sweep-max N` tasks, default 5; `--sweep-plausible` adds PLAUSIBLE ones). Each fix task runs on the plan's low tier, with a narrow review that says, finding by finding, whether it was fixed. The sweep plan, with every finding it left out and why, is printed and kept in `sweep/plan.md`. `forge issues` no longer offers a finding the sweep fixed, and a fix task that fails does not fail the run.
