---
type: added
breaking: false
audience: user
commits: 371538c, 6a344bc, 8172468, 2db091b, b22a958, f5cb7f0, 53b8c02, 6813c49, b23a4e1, c7b653f, 9cffef2, 2de33c1, d071ef2, ec68186, 31388ba
---
A task whose reviews keep failing with new findings now stops as `NEEDS_DECISION` and waits for you instead of retrying: `retry --decide retry=N` or `threshold=Pn`, `retry --dwarf <spec>`, `accept`, or a split in `tasks.tsv`. A task that reaches its cap on attempts across runs stops the same way (`plan --max-attempts N`, default 8; raised only with `forge control set max-attempts N --confirm`), and `plan --converge-window N` sets how many reviews the signal looks at (#69).
