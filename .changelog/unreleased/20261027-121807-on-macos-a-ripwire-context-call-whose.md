---
type: fixed
breaking: false
audience: user
commits: 7046af4
---
On macOS, a Ripwire context call whose output was too long or that ran too long no longer fails with a PermissionError; it is skipped with its reason, as intended.
