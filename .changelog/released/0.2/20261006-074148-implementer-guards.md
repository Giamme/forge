---
type: added
breaking: false
audience: user
commits: 7b0954d
---
Forge now records when an implementer commits on its own, switches branch, leaves background processes running (they are cleaned up) or promises later work. The notes go to `guard.txt`, the reviewer prompt and the run summary; they are warnings and never change a task's status.
