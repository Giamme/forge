---
type: changed
breaking: false
audience: user
commits: 
---
A failed verification no longer reruns the whole suite to confirm a real failure: with `.forge/verify-subset`, forge first reruns only the failing test files, and runs everything again only if they pass. A `retry` that leaves tasks still to run skips its verification, since the next `run` or `integrate` verifies the branch (#112).
