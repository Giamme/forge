---
type: fixed
breaking: false
audience: user
---
Starting a new verification now clears `verification.status` and `verification.exit` alongside `verification.commit`, `verification.runs.tsv`, and logs so stale results from an earlier verification never appear paired with the new commit while verification is still running.
