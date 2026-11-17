---
type: fixed
breaking: false
audience: user
commits: 
---
Review findings now show their real state. A finding that was filed as an issue, fixed in the code, dismissed or swept no longer shows as open: forge reads issue states from GitHub (closed issues count as fixed), keeps one record per repository whatever checkout or worktree a run used, and finds every run of the repository, including runs from removed worktrees. forge-ui's open count leaves out shadow and unlabelled reviews.
