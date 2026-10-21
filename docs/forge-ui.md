# forge-ui

[← README](../README.md) · [All docs](README.md)

forge-ui is a small read-only local web page that shows runs live, as a 3D isometric map
ported from [archivision](https://github.com/Giamme/archivision). Its **run cloud** puts every
run in one full-screen view, grouped by project, in five layouts (helix, sphere, galaxy,
timeline, constellation), filtered down to exactly the runs you want; drag any run anywhere
and the cloud remembers it.

<p align="center">
  <img src="../ui/docs/media/cloud.webp" width="880" alt="The run cloud: about seventy runs as blocks in five project spirals, orbited, then laid out as galaxies and as a timeline with one lane per project.">
</p>

<table>
<tr>
<td width="50%" valign="top"><img src="../ui/docs/media/run.png" alt="A run's eight tasks on a 3D plate in their planned waves, with dependency cables and the finish nodes, beside the run's inspector."><br><b>A run, as planned.</b> Tasks in their waves with dependency cables, ending in integration, verification and the final review. Click a task to open its attempts, dwarves and reviews.</td>
<td width="50%" valign="top"><img src="../ui/docs/media/execution.png" alt="The same run on wall-clock time: one block per attempt, failed attempts in red, a plane marking now, and the inspector's timeline and critical path."><br><b>And as it ran.</b> One block per attempt in the lane it ran in, failed ones in red, against <code>--max-parallel</code>; the inspector names the critical path.</td>
</tr>
<tr>
<td valign="top"><img src="../ui/docs/media/diff.png" alt="The diff a reviewer read, with a P1 finding pinned under the line comparing token digests and a P2 finding under the revoke function."><br><b>Findings on their lines.</b> Any diff of a task, or exactly the diff one reviewer read, with each P0–P3 finding under the line it names.</td>
<td valign="top"><img src="../ui/docs/media/models.png" alt="The model leaderboard: per implementer, tasks, first-attempt pass rate with a 95% interval bar, retries, escalations, timeouts, accepts, median model time and tokens."><br><b>Models, measured on your runs.</b> First-attempt pass rate with a confidence interval, retries, escalations, time and tokens; reviewers' verdicts and shadow agreement.</td>
</tr>
<tr>
<td valign="top"><img src="../ui/docs/media/files.png" alt="The files heatmap: files grouped by directory against tasks grouped by wave, with planned, touched, drift and same-wave overlap marks."><br><b>Who touched which file.</b> Planned, changed, drifted into or conflicted on, wave by wave; a cell opens that file's diff.</td>
<td valign="top"><img src="../ui/docs/media/map.png" alt="The map of every run: one block per run in a district per repo, green when done, blue while running, red with failures."><br><b>Every run, by repo.</b> One block per run with a lamp per task, filtered by state, repo, model, age or review verdict.</td>
</tr>
</table>

Also: replay, critical path, Fractal child-task trees, quick jump (Ctrl K, ⌘K on a Mac, or
`/`), a bell for runs that finish or fail, failed tasks and final-review verdicts (with opt-in
browser notifications), and a run exported as one HTML file that opens without forge-ui, with
prompts, diffs and local paths left out unless you choose them.

Offline (it loads three.js from a CDN for the 3D view) the same page works in 2D. It is opt-in.
The `/forge` skill asks "Open forge-ui to watch this run? [y/N]" at the start of every new
interactive run, or you pass it yourself:

```sh
bash scripts/forge-solo.sh "$RUN" --repo "$REPO" --dwarf sol --ui
bash scripts/forge-parallel.sh plan "$PLAN" --repo "$REPO" --decompose-level medium --ui
forge ui open [DIR]     # an existing run dir, or the run list
```

All runs share one detached server on `127.0.0.1:8765` (`FORGE_UI_PORT`) that exits after
four idle hours (`FORGE_UI_IDLE_S`). A UI that cannot start never fails the run. It has no
token: any local process can read the prompts and diffs of the runs it shows. See
[references/ui.md](../references/ui.md) and [ui/README.md](../ui/README.md).

To look around before your first run, point it at made-up runs (the ones in these pictures), from a git checkout:

```sh
python3 ui/tools/demo_runs.py /tmp/forge-demo
(cd ui && python3 -m forge_ui --no-registry --root /tmp/forge-demo --open)
```

## `forge ui` command group

| Command | Purpose |
| --- | --- |
| `open [DIR] [--no-browser] [--port N]` | start or reuse the shared forge-ui server; print `forge-ui: <url>` for the plan or solo run dir `DIR` (the run list without it) and open a browser the first time |
| `status [--json]` | exit 0 and describe the server when it runs, 1 when not |
| `stop` | stop the server |

`FORGE_UI=off` makes `--ui` on a runner start nothing (it does not stop a running server or block `forge ui open`; use `forge ui stop`), `FORGE_UI_BROWSER=off` never opens a browser,
`FORGE_UI_PORT` (8765) and `FORGE_UI_IDLE_S` (14400, `0` = never) tune the server. State and
`server.log` are in `${XDG_STATE_HOME:-~/.local/state}/forge/ui/`.
