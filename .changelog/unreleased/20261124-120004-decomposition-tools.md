---
type: added
breaking: false
audience: user
commits: 
---
The tools read subtasks at every level: `forge runs tree <plan>` prints the tree with each decision and reason, `forge runs` counts subtasks, `forge runs rm` removes a run's sub-plans with it, `forge control show`, `forge handoff` and `forge watch` name subtasks by path, `forge handoff resume` recovers stale subtasks, and the dataset records each decision. forge-ui shows a split task's tree with every node's decision, reason, model, tokens and diff (#153).
