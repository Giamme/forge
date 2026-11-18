---
type: fixed
breaking: false
audience: user
commits: 
---
In a parallel run, a task that started at the same moment as others could end ERROR with "could not create worktree — fatal: could not open '.git/worktrees/<id>/gitdir'". Tasks no longer clean up stale worktree registrations while another task is creating its worktree.
