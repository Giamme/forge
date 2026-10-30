---
type: changed
breaking: false
audience: user
commits: 9d1e641, 96e45ea, 09c1786, d830194, 8bbed56, 0bd78a7, 1369635, becf2b4, 22904fc, 18a84a8, 28127d3, f5bd374, 53ed141
---
QA verdicts now follow a blocking threshold in both directions, `P2` unless `--qa-threshold` sets another, in solo runs, decomposed tasks and the whole-run review alike. A PASS that lists a CONFIRMED finding at the threshold or worse becomes FAIL, and its retry says why. A FAIL made only of CONFIRMED findings below it is accepted, and those findings are kept in `known_issues.md`. `--qa-threshold P3` (or `none`) makes every CONFIRMED finding block; `plan` now takes the flag too.
