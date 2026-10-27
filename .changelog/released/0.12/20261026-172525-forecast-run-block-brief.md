---
type: changed
breaking: false
audience: user
commits: 
---
The forecast `run` prints before dispatching lists the tasks that don't have enough history yet on one line instead of a row each, and is a single line when no task has enough history, so a new setup no longer opens every run with a table of "not enough history" rows. `forge dataset forecast <plan>` still shows every task.
