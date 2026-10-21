# Fractal execution

[← README](../README.md) · [All docs](README.md)

Fractal is an optional implementation backend for both solo and decomposed runs. It uses
the existing Forge dispatcher for Codex, Claude, OpenClaude, OpenCode and Antigravity.
Each work step gets a fresh model invocation; preserved files and child results provide
continuity when a parent resumes. Independent QA reviews the complete Forge task diff.

`--decompose-level` plans top-level Forge tasks before execution. `--fractal` lets an
implementer request nested children during execution. They can be used together, and
solo runs can use Fractal without a top-level decomposition.

## Install the optional runtime

```bash
forge fractal install --dry-run
forge fractal install --yes --with-prerequisites
forge fractal doctor --spec sol --spec opus --json
```

The managed runtime pins **Fractal 1.2.0** at revision
`18793200c0d7e8cdb2db369ea3abe5647a1e15e4`, checks the source archive checksum,
and includes the required `wiki` executable. It prefers an existing Python 3.12–3.14;
with prerequisite installation selected, it can provision uv, Python 3.13 and tmux.
tmux installation uses an existing Homebrew on macOS or apt on Debian/Ubuntu.
The [installation reference](../references/fractal.md#installation-and-provenance) covers
consent, supported bootstrap platforms, provenance and failure diagnostics.

Installation never enables Fractal for a run, adds no command and leaves your shell startup
files alone.
Doctor checks the runtime and advertised harness capabilities without calling a provider.

## Start and inspect a run

Through the skill:

```text
/forge "add retry with exponential backoff to the HTTP client" --dwarf sol:high --fractal
```

Or prepare a solo run directory outside the product repository and invoke the runner:

```bash
FORGE_RUN="$HOME/.local/state/forge/examples/http-client"
mkdir -p "$FORGE_RUN"
printf '%s\n' 'Add bounded exponential backoff to the HTTP client and run its tests.' > "$FORGE_RUN/prompt.md"
bash scripts/forge-solo.sh "$FORGE_RUN" --repo /absolute/path/to/product \
  --dwarf sol:high --qa opus --fractal --fractal-concurrency 3
```

Choose a new run directory for unrelated work. Backend selection, routing pools and limits
are stored with the run; resume and explicit retry retain them. For a prepared, approved
decomposed plan, select the backend on `run`:

```bash
bash scripts/forge-parallel.sh run /absolute/path/to/plan --fractal --dry-run
bash scripts/forge-parallel.sh run /absolute/path/to/plan --fractal
```

Dry runs show resolved model pools, limits and proposed workspaces without installation
or provider calls. When a selected runtime is unavailable, interactive runs offer
installation; unattended runs return an actionable error instead of changing backends.

Discover the managed run ID, then inspect it from another terminal:

```bash
forge fractal runs --repo /absolute/path/to/product --json
forge fractal open                         # live runs index
forge fractal status RUN_ID --json
forge fractal tree RUN_ID --json
forge fractal logs RUN_ID --follow
forge fractal report RUN_ID --html /absolute/path/to/report.html
```

`RUN_ID` is the `run-…` identifier returned by `runs`, not the solo directory or plan name.
The [run inspector](../references/fractal-dashboard.md) opens on progress and problems, then lets
readers inspect task QA/history or node decisions, logs and changes. It refreshes about every
two seconds without replacing the workspace; search, selection, focus and scrolling remain in
place. “Pause live updates” affects the browser only. Browser access is read-only, bound to
`127.0.0.1` with an ephemeral access token. Closing its server does not stop execution.
Portable HTML embeds complete captured history and logs, works offline and needs no Fractal
runtime to read.

## Automatic recursive decomposition

`--fractal` permits workers to request children. To require a planning decision before
every eligible node implements, use `--fractal-auto-decompose`:

```text
/forge "<goal>" --dwarf luna:high --qa sol:high --fractal-auto-decompose --fractal-planner sol:xhigh --fractal-depth 3 --fractal-nodes 24
```

After the top-level Forge approval, the planner automatically splits useful work or
records why it is atomic. Children repeat this decision within the approved ownership
and frozen limits. `--fractal-planner` selects a separate decision model; when omitted,
Forge reuses the configured top-level planner, then the task's initial root model.
Decisions use disposable snapshots, persist for resume, and appear in tree/status/HTML
reports. Ordinary `--fractal` and existing runs retain their current behavior. Start a
new run to change policy or planner. See the [full decision contract](../references/fractal.md#automatic-decomposition-decisions).

## Bounds, recovery and acceptance

Defaults per Forge task are two nesting levels below implementation, three unsettled
direct children per node, twelve lifetime nodes, six iterations per node per attempt,
and a 45-minute attempt deadline excluding explicit pauses. Three model slots are shared
across the whole Forge run, including QA. `--max-parallel` bounds top-level task pipelines;
`--fractal-concurrency` bounds model invocations across their nested trees.

Child difficulty selects a frozen `--dwarf-low`, `--dwarf-medium` or `--dwarf-high` pool,
falling back to the configured general dwarf, then the parent. Dependencies and owned
paths constrain admission; overlapping ownership is serialized. Fractal never implies
`--yolo-dwarf`. Costs are observational; unavailable usage stays unknown and dollar-cap
requests are rejected.

```bash
forge fractal pause RUN_ID
forge fractal resume RUN_ID
forge fractal stop RUN_ID
```

Pause and stop preserve work and diagnostics. Run-level resume recovers recorded execution
and the Forge pipeline without repeating accepted stages. Use explicit runner retry for
a new attempt, retaining work and the original Forge QA baseline. Controls also accept
`--task TASK_ID` and `--task TASK_ID --node NODE_ID`; those IDs come from `tree` output.

Managed data lives in `${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/`. Solo execution
uses an isolated snapshot and imports changes only after checking source drift, preserving
staging and leaving changes uncommitted. Decomposed candidates return to Forge's existing
review/integration pipeline. **Fractal completion is not Forge acceptance**: QA, repository
verification and authorization to update the user's branch remain distinct.

See the [full Fractal reference](../references/fractal.md) for limits, child requests and
recovery details, and [verification evidence](../tests/fractal-verification.md) for the tested
platforms and browser checks. No paid-provider or speed/cost claim follows from fake-CLI tests.

## `forge fractal` command group

Run it as `forge fractal`, or `forge fractal` from a git checkout.
This group manages Fractal runs; it does not replace the `/forge` skill or runner scripts.

| Command | Purpose |
| --- | --- |
| `install [--dry-run] [--yes] [--with-prerequisites]` | preview or install the isolated pinned runtime |
| `doctor [--spec SPEC] [--json]` | check runtime/hooks/tmux and optional repeated harness specs without provider calls |
| `runs [--repo DIR] [--json] [--html PATH]` | discover managed runs, optionally filtered by repository |
| `status\|tree\|activity RUN_ID` | inspect progress, hierarchy and history |
| `logs\|costs\|messages\|config RUN_ID` | inspect diagnostics, observed accounting, radio traffic and effective settings |
| `pause\|resume\|stop RUN_ID` | request a control and record its observed state |
| `open [RUN_ID] [--port PORT]` | open the read-only live dashboard or runs index |
| `report RUN_ID [--html PATH]` | capture a self-contained HTML report; default filename is `RUN_ID.html` |

Inspection commands accept `--task TASK_ID`, `--node NODE_ID`, `--json`, `--html PATH`,
`--offset N` and `--limit N` (1–1000). They currently return a common JSON run projection;
the command names do not imply separate output schemas. `logs --follow` follows changes.
HTML exports capture full history and logs. Controls require an explicit run ID, and a
node control also requires its task ID; they never guess the latest run. Use each command's
`--help` for its accepted options. Installation is also available through
`bash scripts/forge-install-fractal.sh`.

Fractal adds a managed workspace alongside the runner artifacts described here:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/
  runtime/                       isolated pinned Fractal and wiki environment
  provenance.json                installed versions and source checksum
  installs/                      installation attempts and diagnostics
  runs/<run-id>/run.json          effective routing, limits and runner binding
  runs/<run-id>/controls/         requested controls and observed results
  runs/<run-id>/tasks/<task-id>/
    request.json / state.json     task request and execution checkpoint
    initialization.json         control repository and Fractal ledger provenance
    control/                     Fractal metadata, separate from product changes
    nodes/<node-id>/product/      isolated product candidate
    nodes/<node-id>/steps/        fresh dispatcher attempts and raw logs
    outcome.json                 captured Forge acceptance and verification
```

The original solo/plan directory stores `fractal-selection.json`; decomposed plans also
store `fractal-routing.json`. Prefer `forge fractal runs`, `tree`, `logs` and `report`
to direct ledger access. Inspection and stop never delete managed work.
