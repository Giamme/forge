---
type: changed
breaking: false
audience: user
highlight: true
commits: 8433425 692e78a
---
On a retry, the per-task reviewer now first checks the previous round's findings, sees the diff between the two attempts and reads the implementer's reply, instead of starting cold. This applies on every harness, so a finding that was fixed is confirmed as fixed and one that was not is raised again.
