---
type: added
breaking: false
audience: user
commits: 
---
`adopt` recognises a resubmission: a range that extends one a review refused (or one named with `--resubmit <adopt-dir>`) is reviewed with the previous review's findings, the commits and the diff since that submission, and the reviewer is asked to settle each earlier finding, and look for its siblings, before reviewing the rest. After `--escalate-after` refusals of the same work (3 by default, the plan's setting when it has one) the next resubmission waits for a decision: `--decide resubmit` for one more round, `--approved` to keep it, or adopting it in parts. Each review records its submission in `adopt-*/submission`, and a refused range taken from elsewhere stays reachable at `refs/forge/adopt-refused/<adopt-dir>` (#155).
