---
type: added
breaking: false
audience: user
commits: 
---
`plan --reviewer-session` is an opt-in experiment that continues the reviewer's own session across rounds on claude, openclaude and codex, and falls back to a fresh session whenever it cannot. Those reviews are tagged in the dataset and kept out of the reviewer statistics, so the experiment does not skew the independence numbers.
