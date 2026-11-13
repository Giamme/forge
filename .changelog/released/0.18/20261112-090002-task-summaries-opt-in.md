---
type: added
breaking: false
audience: user
commits: 
---
Task summaries, opt-in with `plan --summaries on --summaries-model SPEC`: a cheap read-only dispatch writes a few sentences per task, plus one sentence for the tasks table, rewritten only when the task's attempts, status or review change. `forge summarize` runs it by hand, and `forge control set summaries on|off` (or `--task ID on|off|inherit`) switches it during or after a run. forge-ui shows each summary in the inspector with its model, time and sources, marks one out of date, and has a master switch and a per-task override. forge never picks the model (#133).
