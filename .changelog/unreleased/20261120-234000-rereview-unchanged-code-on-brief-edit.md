---
type: fixed
breaking: false
audience: user
---
Retrying a task after you clarify its `prompt.md`, `approach.md` or `qa_threshold` now reviews the same code again against the new brief, instead of refusing it as an identical diff. With an unchanged brief, an identical diff is still refused.
