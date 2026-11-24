---
type: added
breaking: false
audience: user
commits: 
---
A task stopped because its fix needs a file no task declares can now get that file added and resume with one command, on a live run too: `forge control retry <plan> <task> --decide files+=<path> --reason "…" --approved` (or `forge-parallel.sh retry` with no run live). Its `decision.md` prints the command with the path filled in, and says that on a live run the other choices are given through `forge control retry` (#199).
