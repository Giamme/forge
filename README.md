# forge-ui

forge-ui is a small, read-only local web UI for forge runs: tasks and statuses,
waves and dependencies, branches, model choices with routing exploration and
propensities, attempts and escalations, reviewer verdicts and shadow reviews,
and Clef predictions. It runs on `127.0.0.1` using Python's standard library and
a static vanilla JavaScript page, with no install step, third-party packages or build step.

It is the companion of forge+.

## Run it

From the repo root:

```sh
python3 -m forge_ui
```

Open the printed URL (default `http://127.0.0.1:8765/`). Stop with Ctrl-C.

- `--plan DIR`: add one plan directory; repeatable, default none.
- `--root DIR`: scan for plan directories at depth 0–2; repeatable, default none.
- `--no-registry`: disable the registry, which is read by default.
- `--registry-dir DIR`: override the registry directory described below.
- `--port N`: use port N, default `8765`; `0` selects a free port.
- `--quiet`: suppress request logging; off by default (errors can still be logged).
- `--open`: open the browser after startup; off by default.
- `--version`: print the version and exit.
- `-h`, `--help`: print usage and exit.

Three examples (replace the example paths with your own):

```sh
# Registry only
python3 -m forge_ui
# One plan directory only
python3 -m forge_ui --no-registry --plan /path/to/plan
# A root containing many runs, without registry entries
python3 -m forge_ui --no-registry --root /path/to/runs
```

## Where runs come from

Sources are checked in this order:

1. Explicit `--plan DIR` directories.
2. `--root DIR` itself, its children and grandchildren. Scanning stops when a plan
   directory is found; hidden children and symlinked child directories are skipped.
3. Forge's registry: `*.txt` files in `$XDG_CONFIG_HOME/forge/runs/`, defaulting
   to `~/.config/forge/runs/` when XDG_CONFIG_HOME is unset or empty. Each file
   supplies absolute paths, one per line; blank lines and `#` comments are ignored.
   `--registry-dir` replaces this location; `--no-registry` disables it.

Only plan directories (directories containing a `tasks.tsv` file) appear; solo
runs do not. Stale or invalid paths are skipped. Duplicate real paths appear once,
with the first source winning. Discovery and disk reads repeat on each API request.

## What you see

- **Run list:** newest update first, with goal, repo name, run ID, status counts
  and verification status. Filter by goal, repo, ID or path.
- **Run view:** repo and plan paths, source, settings, counts and verification
  command; expandable request, verification output, waves, warnings and truncation notices.
- **Graph:** tasks in wave columns with dependency arrows, status colors and dwarf
  model specs. Exploratory picks have dashed outlines and `*` markers. Click a node
  (or use Enter/Space when focused) to select it; hover/focus highlights its edges.
- **Task table:** title, difficulty, wave, status, dwarf/QA models, attempt, verdict,
  exploratory picks and their propensities, dependency links and branch names.
  Select a row or task ID to open its panel.
- **Task panel:** files and metadata; routing candidates, default/chosen models,
  exploration, propensity, epsilon and inputs; attempts and escalations; primary
  review tail and shadow verdicts; Clef first-pass/retry probabilities and assignments;
  expandable prompt, approach and retry findings. Shadow agreement is compared only
  for the current attempt; otherwise it is unknown.

Auto-refresh fetches the run list and selected run every five seconds while the
tab is visible. Uncheck **Refresh every 5s** to pause polling; rechecking it or
returning to a visible tab refreshes when enabled. Navigation still loads the chosen
run while paused. Failed requests retain the last good data and show an error banner.

URLs such as `http://127.0.0.1:8765/#/runs/<key>/tasks/<id>` are shareable within
the machine while this server is running and discovering that plan directory.

## Layout of the code

- `forge_ui/reader.py`: discover plans, parse files and produce run summaries/details.
- `forge_ui/server.py`: serve the JSON API and static assets, with GET/HEAD only.
- `forge_ui/__main__.py`: CLI options, local startup and optional browser opening.
- `forge_ui/__init__.py`: package version.
- `forge_ui/static/index.html`: page shell and controls.
- `forge_ui/static/app.js`: API fetching, navigation, polling, tables and task panels.
- `forge_ui/static/style.css`: layout, light/dark themes and status colors.
- `forge_ui/static/graph.js`: SVG dependency graph and task selection.
- `tests/test_reader.py`, `tests/test_server.py`, `tests/fixtures/`: reader and server checks and sample plans.

[docs/api.md](docs/api.md) is the data contract between the reader, server and page.

### Known differences from docs/api.md

- Root discovery stops at a discovered plan and skips symlinked child directories;
  it does not visit every nested plan within depth 2.
- The reader accepts wave numbers only from 1 to 10,000 and warns about other values.
- List `updated` timestamps cover the directory and summary inputs; detail timestamps
  additionally cover files read for the task panels and other run details.
- The server rejects Host headers other than `127.0.0.1` or `localhost` (optional
  port) with `403`; an absent Host is accepted. This also precedes method rejection.
- Static filenames must contain only letters, digits, `_`, `-` and `.` and must
  not contain `..`; resolved files must be directly inside the static directory.

## Develop

Use Python 3.9+ to run the server and tests. Node.js 14+ supports the JavaScript
syntax used here and is needed only for the syntax checks below; use a modern
browser for the page. No Python or Node packages need installing.

From the repo root:

```sh
python3 -m unittest discover -s tests
for file in forge_ui/static/*.js; do node --check "$file" || exit; done
```

The JavaScript command checks each static script's syntax; it does not run browser tests.

## Privacy

Plan directories can contain prompts, diffs and review text from private repos.
The CLI server binds only `127.0.0.1`, serves local assets without CDN dependencies,
and writes nothing to plan directories, repositories or forge's config. Anyone
with access to the local server can read the discovered runs.
