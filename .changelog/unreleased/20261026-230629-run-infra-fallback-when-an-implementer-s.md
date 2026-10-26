---
type: added
breaking: false
audience: user
commits: d95adb7
---
`run --infra-fallback`: when an implementer's model is at capacity, the task moves at once to the next model of its own tier pool instead of waiting, and the move is recorded like an escalation. The busy model is still paused and counted, so a pool that is entirely at capacity still ends the run.
