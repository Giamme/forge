---
type: changed
breaking: false
audience: user
commits: 1caac75
---
`terra` now resolves to gpt-6.1-terra. The previous model is still available as `terra-5.6`, and dispatches recorded on it are counted under `terra-5.6` in `forge dataset reviewers`, review history and forge-ui's model leaderboard, never mixed into the new `terra`.
