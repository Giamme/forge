---
type: fixed
breaking: false
audience: user
commits: 3f6bf20, d8647a1, c81c5c7
---
Interrupting `forge-parallel.sh run` (Ctrl-C or SIGTERM) no longer goes on to verify a half-merged integration branch: the run stops, frees the plan lock within seconds, exits 130 and prints the command that continues each interrupted task. A signal during verification stops the whole verify command and skips its rerun.
