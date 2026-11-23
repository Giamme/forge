---
type: fixed
breaking: false
audience: user
commits: 
---
Verification runs the lanes (`.forge/verify.d/`) and `.forge/verify` committed on the branch it verifies, not the repository root's copy, so an adopted range that renames a suite and updates its lane is verified with the new lane. Before a run, forge says which lanes differ from the root's. Only committed files count: a repository that keeps `.forge/` out of git still uses the root's lanes (#151).
