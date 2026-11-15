---
type: added
breaking: false
audience: user
commits: 
---
`plan --reviewer-session-rate R` turns the reviewer-session experiment on for a sampled share of the tasks, balanced by difficulty and fixed at plan time, so one run can compare continued and fresh review rounds. The other tasks log `not-sampled` in `review_sessions.tsv`.
