---
type: fixed
breaking: false
audience: user
commits: 5486bdf, bd6cd9c, b8a534e, dee6ef2, a745dd0, 4d02f96, 3cd0f70, 231716f, 3769db2, 1fbccc1, 663e43d, 39dcad0, b43c14e, 0554a7f
---
forge-ui groups runs by repository: a run started in any worktree of a repository joins that repository's group, also after the worktree is removed, and shows the worktree and branch it started in. New runs record `repo.identity` (root, worktree, branch); older runs are matched by their base commit. `/api/runs` carries `repo_root`, `repo_worktree` and `repo_branch`, and per-repository settings resolve against the repository rather than the worktree.
