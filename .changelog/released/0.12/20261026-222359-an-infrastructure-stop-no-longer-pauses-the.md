---
type: changed
breaking: false
audience: user
highlight: true
commits: 39f587d
---
An infrastructure stop no longer pauses the whole decomposed run: a rate limit pauses only that model and a quota or sign-in stop only that CLI, each with its own wait and --infra-retries budget, while tasks routed elsewhere keep running. `forge control show` lists the active pauses.
