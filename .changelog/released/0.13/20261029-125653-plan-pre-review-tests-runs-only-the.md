---
type: added
breaking: false
audience: user
commits: a841924
---
`plan --pre-review-tests` runs only the tests an attempt affects (from `.forge/test-map`, naming conventions or references) before paying for its review; a red result goes back to the implementer as findings, unreviewed. `--pre-review-parallel N` caps how many run at once, `forge control set` changes both during a run, and forge-ui shows the result.
