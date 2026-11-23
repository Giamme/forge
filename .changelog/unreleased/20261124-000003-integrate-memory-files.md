---
type: fixed
breaking: false
audience: user
commits: 
---
`integrate` no longer refuses a working tree whose only changes are forge's own project memory (`.forge/memory.md` and `.forge/ledger.tsv`), and `plan` no longer warns about uncommitted work for them alone. When the tree is dirty for another reason, the refusal lists the changed paths (#156).
