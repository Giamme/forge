---
type: added
breaking: false
audience: user
commits: f68c782 a0b70db ecfbdfc 8c24862 fdfaa26 5170052
---
`forge config show|set|unset` shows every config source in one view (the registry, Jev, a repository's `.forge/verify`, `setup` and `verify-subset`, the dataset and `FORGE_*` variables with secrets hidden) and edits the per-repo files and `jev.enabled`. forge-ui shows the same view as Settings.
