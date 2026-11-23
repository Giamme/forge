---
type: fixed
breaking: false
audience: user
commits: 
---
Merging a commit onto the integration branch (`accept`, a deferred `adopt` or `retry`, or a mid-run merge) now marks any prior `verification.status` (`PASS`, `FAIL` or `INTERRUPTED`) as `STALE` and clears `verification.exit` whenever `accepted.integration` moves past `verification.commit`, and `accept` notes that the merged result is unverified until `run` or `integrate` verifies it (#194).
