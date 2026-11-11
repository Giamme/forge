---
type: added
breaking: false
audience: user
commits: 
---
The new `forge dataset tiers` command looks inside each difficulty tier before anyone adds a tier. It sets plan-time facts (declared files, dependencies, wave, prompt size) against first-try passes, attempts and unfinished tasks. Each fact is tested on runs the split never saw, and adjusted for the implementer, so a weaker model mix is not read as harder tasks. Facts known only after the attempt, such as the diff size, are reported apart. `--sample` writes past prompts for a blind re-rating, and `--blind` reports how often that rating matches the recorded tier. Nothing in a run changes (#123).
