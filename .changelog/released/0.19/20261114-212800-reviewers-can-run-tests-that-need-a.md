---
type: fixed
breaking: false
audience: user
commits: 0357bb6
---
Reviewers can run tests that need a temporary directory: every read-only dispatch gets a writable temp dir of its own outside the repository (`TMPDIR`, `TMP`, `TEMP`), removed when it ends. On codex the read-only sandbox now allows writes to that directory alone, so test runners such as Vitest no longer stop with `EPERM`.
