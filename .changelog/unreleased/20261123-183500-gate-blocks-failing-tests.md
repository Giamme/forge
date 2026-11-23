---
type: fixed
breaking: false
audience: user
commits: 
---
A review that reports failing tests (`FORGE_TESTS: RAN — FAILED: <names>`, `FORGE_TESTS: FAILED — <names>`, or `FORGE_TESTS_FAILED: <names>`) now keeps a `FAIL` verdict from being accepted and turns a `PASS` into `FAIL`, even when every itemised finding is milder than the blocking threshold. Reviewers are asked to report tests failing because of the change as blocking `CONFIRMED` findings (while pre-existing unrelated failures stay in prose), and retries/summaries read the gate's reason directly so `PASS -> FAIL` overrides name the failing tests (#171).
