---
type: fixed
breaking: false
audience: user
commits: 61a2507
---
A codex that never reaches the network ("workspace routing discovery failed", or a reconnect loop that runs out) is now an infrastructure stop. A task pauses as `INFRA` and `--infra-retries` waits it out, where it used to end as `ERROR`, and a whole-run review exits 8 instead of reporting a failed review (#114).
