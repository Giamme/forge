---
type: changed
breaking: false
audience: user
commits:
---
forge-ui is no longer read-only: it gains write endpoints and Controls panels for a run and for each task (QA threshold, timeout, escalate-after, verify retries, budgets, threshold override, retry budget, model pin). It only ever calls forge's own CLIs, and every change must carry a per-launch token and pass the Host and Origin checks. `--read-only` (or `FORGE_UI_READONLY=1`) turns writing off, and `forge ui open` never reuses a server running in the other mode.
