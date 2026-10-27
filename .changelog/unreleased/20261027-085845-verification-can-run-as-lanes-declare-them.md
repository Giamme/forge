---
type: added
breaking: false
audience: user
commits: 88a6ed1, 7b12ee5, 638640a, 2577fcf, 2f612bf, 4b51cc6, 6f96902, 46ecd90, 042eef9, 62a07d0, 5d62f18, 939842f
---
Verification can run as lanes: declare them in `.forge/verify.d/` (one file per lane) or with `run --verify-lane 'name[options]=command'`, and forge runs them side by side (`--verify-parallel`, 3 by default), reruns only a lane that failed, and calls a lane that passed on rerun flaky. Lanes can read JUnit or TAP reports to name the failing tests, and a baseline of known failures keeps those from failing the run. The run summary and the whole-run review name each lane and its state.
