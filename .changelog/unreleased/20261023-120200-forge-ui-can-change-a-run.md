---
type: changed
breaking: false
audience: user
commits: 1e41222 6ef8afb 62f301c 471dd00 3ddcb0c 05d1131 fbfe19c ed6805f 3969f25 4656fec e5dea2c 6ef3cfa eeafe0a 023e543
---
forge-ui is no longer read-only: it gains write endpoints and Controls panels for a run and for each task (QA threshold, timeout, escalate-after, verify retries, budgets, threshold override, retry budget, model pin). It only ever calls forge's own CLIs, and every change must carry a per-launch token and pass the Host and Origin checks. `--read-only` (or `FORGE_UI_READONLY=1`) turns writing off, and `forge ui open` never reuses a server running in the other mode.
