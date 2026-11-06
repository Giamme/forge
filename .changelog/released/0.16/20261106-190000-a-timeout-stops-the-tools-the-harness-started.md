---
type: fixed
breaking: false
audience: user
commits: 
---
A dispatch that times out or exits now also stops the processes its harness started in sessions or process groups of their own, such as a test runner and the web servers it launched, before verification or a retry can overlap with them in the same worktree. forge samples the harness's descendants every 2 seconds and lists what it stopped in `<role>.orphans`. A standalone `retry` that gets a TERM or Ctrl-C now stops what it dispatched before it exits with 143, instead of leaving the implementer running (#129).
