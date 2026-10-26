---
type: added
breaking: false
audience: user
commits: 84b1b58
---
A running decomposed plan can retry a stopped or failed task, optionally on another model (`forge control retry <plan> <task> [--dwarf <spec>]`), and pick up a row you added to tasks.tsv (`forge control add <plan> <task>`) without ending the run. The scheduler applies or refuses each request at its next pass and records the outcome in control.log.
