---
type: added
breaking: false
audience: user
commits: f17f9e4
---
`--gate name=mode` and `--no-gates` on `plan` and solo runs choose the gates for that run; they are kept with the run, so resume and retry decide the same way.
