---
type: fixed
breaking: false
audience: user
commits: 3020e13
---
In brief mode, a task that waits for the brief writer could start right away on macOS with Python 3.9, as if the writer had already finished. The scheduler now always checks the writer the first time.
