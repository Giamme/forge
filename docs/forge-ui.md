# forge-ui

[← README](../README.md) · [All docs](README.md)

forge-ui is a small local web page that shows runs live and, when you ask, changes them, as a 3D isometric map
ported from [archivision](https://github.com/Giamme/archivision). Its **run cloud** puts every
run in one full-screen view, grouped by project, in five layouts (helix, sphere, galaxy,
timeline, constellation), filtered down to exactly the runs you want; drag any run anywhere
and the cloud remembers it.

Runs are grouped by repository. A run started in any worktree of a repository shares that
repository's group, and each run shows the worktree and branch it started in.

<p align="center">
  <img src="../ui/docs/media/cloud.webp" width="880" alt="The run cloud: about seventy runs as blocks in five project spirals, orbited, then laid out as galaxies and as a timeline with one lane per project.">
</p>

<table>
<tr>
<td width="50%" valign="top"><img src="../ui/docs/media/run.png" alt="forge-ui's window: the runs list, a run's eight tasks on a 3D plate in their planned waves with dependency cables, finding marks and the finish nodes, and the run's inspector."><br><b>A run, as planned.</b> Tasks in their waves with dependency cables, ending in integration, verification and the final review. Click a task to open its attempts, dwarves and reviews.</td>
<td width="50%" valign="top"><img src="../ui/docs/media/execution.png" alt="The same run on wall-clock time: one block per attempt, failed attempts in red with their finding marks, a plane marking now, the concurrency strip, and the inspector's timeline and critical path."><br><b>And as it ran.</b> One block per attempt in the lane it ran in, failed ones in red, against <code>--max-parallel</code>; the inspector names the critical path.</td>
</tr>
<tr>
<td valign="top"><img src="../ui/docs/media/diff.png" alt="The diff a reviewer read, with a P1 finding pinned under the line comparing token digests and a P2 finding under the revoke function."><br><b>Findings on their lines.</b> Any diff of a task, or exactly the diff one reviewer read, with each P0–P3 finding under the line it names.</td>
<td valign="top"><img src="../ui/docs/media/conversation.png" alt="A dwarf's conversation: its thinking, a search, the files it wrote, a test run that failed with exit 1 and the run that passed after the fix."><br><b>What each agent said and ran.</b> A dwarf's or reviewer's conversation from its CLI's log: commands with their exit and output, edits linked to the diff, its thinking; searchable, errors one key apart, live while it runs.</td>
</tr>
<tr>
<td valign="top"><img src="../ui/docs/media/models.png" alt="The model leaderboard: per implementer, tasks, first-attempt pass rate with a 95% interval bar, retries, escalations, timeouts, accepts, median model time and tokens."><br><b>Models, measured on your runs.</b> First-attempt pass rate with a confidence interval, retries, escalations, time and tokens; reviewers' verdicts and shadow agreement.</td>
<td valign="top"><img src="../ui/docs/media/files.png" alt="The files heatmap: files grouped by directory against tasks grouped by wave, with planned, touched, drift and same-wave overlap marks."><br><b>Who touched which file.</b> Planned, changed, drifted into or conflicted on, wave by wave; a cell opens that file's diff.</td>
</tr>
<tr>
<td colspan="2" valign="top" align="center"><img width="50%" src="../ui/docs/media/map.png" alt="The map of every run: one block per run in a district per repo, green when done, amber while running, red with failures."><br><b>Every run, by repo.</b> One block per run with a lamp per task, filtered by state, repo, model, age or review verdict.</td>
</tr>
</table>

The page reads like a signal box's desk: engraved plates for headings, push buttons with lamps
for switches, and lamps that keep one meaning everywhere (amber running, green merged, red failed,
unlit pending). Tasks merged with `accept` show as accepted (hollow green) rather than failed.
A final review superseded by tasks merged into the integration branch shows as OUTDATED
(hollow unlit) instead of as the run's verdict; changes on the user's own branch after an
integrate are not tracked. Two layouts share it. *Immersive*, the default, floats the runs, the inspector and the tasks
over a full-window stage (on a first visit the inspector and the tasks start folded, so a run
opens on its plate); *Workbench* docks them beside it with splitters. `1`, `2` and
`3` fold each module away, the header switches layout and theme (system, light or dark), and the
task and model tables sort, reorder and hide columns. forge-ui remembers all of it in your browser.

On the 3D stage every review finding stands over its block as a mark shaped by its severity, and
a click opens the diff at that finding. A chosen task keeps its route lit, a hover shows its card
(attempts, time, tokens, worst finding), and merges, failed attempts and escalations play over the
plate as they happen or as a replay passes them. The *View* menu switches each part of the drawing
on or off; the keys at the stage's edge fit, zoom, switch between perspective, isometric and
top-down, open a legend of what is drawn, and `?` lists every key.

Each dwarf's and reviewer's conversation opens from its attempt: what it said, what it ran and
with what exit, what it changed (with a link into the diff), its thinking folded, and the final
answer with tokens and cost, searchable, with its errors one key apart and, while it runs,
following its log as it grows. It is read from the agent CLI's own log, whichever harness
wrote it (as plain text for those that log no events yet), or for an older claude run from
Claude's session file on this machine.

The *Verification* node opens the run's verification in the inspector. While it runs you see
for how long, the step a lane announced (`::forge step`, see
[verification lanes](../references/decompose.md#execution-checks-and-integration-verification))
and its last printed line, a warning after five minutes with no output, and the live output of
the lane that printed last (or of the one you choose). Once it fails, the failing tests come
first, by lane and marked new or known (from the lanes' JUnit or TAP reports), each with its
message, an excerpt and a link that opens the lane's log at that line; a lane without reports
shows the end of its log and the step it ended in. Below, every lane with its state (flaky and
known called out), time, runs and test counts; a lane's name opens its output. A single verify
command shows the newest of its logs. On the *Execution*
view the verification has rows of its own after the tasks, one per lane with a bar per run and
reruns marked (a single command's runs sit back to back, dashed, because only their durations
are known), and its finish node counts the lanes and reruns.

The task table's *Now* column, and *Now* at the top of a task's inspector, say where each task
is. A running task names the dwarf or reviewer at work, the command it is running (and whether
it is a test, type check, lint or build) and for how long, and flags *quiet N min* once its log
has said nothing for five minutes. A task that is not running says why: the tasks it waits on, a
running task it shares files with, no free slot, a drain or stop, an infrastructure pause with
its countdown, or the retry policy's stop with the split it proposes. The run's inspector says
what the scheduler is doing and how many tasks wait on what.

Under each attempt's dwarf and reviewer are the checks they ran, read from their logs: each
test, type check, lint or build with its exit, its time and the counts it printed, a failed
one's last lines, and a link to that command in the conversation. Each attempt then says which
of the commands its brief's `## Acceptance criteria` names ran, ran another way, or never ran.
When a failed command met an environment problem (Docker down or out of address pools, a port in
use, the sandbox's EPERM, a refused local service, no network, a full disk, a missing command)
the attempt says so, and when more than one task met the same one, a banner in the run's header
lists them. codex and grok log no times, so forge keeps a small clock beside each agent's log
(`<role>.clock`, off with `FORGE_LOG_CLOCK=off`); runs from before it show their checks without
times.

A run with a [finding sweep](reviews.md#fixing-them-in-the-run-the-finding-sweep-opt-in) shows its fix
tasks like any other. A fix task's inspector lists the findings it took, each with a lamp for what
its review said (fixed, not fixed, not reported), and links each to the task it came from. That task
shows which of its findings the sweep took and where they stand. The run's *Finish* says how many
the sweep fixed and lists what it left for `forge issues`, with each reason.

A review the reviewer passed but forge failed, because it listed a blocking CONFIRMED finding, shows a red `forge: FAIL (reviewer said PASS)` chip in the *Review findings* section.

### Controls

forge-ui reads everything, and it changes things only by calling forge's own commands: it
never writes a run file, a repo or a config itself. That keeps one set of rules (validation,
locks, the audit log) whichever way you change something, and a change from the page is
indistinguishable from the same command typed in a terminal, except that `control.log` says
`ui` instead of `cli`. Every change asks you to confirm and says when it takes effect.

- **Run controls** (a plan's inspector): the QA threshold (shown with its source, `P2 (default)`
  or `P1 (set)`); it accepts `P0`–`P3`, *Default* (drop the setting; P2) and *None* (P3, nothing
  tolerated), the per-attempt timeout, how many
  failures before escalating, the effort step, verification retries, the infrastructure backoff,
  and, while a scheduler is live, the retry and infrastructure-retry budgets and max parallel.
  A live run also gets **Drain** (start nothing new; *Resume* ends it), **Stop** (finish what
  runs, then end the run) and **Stop now** (interrupt the running tasks), and one line per
  infrastructure pause: the model or harness it holds, the class, its tasks, and a countdown to
  its next try, or *budget used up*. A task has its own panel for a threshold override
  (*Inherit*, or a level; *None* means P3 for that task), its own retry budget, a
  **model pin**: the model its next full retry uses, once, and, on a live run, **Retry now**
  for a task that stopped or failed, on the policy's choice or a model you name. A retry is
  queued; the panel says *queued*, then *applied* or *refused* with the scheduler's reason.
  Each is `forge control`; see [decomposed runs](decomposed-runs.md#changing-a-run-while-it-works).
  A control that cannot apply (a budget with no live scheduler, a merged task) is disabled and
  says why.
- **Open findings:** the review findings a run left open (tolerated by `--qa-threshold`,
  PLAUSIBLE, or left by the whole-run review), for this run or every run of its repository.
  Findings of an outdated review carry an "outdated review" marker, findings a person
  accepted have origin `accepted`, and findings the run's finding sweep fixed are *swept*: listed,
  never filed. Select some and file them as GitHub issues, or dismiss the
  ones that need no work (and take that back). It runs `forge issues`, so a finding is never
  filed twice.
- **Delete a run:** a dialog that first shows a dry run, then deletes the run directory
  through `forge runs rm`. The dataset keeps the run's traces, and by default a `.tar.gz` of
  them too; you can also remove its clean worktrees and its branches that nothing has checked
  out. Anything that is not the run's own, or has uncommitted changes, is listed and left alone.
- **Settings:** every config source in one view (`forge config show`) and edits for the
  per-repo ones (`repo.verify`, `repo.setup`, `repo.verify-subset`, and Jev on or off). The
  registry stays read-only: the skill directory is often a live symlink to a checkout. You pick
  the repository through one of its runs, from a list grouped by repository that shows each
  run's worktree and branch (arrow keys and typing work in it), and the page names the
  directory the configuration was read from.

Writing is protected, because anything that can reach the server could otherwise change your
runs. The server makes a random token each time it starts and puts it in the page it serves;
the page sends it with every change, and the server also checks the `Host` and `Origin`
headers, so another website open in your browser cannot make your browser change a run. Reading
needs no token. `--read-only` (or `FORGE_UI_READONLY=1`) turns writing off altogether: no
token is issued and every change is refused. `FORGE_UI_READONLY=1 forge ui open` starts such a
server, and the launcher never reuses a server running in the other mode; it replaces it.

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
four idle hours (`FORGE_UI_IDLE_S`). A UI that cannot start never fails the run. Reading needs
no token: any local process can read the prompts and diffs of the runs it shows; only changes
need the token (see [Controls](#controls)). See
[references/ui.md](../references/ui.md) and [ui/README.md](../ui/README.md).

To look around before your first run, point it at made-up runs (the ones in these pictures), from a git checkout:

```sh
python3 ui/tools/demo_runs.py /tmp/forge-demo
(cd ui && python3 -m forge_ui --no-registry --root /tmp/forge-demo --open)
```

## `forge ui` command group

| Command | Purpose |
| --- | --- |
| `open [DIR] [--no-browser] [--port N]` | start or reuse the shared forge-ui server (never one in the other mode); print `forge-ui: <url>` for the plan or solo run dir `DIR` (the run list without it) and open a browser the first time (a later `open` of the same run says on stderr that it opened none) |
| `status [--json]` | exit 0 and describe the server (and whether it is read-only) when it runs, 1 when not |
| `stop` | stop the server |

`FORGE_UI=off` makes `--ui` on a runner start nothing (it does not stop a running server or block `forge ui open`; use `forge ui stop`), `FORGE_UI_BROWSER=off` never opens a browser,
`FORGE_UI_PORT` (8765) and `FORGE_UI_IDLE_S` (14400, `0` = never) tune the server, and `FORGE_UI_READONLY=1` starts it read-only. State and
`server.log` are in `${XDG_STATE_HOME:-~/.local/state}/forge/ui/`.
