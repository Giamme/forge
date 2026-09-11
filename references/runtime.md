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
can resume without a new dispatch; ordinary failed and interrupted work requires explicit
retry. Fractal has the additional recovery checkpoints described below.

Review repositories import self-contained tree object packs and create two commits before
checking out the final tree. No alternates or source repository access is required afterward.
Both exact tree comparisons and the existing fingerprint checks remain mandatory.

Telemetry is independent of project memory. Dispatch and pipeline attempt records are nested;
their total durations must not be added together. Unsupported usage remains unknown. See
[the evaluation](../tests/benchmark-2026-09-07.md) for observed improvements and regressions.

## Optional Fractal adapter decision

Fractal adds a Python adapter around the existing shell pipeline instead of replacing
Forge's dispatcher, model registry, QA or integration rules. This keeps provider effort,
permission and attempt-record behavior in one place. The isolated runtime pins Fractal
1.2.0 and registers `ForgeAgent` through upstream `agents.py`; no Fractal fork is required.
Ordinary execution and inspection do not require that runtime to be installed.

`forge-fractal-options.sh` records the per-run choice and selects the bridge only when
enabled. `forge_fractal/selection.py` freezes routing and limits; `execution.py` manages
bounded children, shared model slots, process ownership and candidate import checks;
`pipeline.py` preserves Forge checkpoints for resume and explicit retry. Work steps use
fresh dispatcher invocations rather than provider conversation continuation.

Each task separates a Fractal control repository from product snapshot repositories.
That separation prevents wiki/configuration/ledger initialization from entering the product
diff. Solo imports check source fingerprints and preserve the user's index. Decomposed
candidates return to the task's existing pinned review and integration boundary. Explicit
retry retains prior work and the original Forge QA baseline while recording a new attempt.

Runtime and durable run records live under
`${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/`, outside the product repository.
A single task coordinator and a run-wide invocation limiter bound nested work and QA;
setup and shutdown count toward the task deadline, explicit pauses do not. Fractal
execution status, Forge acceptance and repository verification remain separate records.

`inspection.py` reads curated managed artifacts and the pinned SQLite ledger without
constructing lifecycle-reconciling Fractal nodes or marking messages read. The browser
server is read-only and localhost-bound; portable HTML embeds captured data and assets.
Unsupported tokens/cost/tool details stay unknown, and the bridge rejects dollar caps.
See [operating instructions](fractal.md) and [verification evidence](../tests/fractal-verification.md).
