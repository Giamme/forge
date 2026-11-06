---
type: added
breaking: false
audience: user
commits: 
---
forge-ui has a Worktrees screen (`#/worktrees`, linked from the header and from each run's page). It lists every worktree forge created, in any repository it knows, with its run, task, branch, last commit, size and status: in use, uncommitted changes, locked, needed by the run, failed task, finished, missing on disk, not forge's, unknown to git, or removed. It gives the command that removes each one safely, to copy, never with `--force`. The page runs nothing, so it works with `--read-only`. forge now records each worktree it adds or removes in the dataset (`worktree_created`, `worktree_removed`, read with `forge dataset worktrees`), so removed ones stay listed with their dates. Worktrees from older runs are derived from run dirs and the dataset (#125).
