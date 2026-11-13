---
type: fixed
breaking: false
audience: user
commits: 
---
forge-ui's Worktrees screen no longer greys out worktrees that are still on disk. It used to miss a repository reached through a symlink, or one whose recorded checkout was deleted by hand; their worktrees then showed as removed or not at all. Now it finds a repository through any of its worktrees and never calls a worktree on disk removed. It names a repository git cannot list, counts a split task's worktree as the run's, and offers to delete a folder left where a worktree was. The screen opens on the worktrees on disk.
