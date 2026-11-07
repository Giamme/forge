---
type: changed
breaking: false
audience: user
commits: 
---
`run --sweep` now fixes what an adopted range's review tolerated, with the adoption's reviewer reviewing the fix, where it used to drop those findings for want of a model. Findings a person kept (`accept`, or `adopt --approved`) are still never swept, and the sweep plan now lists them as `accepted` instead of leaving them out silently (#131).
