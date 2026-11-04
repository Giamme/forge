---
type: added
breaking: false
audience: user
commits: aa50b5d, 430308a, 1d6e42a, 259eb9d, 10d7bea, 5571a8d, 4b934d0
---
docs/models.md gives a recommended routing per difficulty, measured on benchmark #86, and forge-ui's Models page shows it under each tier. `doctor --spec` and the plan table add a note when a spec is measurably slower than a lower effort of the same model with no better first-try pass rate, such as `haiku:max` against `haiku:xhigh` (#91).
