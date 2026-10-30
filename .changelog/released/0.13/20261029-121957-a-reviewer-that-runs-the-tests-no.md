---
type: fixed
breaking: false
audience: user
commits: d220bef, f422b90
---
A reviewer that runs the tests no longer gets a passing attempt thrown away: files it leaves untracked in its review copy, such as `__pycache__/` in a repository with no `.gitignore`, no longer void the review. A task `INVALIDATED` because its review copy changed is now retried by `--retry-failed`, and that retry, like an explicit `retry`, only reviews the unchanged work again; forge-ui offers Retry now for it.
