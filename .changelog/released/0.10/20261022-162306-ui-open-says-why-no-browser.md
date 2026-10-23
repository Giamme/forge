---
type: changed
breaking: false
audience: user
commits: 
---
A repeated `forge ui open` for a run that already has a browser tab no longer looks like it did nothing: it prints the link as before and says on stderr that no new tab was opened, so you know to use the link.
