---
type: fixed
breaking: false
audience: user
---
`forge install` now resolves junction and symlink targets physically (`cd -P ... && pwd -P`) so re-installing stays idempotent when Git Bash mounts `/tmp` or uses 8.3 short paths on Windows, and documents Windows Job Object behavior for abrupt scheduler termination.
