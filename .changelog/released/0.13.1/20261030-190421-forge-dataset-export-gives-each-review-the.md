---
type: fixed
breaking: false
audience: user
commits: 4241fe1, c1f57dc, dffc432, 6c7aa44, f42b786
---
`forge dataset export` gives each review the gate decision of its own QA execution: a review that ended in an outage before a QA-only resume no longer inherits the gate of the review the attempt ended on, and separate runs that met the same diff still export as one review case.
