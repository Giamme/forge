---
type: added
breaking: false
audience: user
commits: 62cbc2e, adcdb6b, b879307, 035a55b, b9f4349, ea068a4, f868a0a, 7610177
---
`forge-parallel.sh attribute <plan>` (or `run --attribute`) finds the merged task that broke the combined verification, by re-running the same verification at merge boundaries. With the new `attribution` gate on, the likeliest culprit is checked first.
