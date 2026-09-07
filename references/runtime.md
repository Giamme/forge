# Runtime helper decision

The approved optimization plan permits Python 3 standard-library helpers while retaining
shell entrypoints and all existing model, retry and integration boundaries.

A complete Python runner would simplify process ownership, but would also rewrite artifact
capture, retry, merge, permission and setup behavior. The chosen design keeps those shell
contracts and adds focused helpers: `forge-prompt.py` assembles context without summarizing
requirements, `forge-schedule.py` coordinates eligible tasks, and `forge-runtime.py` handles
capability caching and attempt telemetry. `forge-metrics.sh` connects pipeline timing to
existing shell stages. Python unit tests retain the repository's standard-library unittest
convention; no new dependency manager or test framework is required.

Tasks execute through `_task` and merge through `_merge`. The coordinator pins baselines
before launching tasks, excludes overlapping active ownership paths, and serializes merges.
Each merge still checks dependency status, reviewed commit, source fingerprint and accepted
integration revision. A plan lock spans run/retry/integrate operations. Saved passing work
can resume without a new dispatch; failed and interrupted work requires explicit retry.

Review repositories import self-contained tree object packs and create two commits before
checking out the final tree. No alternates or source repository access is required afterward.
Both exact tree comparisons and the existing fingerprint checks remain mandatory.

Telemetry is independent of project memory. Dispatch and pipeline attempt records are nested;
their total durations must not be added together. Unsupported usage remains unknown. See
[the evaluation](../tests/benchmark-2026-09-07.md) for observed improvements and regressions.
