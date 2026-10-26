---
type: added
breaking: false
audience: user
highlight: true
commits: 
---
`forge dataset reviewers`: a report card for reviewer models. For each reviewer, how often work it PASSed was later reverted or mostly rewritten within the outcome horizon (14 days by default), with a 95% interval, or "not enough data" below 10 outcomes. forge-ui's Models view shows it as the Reviewers table's Did not hold column. Advisory only: it never changes routing.
