---
type: added
breaking: false
audience: user
commits: 3ead253
---
Groups derived with `plan --groups waves` get a title (`Wave N`). The approval table shows each group's title and goal, says which groups have no goal, and `forge group set PLAN GID --goal "…"` (or `--title`) sets one and freezes the groups again, logged in `control.log`.
