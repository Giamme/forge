---
type: changed
breaking: false
audience: user
commits: 
---
`adopt`'s reviewer is now told the run's verification command (or its lanes' commands), so it can run the parts that touch the adopted range. Verification itself still runs after the review, as before (#131).
