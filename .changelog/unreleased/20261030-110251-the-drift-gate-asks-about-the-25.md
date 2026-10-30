---
type: changed
breaking: false
audience: user
commits: 163bfa0, ce41975, 2378574, 615001c, 2d23a83, e15dfde, b7d1c71
---
The drift gate asks about the 25 files a task could plausibly touch, ranked, instead of the first 120 in alphabetical order. When `on`, drift adds the files it predicts to the task before the waves are computed, and the forward gate adds the dependency it predicts (never one that would form a cycle); both are recorded in `plan-gates.json`. In `shadow`, and under `--jev-shadow`, they only record what they would do.
