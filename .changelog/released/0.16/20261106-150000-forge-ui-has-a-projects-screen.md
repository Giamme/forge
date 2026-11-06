---
type: added
breaking: false
audience: user
commits: 
---
forge-ui has a Projects screen (`#/projects`), linked from the header, a run's repository name and each map district. It has one page per repository with three tabs. Runs lists its runs, with the checkout and branch as a column. Findings lists every finding of every review of every run, with its run, task, attempt, reviewer and status, and opens the diff at it. A finding a later review got past is `superseded`, never `fixed`, and repeats across rounds are one row with a count. Memories shows what `memory.md` gives each role and every learning in the ledger with its state, from each checkout. The dataset fills in runs and checkouts that are gone, and those rows say so (#126).
