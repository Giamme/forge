# forge-ui

forge-ui is a local web page that shows a forge run live and can change it: a decomposed plan or a
solo run. It is opt-in per run. The orchestrator asks "Open forge-ui to watch this run?
[y/N]" once at the start of every new interactive run and passes the answer as `--ui` or
`--no-ui`. Nothing about the run depends on it: a UI that cannot start prints a note and the
run goes on, with the same exit code.

## What it shows

- **Map of every run:** every run forge has registered, one district per repo and one block per
  run with a lamp per task, filterable by state, repo, kind, model, age and review verdict.
- **Run cloud:** every run at once, full screen in 3D, grouped by project, in one of five layouts
  (helix, sphere, galaxy, timeline, constellation) as blocks or orbs. Filters narrow it to exactly
  the runs wanted (project, state, difficulty, tasks per run, models and more), and runs can be
  dragged anywhere; where they were dropped is remembered in that browser.
- **Files heatmap:** which tasks planned, changed, drifted into or conflicted on which files,
  with same-wave overlaps marked, and each file's diff a click away.
- **Model leaderboard:** first-attempt pass rates with confidence intervals, retries, escalations,
  timeouts, time and tokens per implementer, and verdicts, findings and shadow agreement per
  reviewer, filterable by project, difficulty and period.
- **Diffs with findings in place:** any task diff, the exact diff each reviewer read, or a final
  review's combined diff, with every P0–P3 finding shown under the line it names.
- **Fractal child tasks:** the tree a Fractal task was split into, in the inspector and above
  the opened task in 3D, each node with its decision, cost, output and candidate diff.
- **Controls:** change a live plan from its inspector: QA threshold, timeout, escalate-after,
  the effort step, verify retries, the infrastructure backoff, and (while a scheduler is live)
  the retry and infra-retry budgets, max parallel, Drain/Resume, Stop and Stop now; per task, a
  threshold override (`None` exempts it), a retry budget, a one-shot model pin and, on a live run,
  *Retry now* for a stopped or failed task (optionally on a chosen model), which shows its queued
  request until the scheduler applies or refuses it. Each infrastructure pause gets a line: what
  is paused, why, its tasks and the time left. Each is one `forge control` call; the change is
  confirmed first and logged in `control.log`.
- **Open findings and issues:** the findings a run (or all of its repository's runs) left open,
  filed as GitHub issues or dismissed through `forge issues`.
- **Delete a run:** a dry run, then `forge runs rm`: the dataset keeps the traces, an archive is
  written unless declined, and clean worktrees and unchecked-out branches can go with it.
- **Settings:** `forge config show` in one view, with edits for the per-repo files and Jev.
- **Export a run:** one HTML file to share that opens without forge-ui; prompts, diffs, reports
  and local paths stay out unless chosen.
- **Quick jump and notifications:** Ctrl K (⌘K) or `/` searches every run, task, project and
  model; the bell tells you when a run finishes or fails, a task fails or a final review gives its
  verdict, and can raise browser notifications while the tab is in the background.
- **Plan run:** tasks in their planned waves with dependency cables, then the finish line:
  integration, verification and the whole-run final review, each a node with its status. Tasks
  merged with `accept` show as accepted (hollow green), not failed. A final review superseded
  by later task merges into the integration branch shows as OUTDATED (hollow unlit) instead of
  as the run's verdict; changes on the user's own branch after an integrate are not tracked. The
  *Execution* view shows the same run on wall-clock time, one block per attempt in the lane it
  ran in, with projected blocks for what has not started and a strip of busy slots against
  `--max-parallel`; the verification follows on rows of its own, one per verification lane
  with each run and rerun.
- **Verification** (its finish node): live, the elapsed time, a lane's announced step
  (`::forge step`), its last line, a warning after five minutes without output and the live
  output; after a failure, the failing tests first, by lane, new or known (from JUnit/TAP
  reports), each opening the lane's log at its line, or a reportless lane's log tail; then every
  lane's state, time, runs and tests; a lane's name opens its output.
- **Where each task is:** the task table's *Now* column and a task's *Now* section: the command a
  running dwarf or reviewer runs and for how long, flagged *quiet* after five minutes with no
  output, or why a task waits (dependencies, shared files, no free slot, a drain or stop, an
  infrastructure pause with its countdown, the retry policy's stop and its split). The run
  inspector says what the scheduler is doing.
- **Checks:** each attempt's tests, type checks, lints and builds, read from its dwarf's and
  reviewer's logs, with exits, times and counts, a failed one's last lines, and which of the
  commands its `## Acceptance criteria` name never ran. codex and grok logs carry no times, so
  `forge-dispatch.sh` keeps `<role>.clock` (when the log grew, each second) beside each log for
  them; `FORGE_LOG_CLOCK=off` turns it off. An environment problem (Docker, a sandbox's EPERM, a
  port in use, …) that several tasks' failed commands met gets one banner in the run header.
- **Opening a node** shows its attempts, dwarves and reviews, and an inspector with the prompt
  that launched each one, the model, timings by phase, tokens, verdicts and the P0–P3 findings
  of every review (the final review's included).
- **Solo run:** one task: its attempts, specs, prompts and the final messages.
- It refreshes every two seconds while something runs (five otherwise) while the tab is
  visible, so a run can be watched as it goes. A run can also be replayed from its record.

The 3D view loads three.js from cdn.jsdelivr.net when first used; offline, the page shows the
same run in 2D. The full page description is in [ui/README.md](../ui/README.md).

## Commands

```bash
bash <skill_dir>/scripts/forge-parallel.sh plan "$PLAN" --repo "$REPO" ... --ui
bash <skill_dir>/scripts/forge-solo.sh "$FORGE_RUN" --repo "$REPO" --dwarf <spec> --ui
<skill_dir>/forge ui open [DIR] [--no-browser] [--port N]
<skill_dir>/forge ui status [--json]
<skill_dir>/forge ui stop
```

- `--ui` starts (or reuses) the server for this run and prints a `forge-ui: <url>` line;
  relay that URL to the user. `--no-ui` is the explicit "no". `plan --ui` prints the link
  under the task table. `forge-solo.sh --ui` is ignored with `--dry-run`.
- `forge ui open DIR` does the same for an existing plan or solo run dir; without `DIR` it
  opens the run list. `--no-browser` prints the link only. The browser opens the first time a
  run is opened, not on every `open`.
- `forge ui status` exits 0 when the server runs and 1 when not; `--json` prints
  `{"running": ...}` with pid, port, url, version, build, started and log.
- `forge ui stop` stops it; it exits 0 when nothing was running.

| Variable | Meaning |
|---|---|
| `FORGE_UI=off` | `--ui` on a runner starts nothing (a note is printed); `forge ui open` still starts the server |
| `FORGE_UI_BROWSER=off` | print the link, never open a browser |
| `FORGE_UI_PORT` | preferred port, default `8765`; `--port` wins; a taken port falls back to a free one |
| `FORGE_UI_IDLE_S` | idle seconds before the server exits, default `14400`; `0` never |
| `FORGE_RUN_REGISTRY=off` | runs stop registering themselves (`--ui` and `forge ui open DIR` still register the run they open) |

## One shared server

All runs share one detached server on `127.0.0.1`. A second `forge ui open` reuses it.
It is replaced when it runs old code: `open` compares the server's build (a hash of the
`forge_ui` Python files, reported by `/api/health`) with the checkout's, and when they differ,
or the registry directory changed, stops the old server and starts a new one. It exits by
itself after `FORGE_UI_IDLE_S` seconds without a request; the next `open` starts it again.
Concurrent launchers are serialised by a lock, so they never start two servers.

## State and logs

```text
${XDG_STATE_HOME:-~/.local/state}/forge/ui/
  server.json     pid, port, build, registry and the runs a browser was opened for
  server.log      the server's output; read it when the server did not start
  lock            serialises launchers
```

Every plan, run and solo run registers its run dir in forge's run registry
(`${XDG_CONFIG_HOME:-~/.config}/forge/runs/`), whether or not the UI was asked for: that is
how the map shows all of a repo's runs. `forge-parallel.sh run` also records the
`--max-parallel` it was given in `$PLAN/max_parallel`.

## Privacy

The server binds only `127.0.0.1`. **Reading needs no token**: any process on the machine,
under any user who can reach loopback, can read every run it knows, including prompts, diffs and
review text from private repos. A running server keeps serving them, including runs that were
later rerun with `--no-ui`.

**Changing needs one.** The server never writes a run file, a repo or a config itself: every
change is a call of forge's own CLI (`forge control`, `forge issues`, `forge runs rm`,
`forge config`), so validation, locks and the audit log are the CLI's. Each server start makes a
random token and puts it in the page it serves; every change must send it back, from a `Host` of
`127.0.0.1` or `localhost` and with an `Origin` equal to that host when the browser sends one, or
it is refused before any CLI runs. There are no CORS headers, so another site in your browser
can neither send nor read a change. Any process that can fetch the page can read the token, as
it can read the runs, so on a shared machine start the server **read-only**: `--read-only`
(`python3 -m forge_ui`) or `FORGE_UI_READONLY=1` (also for `forge ui open`). A read-only server
issues no token and answers every change with 405; its `/api/health` says `"controls": false`.
`forge ui open` never reuses a server in the other mode: it replaces it, as it does one running
old code, and `forge ui status` shows the mode.

Every run is registered, so a running server shows every run on the machine, not only those
opened with `--ui`. The 3D view fetches three.js from cdn.jsdelivr.net (no run data is sent;
choose 2D in the page to never contact it).

`--no-ui` and `FORGE_UI=off` only stop a *runner* from launching the server. They do not stop a
server that is already running, do not unregister runs, and do not block `forge ui open`. To
close the exposure, run `forge ui stop`. Do not forward the server's port.

The API contract is [ui/docs/api.md](../ui/docs/api.md).
