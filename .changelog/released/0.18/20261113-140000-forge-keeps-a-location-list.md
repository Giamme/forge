---
type: added
breaking: false
audience: user
commits: 
---
forge now keeps a list of everything it puts on disk, in `<config home>/forge/locations.jsonl`: every run dir and worktree, with its repository's git dir and main checkout, and when forge removed it. `forge du`, `forge runs compact` and forge-ui's Disk and Worktrees screens use it to know where to look, so a checkout deleted by hand, a symlinked path or a run dir that is gone no longer hides anything. It is written even with the dataset off (`FORGE_RUN_REGISTRY=off` turns it off), and filled once from the run registry and the worktrees already on disk.
