---
type: added
breaking: false
audience: user
commits: 
---
`forge runs compact` takes back the space run dirs hold for nothing and keeps every run: after a strict dataset import it removes leftover review snapshots and, with `--logs`, gzips each dwarf's and reviewer's log copy (forge-ui reads them the same way). It holds the run's lock, skips active runs, and reports anything forge did not create (a dependency install inside a run dir) instead of deleting it. On a real 2.3 GB plan dir it took back 2.2 GB (#139).
