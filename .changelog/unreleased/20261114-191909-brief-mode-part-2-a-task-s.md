---
type: added
breaking: false
audience: user
commits: 
---
Brief mode part 2: a task's questions name its group, `--brief-scope wave|plan` lets you answer wider questions, and the answer becomes a proposal (`forge brief propose`). Nothing in the plan changes until you approve its exact diff with `forge brief apply PLAN PID --approve HASH`, or reject it. A wave proposal changes only that group's unstarted tasks; a plan proposal can also add, remove or split tasks. Affected tasks get their questions asked again. Proposals can be approved or rejected in forge-ui too.
