# Fractal implementation verification

Verified on 11 September 2026 against Fractal **1.2.0**, revision
`18793200c0d7e8cdb2db369ea3abe5647a1e15e4`. All model invocations used fake CLIs;
no paid provider smoke test or performance comparison was run.

## Automated execution

| Environment | Command | Result |
| --- | --- | --- |
| macOS, Bash 3.2.57, Python 3.14.4, real tmux | `FORGE_TEST_FRACTAL_RUNTIME=/tmp/forge-fractal-test-runtime FORGE_RIPWIRE=off /bin/bash tests/check.sh` | 90 tests passed, 199.112 seconds |
| Linux container, Python 3.13, real tmux | `docker run --rm --init -v /Users/Ayoub/dev/wizaird/forge:/work:ro forge-fractal-tests:local` | 90 tests passed, 104.380 seconds |

The Linux image uses `python:3.13-slim`, Git, tmux, procps and system Python,
with the pinned source archive installed in `/opt/fractal` and
`FORGE_TEST_FRACTAL_RUNTIME=/opt/fractal`. Socket permissions are necessary for tmux.
The adapter suite is `tests/test_fractal.py`; the same invocation includes all existing
Forge dispatch, artifact, scheduler, installer, update, memory and Ripwire regressions.

Coverage includes opt-in persistence; installation consent, dry runs, checksums,
failure retention and idempotence; all five bridge harnesses; nested children and a
grandchild at depth two; sibling dependencies and overlapping ownership; shared
concurrency; dirty solo snapshots with staging and binary/new files; source drift;
metadata exclusion; cumulative independent QA; decomposed task integration;
pause/resume during implementation and QA; stop; worker-crash recovery through QA;
explicit retry preserving work; accepted checkpoint reuse; setup deadlines; step
timeouts and distinct iteration exhaustion; read-only inspection and complete HTML
exports containing large logs and hostile artifact text.

## Installation and browser checks

A real installation into `/tmp/forge-fractal-install-check` verified the pinned
archive, isolated runtime and `wiki` executable. Doctor reported ready with wiki
1.3.1. Repeating installation returned “already installed; no changes”. Missing
prerequisite installation and privilege failures were tested with mocks; Homebrew
or apt did not install system packages on the host.

Safari rendered synthetic runs for empty, active, paused, failed and completed
states. Execution, Forge acceptance and verification stayed visibly separate.
Keyboard navigation expanded an eight-level tree with visible focus. Large logs
loaded on demand and artifact `<script>` text remained literal text. The portable
1.5 MB report rendered its capture timestamp and embedded data through a plain
static file server, without Fractal APIs. Direct `file:` opening was not verified:
Safari's file chooser disabled Open in this environment. The native checks were
desktop checks; no mobile-browser or screen-reader certification is claimed.

HTTP probes verified invalid-token and foreign-Host rejection, traversal rejection,
and rejection of POST writes. Hashes of all managed fixture files were identical
before and after those requests. Unit tests separately verify ledger inspection
does not modify records or message-read state and reject symlink escapes.

## Final review

The final review checked source drift and staging boundaries, retry/checkpoint
semantics, process ownership, provider permissions, frozen routing, deadline
propagation, read-only ledger access and artifact escaping. `git diff --check`
passed. Ripwire quality-delta's remaining gate is historical short-horizon churn
on the existing `do_task` function; it is not a failing execution test. Its dynamic
shell/Python impact mapping does not replace the actual regression suite.

Provider authentication, real provider event variations and speed/cost measurements
remain separate explicit runs. Unknown accounting stays unknown; dollar caps are
rejected rather than presented as enforced.
