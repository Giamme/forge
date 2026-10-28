---
type: added
breaking: false
audience: user
commits: 48f7851, 6e6ca1e, a0e5c8a, c1dc3e9
---
Decision models can be capped per run and per day, in tokens and estimated cost (`forge decide budget`, or `--decide-budget tokens=N,usd=X` on a run); a gate past a cap stops asking for the rest of the run or day.
