---
type: added
breaking: false
audience: user
commits: b6d2a9e, 5fcdabc
---
The verification panel in forge-ui now follows a running verification live (current step, last line, elapsed time, a warning after five silent minutes, the log's tail) and, when it fails, opens on the failing tests, marked new or known, each linking to its line in the log.
