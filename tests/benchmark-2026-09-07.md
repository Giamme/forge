# Forge runtime evaluation — 2026-09-07

Local overhead improved substantially, but the capped live sample did **not** demonstrate
lower total model token use or end-to-end latency. All tested quality outcomes matched.

## Live comparison

Baseline: `f436210eab78c0006fe2b8c611236acb607a7679`. Implementer:
`sol:medium:codex`. Independent reviewer: `terra:xhigh:codex`. Both arms used the same
fixture contents, CLI environment, permission settings, and 180-second dispatch timeout.
Project memory was disabled. No bypass flags, implicit retries, model changes or effort
changes were used. Baseline JSON event instrumentation matched candidate output.

Exactly **24 dispatches** ran (12 per arm), including the budget allocation for failures;
all calls completed successfully. A locked append-only ledger enforced the cap.

| Measurement | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Sum of case wall times | 324.10 s | 358.02 s | +10.5% |
| Native input tokens | 967,313 | 1,093,119 | +13.0% |
| Native cached-input tokens, included in input | 786,048 | 851,072 | +8.3% |
| Input minus cached-input tokens | 181,265 | 242,047 | +33.5% |
| Native output tokens | 8,341 | 10,799 | +29.5% |
| All prompt bytes | 23,147 | 18,135 | −21.7% |
| QA prompt bytes | 17,107 | 11,881 | −30.5% |
| Returned runner output bytes | 33,028 | 8,350 | −74.7% |

Prompt/output bytes are **not token estimates**. Native tokens include the CLI's system
context and tool interactions. Cached input is a subset of input, not an additional count.
No monetary savings are established by these measurements. Runner output includes stderr;
complete backend logs and final responses remain on disk.

| Fixture | Baseline wall time | Candidate wall time | Quality |
|---|---:|---:|---|
| Solo Unicode slug (4 calls) | 76.90 s | 74.67 s | Both acceptance checks and QA PASS |
| Solo integer ranges (4 calls) | 78.92 s | 79.17 s | Both acceptance checks and QA PASS |
| Three-task parsing/formatting pipeline (12 calls) | 156.75 s | 189.97 s | All tasks merged; both combined acceptance checks PASS |
| Clean QA control (2 calls) | 6.15 s | 8.04 s | Both correctly PASS |
| Empty-input division defect (2 calls) | 5.38 s | 6.18 s | Both correctly FAIL |

All implementation acceptance scripts remained unchanged. Both reviewers identified the
known empty-input `ZeroDivisionError`; neither reported a defect in the clean control.
All four QA control snapshots remained clean. These checks establish the tested contracts,
not exhaustive correctness or universal quality parity.

The live parallel fixture has a consumer depending on **both** producers, so it does not
isolate the benefit of starting a consumer while an unrelated task remains active. The
separate offline fixture below does. The candidate's parallel implementers also attempted
combined acceptance before the other tasks had landed; the baseline implementers restricted
themselves to focused checks. This is observed extra work, not a complete causal explanation
of the token/latency difference. Model behavior and cache reuse varied between calls.

## Offline measurements on macOS Bash 3.2

| Measurement | Baseline | Candidate |
|---|---:|---:|
| Validate 5,000 characters with repeated spaces | 6.404 s | 0.0041 s |
| Validate 10,000 characters with repeated spaces | Timed out at 25 s | 0.0050 s |
| Prepare independent review snapshot, 2,000 files | 1.361 s | 0.171 s |
| Dependent start after prerequisite QA response, unrelated slow task | 4.503 s | 0.618 s |
| Total time for that offline scheduling fixture | 8.187 s | 6.511 s |

Snapshot baseline and final trees matched exactly, including a deletion and added symlink.
Scheduling used fake CLIs and measured from the prerequisite's saved QA response to the
consumer's model start; that interval includes fingerprint checks, merge and dispatch
preparation. The shared-cache regression used two tasks: eight preflight/dispatch sites
required only two help invocations, and changing the executable forced a new help read.
Registry, recipe and command-mode invalidation have separate regressions.

The final offline suite contains 49 tests. It passed locally on macOS with system Bash 3.2.
The existing CI matrix runs the suite on macOS and Linux. Linux was not executed in this
session: the local Docker daemon was unavailable.

## Reproducibility and limits

[Machine-readable measurements](benchmark-2026-09-07.json) include per-case data, native usage,
offline results, and SHA-256 hashes of the frozen live candidate scripts. Full fixtures,
revision copies, dispatch ledger, prompts, diffs, final responses and logs are retained at
`/private/tmp/forge-live-20260907`. Nothing was committed, pushed or integrated into the
Forge source branch by the evaluation.

The live candidate was frozen before the final edge-case cleanup. Subsequent changes protect
aliased prompt paths and stale-input metrics, retain CRLF context, reject zero capacity,
include metric finalization cost, record overall parallel phases, harden interruption cleanup,
and clarify scheduler code. Based on the observed redundant acceptance runs, the added
parallel implementation instruction now says to run **the task's requested verification**
instead of the project's checks. Those final changes have offline coverage but were not
remeasured live: the authorized 24-call cap is exhausted.

`tests/live_compare.py` reproduces the approved allocation in a fresh directory only when
explicitly authorized with `--execute`. `tests/bench_offline.py` measures validation and
snapshot overhead without model quota. This small, sequentially paired sample is sensitive
to provider latency, caching and model decisions; it is an observation, not a universal
performance guarantee.
