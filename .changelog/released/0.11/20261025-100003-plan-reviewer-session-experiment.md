---
type: added
breaking: false
audience: user
commits: 4570cc1 8943623 2e6e37e a176a8a b324da4 8de6fda cc755c2
---
`plan --reviewer-session` is an opt-in experiment that continues the reviewer's own session across rounds on claude, openclaude and codex, and falls back to a fresh session whenever it cannot. Those reviews are tagged in the dataset and kept out of the reviewer statistics, so the experiment does not skew the independence numbers.
