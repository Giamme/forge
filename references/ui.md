# forge-ui

forge-ui is a read-only local web page that shows a forge run live: a decomposed plan or a
solo run. It is opt-in per run. The orchestrator asks "Open forge-ui to watch this run?
[y/N]" once at the start of every new interactive run and passes the answer as `--ui` or
`--no-ui`. Nothing about the run depends on it: a UI that cannot start prints a note and the
run goes on, with the same exit code.

## What it shows

- **Run list:** every run forge has registered, newest first, with goal, repo, status counts
  and verification status.
- **Plan run:** tasks by wave with dependency edges, statuses, dwarf/QA models, attempts,
  escalations, reviewer verdicts, shadow reviews, routing exploration and Clef predictions.
- **Solo run:** one task: its status, dwarf and QA specs, attempts and the final messages.
- It refreshes every five seconds while the tab is visible, so a run can be watched as it goes.

The full page description is in [ui/README.md](../ui/README.md).

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
| `FORGE_UI=off` | `--ui` starts nothing (a note is printed) |
| `FORGE_UI_BROWSER=off` | print the link, never open a browser |
| `FORGE_UI_PORT` | preferred port, default `8765`; `--port` wins; a taken port falls back to a free one |
| `FORGE_UI_IDLE_S` | idle seconds before the server exits, default `14400`; `0` never |

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

Opening a run dir also registers it in forge's run registry
(`${XDG_CONFIG_HOME:-~/.config}/forge/runs/`), which is how the server finds it.

## Privacy

The server binds only `127.0.0.1` and serves read-only. It has **no token**: any process on
the machine, under any user who can reach loopback, can read the runs it knows, including
prompts, diffs and review text from private repos. Stop it with `forge ui stop` when that
matters, or run with `--no-ui` / `FORGE_UI=off`. Do not forward its port.

The API contract is [ui/docs/api.md](../ui/docs/api.md).
