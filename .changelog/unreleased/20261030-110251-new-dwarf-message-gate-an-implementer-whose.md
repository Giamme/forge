---
type: added
breaking: false
audience: user
commits: 295911d, e2c5218, 98765b5, 3728451, 74c043b, 5dae8ef, b7d1c71
---
New `dwarf-message` gate: an implementer whose final message says it did not finish, or was blocked, is sent back without a review (`INCOMPLETE`), keeping the findings it was working on. One that asks a question only you can answer stops the task as `NEEDS_INPUT`; the run summary shows the question, and `forge-parallel.sh retry <plan> <task> --answer "…"` (or `--answer-file`) resumes it.
