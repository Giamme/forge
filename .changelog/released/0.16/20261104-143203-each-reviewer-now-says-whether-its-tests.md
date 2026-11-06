---
type: added
breaking: false
audience: user
commits: 4b65cb4
---
Each reviewer now says whether its tests ran, with a `FORGE_TESTS: RAN`, `NOT NEEDED` or `COULD NOT RUN` line before its verdict. It never changes the verdict. The run summary lists reviews whose tests could not run, and forge-ui and the dataset record the answer (#117).
