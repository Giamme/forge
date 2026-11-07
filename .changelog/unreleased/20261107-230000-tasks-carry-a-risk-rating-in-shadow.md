---
type: added
breaking: false
audience: user
commits: 
---
A task can now carry a risk rating, separate from its difficulty: what a mistake that gets through would cost. The planner rates it, or you write `low`, `medium` or `high` to `tasks/<id>/risk` before `plan`. It is recorded in shadow. No model, reviewer or verdict changes, `tasks.tsv` keeps its columns, and a plan without ratings behaves as before. The new `forge dataset risk` command shows tasks by difficulty and risk together: how often the two ratings differ, and, per pair, first-try passes, serious findings and landed work that did not hold. The docs now say that reviewer tiers follow difficulty (#122).
