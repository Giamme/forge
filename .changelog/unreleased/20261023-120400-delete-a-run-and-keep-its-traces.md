---
type: added
breaking: false
audience: user
commits:
---
`forge runs rm <run-dir>` deletes a run while the dataset keeps what it taught: a strict import, a self-describing archive (`FORGE_ARCHIVE.json`), and `run_delete_started` and `run_deleted` events, all under the run's own lock. `--worktrees` and `--branches` also remove the run's clean worktrees and its unchecked-out branches (foreign and dirty worktrees and UNMERGED branches are reported); `--dry-run` previews. forge-ui has a matching delete dialog. The dataset also imports `escalations.tsv` and `control.log`.
