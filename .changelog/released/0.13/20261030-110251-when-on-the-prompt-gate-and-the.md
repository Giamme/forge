---
type: added
breaking: false
audience: user
commits: 07d379b, 84fb0d1, 01e9df4, 1c1dc9e, 6ecc381, 68fea30, db8cc2d, d261b4a, cd84de5, 0ead756, a61e1f1, b7d1c71
---
When `on`, the prompt gate and the new coverage gate send work back to the planner once: a task whose prompt reads as too vague to check, or a requirement in `request.md` that no task covers. `plan` then exits 4 and writes `plan.sendback.md`; fix the plan and run `plan` again. A block under the task table shows what each plan-time gate did.
