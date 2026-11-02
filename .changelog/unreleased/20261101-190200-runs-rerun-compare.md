---
type: added
breaking: false
audience: user
commits: 
---
`forge runs rerun <run-dir> --dwarf SPEC` sets the same plan up again with another implementer, starting from the commit the original run started from, and `forge runs compare <run-dir-A> <run-dir-B>` sets two runs side by side per task (status, attempts, tokens, time, diff size) and records the pair in the dataset. A rerun spends nothing until you pass `--run --yes` after reading the token warning and forecast it prints, and it never merges anything.
