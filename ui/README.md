# forge-ui

forge-ui is a small, read-only local web UI for forge runs. It draws them the way
[archivision](https://github.com/Giamme/archivision) draws a codebase: a 3D isometric map of
blocks on a plate, with cables between them and lamps that light up as things happen.

- **A map of every run**, one district per repo and one block per run, with a lamp per task.
  Filter it by state, repo, kind, model, age or review verdict; what does not match fades
  instead of disappearing.
- **A run, as planned and as it ran.** *Planned waves* puts each task in its wave, with
  dependency cables, and ends in a *finish* line of three nodes: integration, verification
  and the whole-run **final review**. *Execution* lays the same work out on wall-clock time:
  one block per attempt, in the lane it really ran in, with its dwarf and review inside, the
  attempts that have not started projected by forge-schedule's own rules, and a strip of how
  many slots were busy against `--max-parallel`. Switching between the two moves each task
  from its wave to where it ran.
- **Click to open.** A task opens to show its attempts, each with its dwarf, its reviewer and
  any shadow reviews, linked; an escalation is a labelled cable between attempts. The
  inspector beside the stage shows what launched each step and what came of it: the exact
  prompt, the model (alias, harness, model, effort, clamping), the timings by phase, tokens,
  the verdict, and every **P0–P3 finding** of every review, the final review included.
- **Live.** Statuses change in place while a run works (every 2 s while something runs, 5 s
  otherwise); running nodes pulse and show their current phase.
- **Extras:** replay a run from its recorded times, light its critical path, set block height
  and colour by duration, tokens, attempts or model, and mark shadow reviews that disagreed
  with the primary and how sure Clef was that each task would pass.

The 3D view loads three.js from a CDN the first time it is used (see [Privacy](#privacy)).
Without it (offline, an old browser, no WebGL) the same page shows the 2D dependency graph,
an execution chart and the same inspector. The server is Python's standard library; there
is no install or build step.

It lives inside forge, in `ui/`, and is forge's companion: runs open it with `--ui`.

## Run it

Normally forge launches it. `forge ui open [DIR]` (from the forge checkout) starts or
reuses one shared detached server and prints the link to a run dir, `forge ui status`
shows it and `forge ui stop` ends it; see [references/ui.md](../references/ui.md). A run
starts it itself with `forge-solo.sh --ui` or `forge-parallel.sh plan --ui`.

To run the server directly, from `ui/`:

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
- `--idle-exit SECONDS`: exit after that many idle seconds; default `0` (never). `forge ui` sets it from `FORGE_UI_IDLE_S`.
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

Plan directories (containing a `tasks.tsv` file) and solo run directories (a `prompt.md`
and a `repo` file) appear through `--plan` and the registry; `--root` finds plan directories
only. Stale or invalid paths are skipped. Duplicate real paths appear once,
with the first source winning. Discovery and disk reads repeat on each API request.

## What you see

- **Map (`#/`):** every discovered run, newest first, grouped by repo. By default the last
  60 runs from the past 14 days; *older runs too* shows the rest. The sidebar list follows
  the same filter.
- **Run (`#/runs/<key>`):** a header (goal, repo, counts, and *Run details*: settings, request,
  waves, verification output), a toolbar (*Planned waves* / *Execution*, *3D* / *2D*, height
  and colour overlays, *Critical path*, *Review marks*, *Projected*, *Replay*), the stage with
  the inspector beside it, and the task table.
- **Inspector:** for the run, its timeline, critical path, peak concurrency and finish line;
  for a task, its models and routing, escalations, timings (busy, model time, overhead,
  queue wait after its dependencies merged), attempts and their executions, review findings
  with the gate decision, Clef predictions against outcomes, and its prompts; for one
  execution, its model, phases, tokens, exact prompt and final message; for the final
  review, its verdict, findings, report, prompt and history.
- **Keys on the stage:** ← → move between tasks, Esc closes, `e` switches plan and
  execution. Drag to orbit, right-drag to pan, scroll to zoom once the stage is clicked.

URLs such as `http://127.0.0.1:8765/#/runs/<key>/tasks/<id>` are shareable within the
machine while this server is running and discovering that run dir. *Live refresh* pauses
polling; a hidden tab never polls. Failed requests keep the last good data and show a banner.

## Layout of the code

- `forge_ui/reader.py`: discover runs, parse their files and produce summaries and details.
- `forge_ui/executions.py`: the per-dispatch records (`attempts/<role>-<uuid>/`), nested and
  matched to attempts, through a cache keyed by each file's identity and mtime.
- `forge_ui/findings.py`: P0–P3 findings and the gate, parsed by forge's own `scripts/forge-contract.py`.
- `forge_ui/server.py`: serve the JSON API and static assets, with GET/HEAD only.
- `forge_ui/__main__.py`: CLI options, local startup and optional browser opening.
- `forge_ui/__init__.py`: package version and the build id `forge ui` compares to find a stale server.
- `forge_ui/static/index.html`, `style.css`, `app.js`: the page shell, styles, routes, views and polling.
- `forge_ui/static/runmodel.js`: the pure run model (layouts, schedule projection, critical path, replay).
- `forge_ui/static/panel.js`, `gantt.js`, `graph.js`: the inspector, the execution chart and strip, the 2D graph.
- `forge_ui/static/map3d.js`, `scene3d.js`, `iso.js`: the 3D loader, scenes and engine (ported from archivision).
- `tests/`: reader, executions, endpoints, server, static-script and browser checks; `run_fixture.py`
  builds realistic runs in a temp dir (`python3 tests/run_fixture.py DIR` writes two you can open).
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): the licences of the ported and loaded code.

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

Use Python 3.9+ to run the server and tests. Node.js 18+ runs the JavaScript checks; use a
modern browser for the page (import maps: Chrome 89, Firefox 108, Safari 16.4). No Python or
Node packages need installing.

Run these from `ui/`:

```sh
python3 -m unittest discover -s tests          # includes the syntax checks and node --test
node --test tests/js/*.test.js                 # the run model alone
```

`tests/test_browser.py` drives the page in Chromium when Python Playwright is installed, and
skips otherwise. Its 3D tests need three.js: the CDN, or a local copy of the npm package
(`npm pack three@0.160.0`, unpacked) named by `FORGE_UI_TEST_THREE=<dir>/package`.

## Privacy

Run dirs can contain prompts, diffs and review text from private repos. The server binds
only `127.0.0.1`, makes no network requests, and writes nothing to run dirs, repositories or
forge's config. Anyone with access to the local server can read the runs it discovers, and
forge now registers every run, so that is every run on the machine.

The 3D view loads three.js 0.160.0 from `cdn.jsdelivr.net` the first time it is shown in a
tab: the browser asks the CDN for those script files and sends nothing about your runs
(`Referrer-Policy: no-referrer`, and the page's CSP allows only that one package). Choose
**2D** in the toolbar and the browser remembers it and never contacts the CDN.
