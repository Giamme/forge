---
type: added
breaking: false
audience: user
commits: 7a53c08, 75021de, bcf39e4, 591731f, d9861dd, 188b9ac, 393383c, 7a97005, b7d1c71
---
`forge decide status`, `report` and `replay` cover the five new gates (coverage, retry-addressed, dwarf-message, attribution, findings), which start `off` and need `--force` to go `on` until a replay passes. Every label now means "the gate should have acted"; the memory gates report how often they acted. `references/decision-models.md` describes what each gate does when on.
