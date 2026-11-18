---
type: fixed
breaking: false
audience: user
commits: 3020e13
---
forge-ui no longer looks up the machine's full host name before it starts listening, which could delay its startup by several seconds on macOS.
