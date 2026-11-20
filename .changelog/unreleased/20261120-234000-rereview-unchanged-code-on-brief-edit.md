---
type: fixed
breaking: false
audience: user
---
Retrying a task after clarifying `tasks/<id>/prompt.md`, `approach.md`, or `qa_threshold` now re-reviews an unchanged diff against the updated brief instead of refusing it as byte-identical, while retries with an unchanged brief continue to refuse identical diffs as before.
