---
type: added
breaking: false
audience: user
commits: 
---
`forge memory learnings <repo> --json` lists every learning in a repository's ledger with what forge does with it: in memory, waiting for a second run, stale, trimmed by the cap, or curated out. The states come from forge's own memory rebuild, run without changing the repository. `forge memory` also takes `show`, `prune`, `spend` and `inject` (#126).
