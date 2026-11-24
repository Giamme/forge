---
type: fixed
breaking: false
audience: user
commits: 
---
Reviewers can run tests that need a git submodule: task, final, adopt and solo review snapshots now get each submodule's files the way task worktrees do, and the reviewer is told which submodules it has (or which forge could not fill). Git does not see the files, so the snapshot's fingerprint and `git status` are unchanged (#255).
