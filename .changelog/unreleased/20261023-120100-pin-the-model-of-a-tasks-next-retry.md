---
type: added
breaking: false
audience: user
commits:
---
`forge control pin <plan-dir> <task> <spec>` sets the model a task's next full retry uses, once: the spec is checked with `forge-dispatch.sh doctor` (no quota), applied at the retry and written to `tasks.tsv`, and `escalations.tsv` gains a `source` column (`policy`, `retry` or `pin`) saying who decided. `forge control unpin` drops it; Fractal plans never apply pins.
