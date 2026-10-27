---
type: fixed
breaking: false
audience: user
commits: df10821, 6379af9
---
A task that finished just as a decomposed run was interrupted is now reaped first: its passing work still merges and its result is kept, and only the tasks still running are marked INTERRUPTED.
