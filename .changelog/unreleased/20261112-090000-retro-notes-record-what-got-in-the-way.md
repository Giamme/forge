---
type: added
breaking: false
audience: user
commits: 
---
Retro notes, opt-in with `plan --retro` (or `forge config set retro on`): implementers and reviewers record what got in their way (a missing tool, a flaky test, a wrong instruction), each with its command and error and checked against the run's own logs. `forge retro report` groups them into `retro.md` (forge's own problems, the project's, the environment's) with counts across tasks and earlier runs. `forge retro issues` drafts redacted GitHub issues for forge's repository and posts only what a person approves by hash. Nothing leaves the machine otherwise (#132).
