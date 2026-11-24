---
type: fixed
breaking: false
audience: user
commits: 
---
forge-ui no longer shows a run whose scheduler died (a crash, a killed terminal) as still running: the tasks it left running read as interrupted with `forge handoff resume` to pick the run up, the clock stops, Projects stops counting it as live, and its worktrees can be removed (#229).
