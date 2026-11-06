---
type: changed
breaking: false
audience: user
commits: 
---
`forge runs rm --worktrees` also removes a clean scratch worktree forge left in the run's folder: an `integrate` candidate (`_candidate-<n>`) or a culprit search's `_attribute`. It used to count them as foreign and keep them. It uses the same ownership rule as forge-ui's Worktrees screen (#125).
