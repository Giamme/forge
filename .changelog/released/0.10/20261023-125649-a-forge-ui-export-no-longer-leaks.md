---
type: fixed
breaking: false
audience: user
commits: 4530dc9
---
A forge-ui export no longer leaks a local path spelled another way: the run, repository and home paths are also scrubbed in their real form (on macOS, `/private/var/...` and `/var/...`).
