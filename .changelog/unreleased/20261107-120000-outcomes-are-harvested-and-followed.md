---
type: added
breaking: false
audience: user
commits: 
---
Outcomes are now recorded without anyone running `forge dataset outcomes`: the end of every `run` and `integrate` harvests that repository, bounded by `FORGE_OUTCOMES_TIMEOUT` (default 20 s), never changing the exit code, and off with `FORGE_OUTCOMES=off`. The harvest also follows work where it went: an integration whose branch a pull request merged and deleted is measured on the branch it landed in, and so is a run whose integration branch was merged by hand. `forge dataset status` shows the outcomes recorded and when they were last harvested (#120).
