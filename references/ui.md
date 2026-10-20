# forge-ui

forge-ui is a read-only local web page that shows a forge run live: a decomposed plan or a
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
- **Model leaderboard:** first-attempt pass rates with confidence intervals, retries, escalations,
  timeouts, time and tokens per implementer, and verdicts, findings and shadow agreement per
  reviewer, filterable by project, difficulty and period.
- **Diffs with findings in place:** any task diff, the exact diff each reviewer read, or a final
  review's combined diff, with every P0–P3 finding shown under the line it names.
- **Fractal child tasks:** the tree a Fractal task was split into, in the inspector and above
  the opened task in 3D, each node with its decision, cost, output and candidate diff.
- **Quick jump and notifications:** Ctrl K (⌘K) or `/` searches every run, task, project and
  model; the bell tells you when a run finishes or fails, a task fails or a final review gives its
  verdict, and can raise browser notifications while the tab is in the background.
- **Plan run:** tasks in their planned waves with dependency cables, then the finish line:
  integration, verification and the whole-run final review, each a node with its status. The
  *Execution* view shows the same run on wall-clock time, one block per attempt in the lane it
  ran in, with projected blocks for what has not started and a strip of busy slots against
  `--max-parallel`.
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

The server binds only `127.0.0.1` and serves read-only. It has **no token**: any process on
the machine, under any user who can reach loopback, can read every run it knows, including
prompts, diffs and review text from private repos. A running server keeps serving them,
including runs that were later rerun with `--no-ui`.

Every run is registered, so a running server shows every run on the machine, not only those
opened with `--ui`. The 3D view fetches three.js from cdn.jsdelivr.net (no run data is sent;
choose 2D in the page to never contact it).

`--no-ui` and `FORGE_UI=off` only stop a *runner* from launching the server. They do not stop a
server that is already running, do not unregister runs, and do not block `forge ui open`. To
close the exposure, run `forge ui stop`. Do not forward the server's port.

The API contract is [ui/docs/api.md](../ui/docs/api.md).
