---
type: added
breaking: false
audience: user
commits: 
---
A task can be split into subtasks, at every level, by forge itself. The subtasks run as a plan of their own inside the task's files, each in its own worktree with its own review; when all have merged, the task is reviewed once on everything they produced. The planner can propose a split when it plans, you can split a task that stopped with one approved choice and no edit of `tasks.tsv` (`retry <plan> <task> --decide split --subtasks FILE`, or `--planner SPEC`; `forge control retry` on a live run), and `run --auto-split` lets the planner do it without waiting. Keeping a task whole is always valid and is recorded with its reason. A split keeps the task's work; `--decide split=fresh` starts over and keeps the old work on a ref (#153).
