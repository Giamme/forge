# Runtime helper decision

The approved optimization plan permits Python 3 standard-library helpers while retaining
shell entrypoints and all existing model, retry and integration boundaries.

A complete Python runner would simplify process ownership, but would also rewrite artifact
capture, retry, merge, permission and setup behavior. The chosen design keeps those shell
contracts and adds focused helpers: `forge-prompt.py` assembles context without summarizing
requirements, `forge-schedule.py` coordinates eligible tasks (including opt-in in-run retries and
infrastructure pauses), `forge-runtime.py` handles capability caching, attempt telemetry and
infrastructure-failure classification, `forge-contract.py` owns the shared QA/dwarf prompt
contract and the severity gate, `forge-guard.py` the implementer guards, and
`forge-verify-coverage.py` the verify-coverage warnings. `forge-metrics.sh` connects pipeline timing to
existing shell stages. Python unit tests retain the repository's standard-library unittest
convention; no new dependency manager or test framework is required.

Tasks execute through `_task` and merge through `_merge`. The coordinator pins baselines
before launching tasks, excludes overlapping active ownership paths, and serializes merges.
Each merge still checks dependency status, reviewed commit, source fingerprint and accepted
integration revision. A plan lock spans run/retry/integrate operations. Saved passing work
can resume without a new dispatch; ordinary failed and interrupted work requires explicit
retry (or an opt-in `run --retry-failed N`), while tasks paused on an infrastructure failure
resume on the next `run` and an unjudged review re-runs QA only.

Review repositories import self-contained tree object packs and create two commits before
checking out the final tree. No alternates or source repository access is required afterward.
Both exact tree comparisons and the existing fingerprint checks remain mandatory.

Telemetry is independent of project memory. Dispatch and pipeline attempt records are nested;
their total durations must not be added together. Unsupported usage remains unknown. See
[the evaluation](../tests/benchmark-2026-09-07.md) for observed improvements and regressions.

## Infrastructure stops

A dispatch that fails for a reason other than the work exits 8, and its task stops with
status `INFRA`. `forge-runtime.py classify` gives the stop a class, and `forge-dispatch.sh`
writes it to `<role>.infra` as `class=`. `forge_infra.py` holds the one table of what each
class means for a recovery (scope, first move, what to do without a peer).

- `capacity`: the model is at capacity for everyone (overloaded, at capacity, HTTP 529).
- `rate_limit`: this account is sending too fast (rate limit, too many requests, HTTP 429).
- `usage_limit`: a spent subscription window, which resets (usage limit, hit your session or weekly limit).
- `budget`: money that is gone (insufficient credits, credit balance too low, billing, quota exceeded, HTTP 402).
- `auth`: credentials refused (401, 403, unauthorized, login required).
- `provider_down`: one provider failing while the connection works (bad gateway, service unavailable, gateway time-out, HTTP 502/503/504).
- `network`: local connectivity (ECONNRESET and similar codes, getaddrinfo, connection refused or reset, fetch failed, name resolution, certificate).
- `empty`: exit 0 with no final answer. `cancelled`: the CLI stopped a working model to ask for approval.

The order is auth > usage_limit > budget > capacity > rate_limit > provider_down > network, and
the first class with a matching phrase wins. A usage-limit message that also mentions credits
is `usage_limit`: the window resets, the money does not.

When the message gives a wait, `<role>.infra` also has `retry_after=<seconds>` and
`reset_at=<epoch>` (now plus the wait). Without a wait there is no `reset_at` line. The
scheduler copies the file into `infra.txt`, so both lines go with it.

Runs recorded before the split say `quota`, which `forge_infra.LEGACY` reads as `usage_limit`.
The scheduler still acts on the classes it was written for: `infra_info` folds `capacity` into
`rate_limit`, `usage_limit` and `budget` into `quota`, and `provider_down` into `network`, until
it reads the table.

## Recursive decomposition is native

Until 0.22 a task could run under Fractal, an external runtime behind a Python adapter. forge
0.23 removed it (#154) and splits tasks itself (#153), with the parts it already had: a split
task's subtasks are an ordinary plan in `<plan>/tasks/<id>/sub`, run by the same
`forge-parallel.sh run` and the same scheduler, so retries, escalation, reviews, controls and the
dataset apply to a subtask as to any task. `scripts/forge_decompose.py` holds what is new: the
node record (`decomposition.json`), the three limits and their freeze, the validation of a split
(ownership inside the parent's files, sibling-only deps, no cycle) and the tree readers use.
`scripts/forge-lib-decompose.sh` wires it into `plan`, `run`, `retry` and `next`.

One bound covers the tree. The root scheduler exports its `--max-parallel` and a slot directory
(`tree.slots/` in the root plan); every scheduler of the tree takes a slot for each task it
starts, and a split task gives its slot back while its subtasks run and takes one again for its
own review. Nesting cannot multiply the limit.

Old records stay readable. `scripts/forge_legacy.py` recognises a plan or solo directory made
with Fractal (`fractal-selection.json`), which `run`, `retry`, `split`, `next` and
`forge handoff resume` refuse, and reads the tier pools older plans kept in
`fractal-routing.json` (new plans write `dwarf-routing.json`). `forge du` reports what the old
runtime left under `${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/`.
