---
type: fixed
breaking: false
audience: user
commits: 760d5ed
---
Interrupting a decomposed run no longer leaves an implementer or reviewer CLI running: forge waits up to 10 s (FORGE_INTERRUPT_GRACE) for each task to stop its harness, and a harness left behind by a killed run is stopped when the next run starts.
