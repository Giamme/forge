---
type: changed
breaking: false
audience: user
commits: 
---
`--sweep` now works per finding instead of once per plan: a later run of the same plan sweeps the findings its new tasks left, in a new round, and never a finding an earlier round already tried. When there is nothing new, the run summary says so. A finding the sweep fixed is recorded as fixed once its fix task merges.
