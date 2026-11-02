---
type: fixed
breaking: false
audience: user
commits: 
---
When a run has a verification command, implementers no longer run the whole test suite on every attempt, even if a task's acceptance criteria ask for it: forge runs the suite after the merge. `plan` also names tasks whose acceptance criteria ask for the whole suite (#111).
