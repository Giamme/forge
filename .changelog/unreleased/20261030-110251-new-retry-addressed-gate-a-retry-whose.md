---
type: added
breaking: false
audience: user
commits: 7d5eef0, aab057c, 4c2e34d, 97d4544, b7d1c71
---
New `retry-addressed` gate: a retry whose change leaves one of its blocking findings untouched goes back to the implementer without paying for a review. The attempt is recorded as `BOUNCED`, and the next one is always reviewed.
