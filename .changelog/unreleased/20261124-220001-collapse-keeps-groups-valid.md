---
type: fixed
breaking: false
audience: user
commits: 
---
`plan --collapse-chains` (on by default in `auto`) no longer refuses its own plan when the planner put the chained tasks in a review group (`group …: member … is not in tasks.tsv`). Collapsing now rewrites `groups.tsv`: the parts become `combined`, and a group made only of parts is dropped. The original is kept as `groups.precollapse.tsv`, and `collapsed.json` lists the dropped groups (#257).
