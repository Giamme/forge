# Opt-in Fractal execution

Forge can run its implementation stage through Fractal while continuing to own
model routing, independent QA of the cumulative diff, verification and integration.
Fractal execution completion and Forge acceptance are separate outcomes. Costs are
observational, and this integration makes no speed or cost-saving claim.

For a first run, follow the [README walkthrough](../README.md#fractal-execution).
This reference covers the exact selection, routing, lifecycle and inspection contracts.

## Contents

- [Selecting a run](#selecting-a-run)
- [Installation and provenance](#installation-and-provenance)
- [Bridge, routing and ownership](#bridge-routing-and-ownership)
- [Limits and lifecycle](#limits-and-lifecycle)
- [Inspection and controls](#inspection-and-controls)
- [Managed data and identifiers](#managed-data-and-identifiers)
- [Troubleshooting](#troubleshooting)
- [Verification](#verification)

## Selecting a run

```sh
bash scripts/forge-solo.sh /path/outside/repo/run --repo /path/to/repo \
  --dwarf sol --qa opus --fractal
bash scripts/forge-parallel.sh run /path/to/plan --fractal
```

Both runners accept mutually exclusive `--fractal` and `--no-fractal`. Either skips
the question. Without flags, interactive runs ask `Use Fractal for this run? [y/N]`;
unattended and dry runs default off. `/forge` asks conversationally and supplies the
answer explicitly. Installing software does not select a backend. A run directory
retains its choice for resume and explicit retry; a new directory starts a new choice.
A selected but unavailable runtime is an actionable preflight failure. Interactive
users can choose installation or explicitly choose ordinary execution.

Dry runs resolve pools and show limits and proposed managed paths. They neither
install dependencies nor invoke providers, including when inspecting an existing run.
Limits and routing pools are frozen with the run; changed settings require a new
run directory. A missing runtime on resume can be reinstalled, preserving the backend.

## Installation and provenance

```sh
./forge fractal install --dry-run
./forge fractal install --yes --with-prerequisites
./forge fractal doctor --spec sol --spec opus --json
```

`scripts/forge-install-fractal.sh` is equivalent to `forge fractal install`. Installation
uses an isolated uv environment under `$XDG_STATE_HOME/forge/fractal/runtime`, defaulting
to `~/.local/state/forge/fractal/runtime`. The pinned package is **Fractal 1.2.0**, revision
[`18793200c0d7e8cdb2db369ea3abe5647a1e15e4`](https://github.com/plasma-ai/fractal/tree/18793200c0d7e8cdb2db369ea3abe5647a1e15e4).
Its source archive is SHA-256 checked. The runtime includes the required `wiki` executable.
Installed dependency versions and provenance are recorded in `provenance.json`.

A compatible installed Python (3.12–3.14) is preferred; otherwise prerequisite
installation provisions managed Python 3.13. Missing uv is bootstrapped from the
official **0.8.22** release using platform-specific pinned checksums. Missing tmux is
installed using existing Homebrew on macOS or apt on Debian/Ubuntu. Other systems
receive concrete instructions. Unattended prerequisite installation requires both
`--yes` and `--with-prerequisites`; privilege errors retain diagnostics and the command
to run. No shell startup file, unrelated Python environment or existing Fractal
installation is modified. Repeated successful installation is a no-op. Partial
installations and diagnostics remain under `installs/`.

The ordinary Forge installer offers this as an optional step; declining or an
installation failure does not block ordinary Forge setup. The executable `./forge`
is always available locally and never replaces another installed `forge` command.

## Bridge, routing and ownership

The deployment's `agents.py` exports `ForgeAgent` using the pinned upstream extension
contract; there is no Fractal fork. Fractal's `Agent.invocation`, `Agent.spawn`, stream
parser and lifecycle ledger wrap existing `forge-dispatch.sh` calls. Codex, Claude,
OpenClaude, OpenCode and Antigravity keep their existing effort validation, permission
flags, prompt preparation, attempt records and raw logs. Selecting Fractal never
implies `--yolo-dwarf`.

Every work step is a fresh provider invocation. Parents receive preserved work and
child results on their next invocation, without requiring provider conversation resume.
The root implementation node covers coordination, implementation and requested checks.
Parents can return the following final-line protocol to request children and yield:

```text
FORGE_FRACTAL: {"children":[{"id":"parser","goal":"Implement and verify the parser","difficulty":"low","paths":["src/parser.py"],"deps":[]}]}
```

After requested work and verification, the node returns:

```text
FORGE_FRACTAL: {"done":true}
```

Child identifiers are unique for the task lifetime. Dependencies refer to siblings
that already exist or are declared in the same batch. Missing or cyclic dependencies,
ownership outside the parent, metadata ownership, and exhausted bounds are rejected
before dispatch. Overlapping ownership is serialized. A parent holds no model slot
while waiting for children; imports are serialized and checked against declared paths.

`--dwarf-low`, `--dwarf-medium` and `--dwarf-high` accept comma-separated pools. The
decomposed planner persists these pools; solo accepts them directly. Routing falls
back to the explicitly configured general dwarf pool, then the parent's resolved
dwarf. All eligible specs are preflighted and canonical resolutions frozen before
execution. Each node records its difficulty, requested spec and resolved model.

## Limits and lifecycle

| Runner option | Default | Scope |
| --- | --- | --- |
| `--fractal-depth` | 2 | Levels below the implementation node |
| `--fractal-children` | 3 | Unsettled direct children per node |
| `--fractal-nodes` | 12 | Lifetime nodes including implementation, per task |
| `--fractal-iterations` | 6 | Per node, per explicit execution attempt |
| `--fractal-concurrency` | 3 | Shared across all task trees and Forge QA |
| `--fractal-deadline` | 2700 seconds | Task execution attempt, excluding pauses |

Limits must be positive integers, except depth may be zero to disallow children.
Decomposed `--max-parallel` remains a separate limit on top-level task pipelines.
Increasing it does not increase the shared Fractal invocation limit.

`--fractal-max-cost` returns an unsupported-capability error: the bridge cannot
enforce a dollar cap. Unknown tokens, tools and cost data remain unknown.
The existing `--timeout` also bounds each implementation invocation when supplied;
the smaller of that timeout and the remaining task deadline reaches the dispatcher.

Execution repositories and durable records live under
`$XDG_STATE_HOME/forge/fractal/runs`. Each task has a Fractal control repository with
a slash-free `forge-root` branch, separate from product snapshot repositories.
Initialization trees and the ledger location are recorded in `initialization.json`;
Fractal metadata therefore cannot enter the product diff. Solo imports preserve
staging and remain uncommitted. Decomposed candidates return to the existing task
review/integration pipeline and retain the task's pinned baseline. The user's branch
still requires the existing explicit integration approval.

One coordinator owns each task tree, and runners retain a pipeline lock. Pause
terminates an in-flight model invocation and preserves its files; resume uses fresh
instructions. Stop reaps managed processes and preserves artifacts. Resume retains
baselines and accepted checkpoints. A stopped or interrupted pipeline can be resumed
through the CLI; explicit retry uses `forge-solo.sh ... --retry` or the existing
`forge-parallel.sh retry` command. Retry preserves work and records the previous attempt;
it cannot expand the frozen routing pool. Inspection and stopping never delete runs.

Timeout, iteration exhaustion, interruption, stop, source drift and Fractal completion
remain distinguishable in task/node records. A source drift blocks automatic import.

## Inspection and controls

```sh
./forge fractal runs --repo /path/to/repo --json
./forge fractal tree RUN --json
./forge fractal activity RUN --offset 100 --limit 100
./forge fractal logs RUN --task TASK --node NODE --follow
./forge fractal costs RUN
./forge fractal messages RUN
./forge fractal config RUN
./forge fractal pause RUN --task TASK --node NODE
./forge fractal resume RUN
./forge fractal stop RUN
./forge fractal open                 # runs index
./forge fractal open RUN             # live detail
./forge fractal report RUN --html /path/to/report.html
```

`status`, `tree`, `activity`, `logs`, `costs`, `messages` and `config` expose the same
captured, self-describing run projection with task/node selectors, `--json` and
`--html PATH`. A node control requires a task selector. Controls require an explicit
run ID and record both the requested action and observed state. Run discovery is
bounded to 1000 entries and returns at most 200 runs with corruption diagnostics.
Use run-level `resume` to recover the complete Forge pipeline through QA and integration.
A task/subtree selector resumes that execution scope and does not relaunch unrelated tasks.

The dashboard is served by Python's standard library on `127.0.0.1` with an ephemeral
access token. It is read-only, polls every two seconds, paginates history and loads
logs on demand. It displays copyable CLI controls, models, iteration, elapsed time,
limits, unknown costs, node hierarchy, candidate changes, QA and verification results.
Closing the browser server does not stop execution.

Ledger inspection uses SQLite `mode=ro` and `query_only`; it never constructs an
upstream Node for browsing, reconciles lifecycle state or marks radio messages read.
Only curated managed artifacts are exposed, and symlink/path escapes are rejected.
Portable reports embed captured data, complete logs and assets, escape artifact
content, display capture time and missing data, and work without Fractal or a server.
Credential directories are not part of the export allowlist.

Inspection verbs currently emit the same JSON run projection, with the selected task/node
scope; `--json` makes that intent explicit. Logs are loaded by `logs`, `report` or an HTML
export. Live text reads are capped at 256 KiB per artifact and include a truncation flag;
portable exports include complete logs and history. History offsets are zero-based and
CLI page limits must be between 1 and 1000. `report RUN` defaults to `RUN.html` when
`--html PATH` is omitted.

## Managed data and identifiers

All commands below can be invoked as `<forge-checkout>/forge fractal ...` when the
checkout is not the current directory. Installing the skill does not put a new `forge`
executable on your PATH.

| Location | Purpose |
| --- | --- |
| `<solo-run-or-plan>/fractal-selection.json` | selected backend and, when enabled, managed run ID |
| `<plan>/fractal-routing.json` | dwarf pools captured during decomposed planning |
| `<state>/forge/fractal/runtime/` | isolated Fractal and wiki runtime |
| `<state>/forge/fractal/provenance.json` | pinned revision, checksum and installed versions |
| `<state>/forge/fractal/installs/` | installation previews, logs and retained failures |
| `<state>/forge/fractal/runs/<run-id>/run.json` | effective repository, routing, limits and pipeline binding |
| `<state>/forge/fractal/runs/<run-id>/tasks/` | task checkpoints, control repositories, product candidates and step records |

`<state>` is `$XDG_STATE_HOME`, defaulting to `~/.local/state`. `runs --repo DIR` discovers
managed `run-…` identifiers. `tree RUN --json` reveals managed task IDs and node IDs;
task request records map back to their Forge runner output directories. These IDs differ
from a directory basename or the human task ID in `tasks.tsv`.

```bash
./forge fractal runs --repo /absolute/path/to/product --json
./forge fractal tree RUN_ID --json
./forge fractal status RUN_ID --task TASK_ID --node NODE_ID --json
./forge fractal logs RUN_ID --task TASK_ID --node NODE_ID --follow
./forge fractal report RUN_ID --task TASK_ID --html /absolute/path/to/task-report.html
```

Use the same scope to resume a pause or stop request. An active control on an ancestor
still applies to its descendants; resuming a child does not clear a run-level pause.
Run-level `resume` is the recovery entry point when the full Forge pipeline also needs
to continue. No inspection or control command deletes these records automatically.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Installed runtime, but a new run uses ordinary execution | explicitly pass `--fractal`; installation is never activation |
| Selected runtime is unavailable | inspect `doctor`, install the managed runtime, then resume or retry the same run |
| New pool, backend or limit rejected on resume | use the recorded settings; changed settings require a new run directory |
| `source_drift` or `scope_drift` | inspect preserved candidate changes and the source/owned paths; automatic import is blocked |
| `timeout` | inspect setup, per-step logs and elapsed task time; a resumed attempt retains elapsed time, while explicit retry starts a new attempt |
| Iterations exhausted | inspect the last response and preserved work; explicit retry starts a new bounded attempt |
| Execution completed, acceptance pending/failed | inspect Forge QA, verdict and verification records; completion alone does not establish acceptance |
| Dashboard stopped or access token no longer works | run `open` again for a new local server/token; execution continues independently |
| Missing or truncated live log data | inspect diagnostics; generate `report` for a complete capture of available artifacts |
| Dollar-cap request rejected | v1 cannot enforce cost caps; limits cover nodes, iterations, time and concurrency |

## Verification

The default unittest suite is provider-free. Set `FORGE_TEST_FRACTAL_RUNTIME` to an
isolated environment containing the pinned runtime to enable real Fractal/tmux
integration tests with fake CLIs. See [test instructions](../tests/README.md) and
[implementation verification evidence](../tests/fractal-verification.md).
Paid provider smoke tests and performance comparisons require separate explicit runs.
