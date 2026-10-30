---
type: added
breaking: false
audience: user
commits: a242cb6, d606514, 00e0aab, 001653e, 0d12b79, 0f62e45, 874bb28, 009c669, 116ed6c, a3c3e1e, 196c532, 81fa62a, 999d8be, cb4411f, 0caf283, 094e734, 826bb98, d54d64a
---
`forge decide` sets each decision-model gate (drift, forward, prompt, memory-curate, memory-slice, memory-stale, verify-discover) to off, shadow or on, for you or per repository (`.forge/decide.json`): `status`, `gate`, `provider`, `budget`, `privacy`, `doctor` and `migrate`. Shadow asks and records but changes nothing. Its first change migrates `jev.json`; until then every run behaves exactly as before. `forge decide privacy never-send --repo DIR` stops every gate from sending anything about that repository.
