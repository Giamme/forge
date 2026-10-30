---
type: fixed
breaking: false
audience: user
commits: 
---
A dispatch no longer hangs forever when its run directory is removed, or cannot be written, while the agent is running: it used to wait for its log-clock sampler, which waited for it. Test runs left one such pair of processes behind each time.
