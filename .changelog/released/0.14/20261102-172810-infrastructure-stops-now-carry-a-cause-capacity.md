---
type: added
breaking: false
audience: user
commits: 10aa7b4, 29e6304, 32f6360, 73eb788, 8b22dea, df0c2f4, f5920a1, ac9f913, bef01f1, d31fbc6, 3d93579, 542bfd7, 7547167, 454f20b, 0c663f5, f0d2be5, d299977, 8c73e48, da55bee, de07fe1, 680eb5f, fc006d4
highlight: true
---
Infrastructure stops now carry a cause (capacity, rate limit, usage limit, budget, login, network, provider down, an empty reply or a cancelled call), and each recovers its own way. When another model of the same tier can take the work, the task, or its review, moves there sideways at once, without spending an attempt or counting as an escalation; a limit that resets is waited out; and a stop only a person can clear, such as spent credits or an expired login, is marked "needs you" in the run summary, `forge control show` and forge-ui. Sideways moves are on for new plans; `plan --no-infra-fallback` turns them off (#81).
