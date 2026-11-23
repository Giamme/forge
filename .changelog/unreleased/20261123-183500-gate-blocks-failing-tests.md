---
type: fixed
breaking: false
audience: user
commits: 
---
A review that reports failing tests (`FORGE_TESTS: RAN — FAILED: <names>`, `FORGE_TESTS: FAILED — <names>`, or `FORGE_TESTS_FAILED: <names>`) now keeps a `FAIL` verdict from being accepted and turns a `PASS` into `FAIL`, even when every itemised finding is milder than the blocking threshold. Reviewers are also asked to report failing tests as blocking `CONFIRMED` findings instead of leaving them in prose alone, so sweep and task runs stop failing diffs before merge (#171).
