---
type: added
breaking: false
audience: user
commits: 
---
`forge control accept <plan> <task> --reason "…" --approved` accepts a `FAIL`, `UNKNOWN` or `NEEDS_DECISION` task while its run is live. The scheduler applies it at its next pass, just as `forge-parallel.sh accept` would: the findings become known issues, the task merges, and the tasks waiting on it start in the same run instead of ending `BLOCKED`. `control.log` and `accepted.by` record who asked and why. With no live run it is refused, and points to the plain `accept` (#138).
