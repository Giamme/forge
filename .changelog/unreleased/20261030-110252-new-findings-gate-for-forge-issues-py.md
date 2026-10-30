---
type: added
breaking: false
audience: user
commits: 98f907e, 535d274, 9da8fd2, b7d1c71
---
New `findings` gate for `forge-issues.py`: a leftover finding that rewords an open issue becomes a comment on that issue instead of a new one, and the `--file-issues ask` dialog pre-selects the findings that name a concrete defect, with a reason for each one left out. `forge-jev.py findings` offers the same check to other tools.
