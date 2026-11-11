---
type: added
breaking: false
audience: user
commits: 
---
`forge decide replay --label first-attempt` judges the stored drift answers on the 2026-10-16 label: a file counts as drift when attempt 1 edited it, whether or not that attempt passed. It sends nothing and records nothing, so promotion still reads the site's own label. The replay report now also shows an inverted gate's threshold as it is configured, such as `prompt_warn 0.55 (inverted: a score at or above 0.45)`, instead of `0.44999999999999996` (#101).
