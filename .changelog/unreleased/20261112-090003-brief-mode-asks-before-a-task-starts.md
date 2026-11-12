---
type: added
breaking: false
audience: user
commits: 
---
Brief mode, opt-in with `plan --brief advisory|gated|strict --brief-model SPEC`: before each task starts, a cheap read-only dispatch writes the questions only you can answer, each with why it matters, options and the default forge will assume. Answers (`forge brief answer`, or the answer box in forge-ui's inspector) go into the task's prompt. `gated` holds a task with an unanswered must-answer question, and `strict` makes `run` exit 3 while any is open. After a merge, dependents' open questions are flagged as possibly stale, and an answer that comes after its task started is kept as late, with a `forge control add` suggestion. forge-ui lists the run's open questions, flagged first, then by how soon each task starts (#135).
