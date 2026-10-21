# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

People who run forge, the multi-model coding skill, on their own machines. The same person usually
launched the run they are looking at. forge-ui serves four jobs, all confirmed as primary:

- **Live watching**: a run is open beside the terminal, often on a second screen, while tasks move,
  retries happen and review findings land, so nobody has to read logs to know where a run stands.
- **Digging in afterwards**: opening a finished or failed run to learn why: which reviewer said what,
  on which line of which diff, which model failed, and whether to retry or accept a task.
- **Comparing models**: the Models leaderboard and the run cloud inform which models to route which
  kind of work to.
- **Showing forge off**: forge-ui's screenshots and animations are the README hero and forge's main
  visual pitch, so its views must also hold up as still images.

## Product Purpose

forge-ui is the opt-in, read-only local viewer for forge runs. forge plans a coding goal into tasks,
dispatches each task to an implementer model (a dwarf) in its own git worktree, has a reviewer model
(qa) read the real diff, retries failures with the reviewer's findings and merges only what passes.
forge-ui makes all of that visible: every run on one plate, each run's tasks in their planned waves
and as they really ran, and the prompt, model, timings, diff and P0–P3 findings behind every step.
Success: a user understands a run's state and the reason for any failure without opening a terminal.

## Positioning

The viewer of a system whose point is that a reviewer reads the code, not the claim. forge-ui shows
the evidence the reviewer saw (the exact diff each review read, each finding pinned to its line) and
measures models on the user's own runs, not on public benchmarks.

## Operating Context

- Runs on the user's machine: `forge ui` starts a small Python server, and the page is opened in a
  desktop browser. Started from forge's interactive prompt or by hand.
- Used both during long runs (minutes to hours, polled every few seconds) and after them.
- Used in daytime and evening sessions alike; both light and dark themes are used about equally, and
  the system setting decides.
- A run can be exported as one self-contained HTML file and shared.

## Capabilities and Constraints

- Read-only: forge-ui never changes a run, a repository or forge's state.
- Plain JavaScript and CSS served as static files: no bundler and no build step. three.js 0.160 loads
  from a CDN through an import map; without it the 2D views take over.
- A strict Content Security Policy: no inline scripts or styles, no `innerHTML`; a test enforces it.
  Keeping this setup is preferred but is not a hard limit.
- Views today: the map of all runs, the run cloud (five 3D layouts), a run's plan and execution
  views in 3D or 2D, the files heatmap, diffs with pinned findings, the inspector, the model
  leaderboard, quick search and notifications.
- Data comes from forge's run directories through a JSON API (`docs/api.md` in the ui folder).

## Brand Commitments

- The **plate-and-blocks 3D model** must survive: tasks as blocks on a plate in their waves, the finish
  nodes (integration, verification, final review) and dependency cables, ported from archivision.
- The **hairline-glow rendering** must survive: a dark hairline drawing where only what is live glows
  in an accent colour (ported from iso-glow and iso-figure).
- **forge's vocabulary** stays: dwarf, qa, run, task, attempt, wave, plate, finish line, Clef, shadow
  review, escalation, accept, P0–P3.
- The name is lowercase `forge`, and the viewer is `forge-ui`.

## Evidence on Hand

- `ui/tools/demo_runs.py` generates made-up runs (labelled demo data) for screenshots and trials.
- README media in `ui/docs/media/` (hero.webp, cloud.webp, run, execution, diff, models, files, map).
- Real runs on the user's machine; none of them may be published without the user's say-so.
- No testimonials, customers or benchmarks exist; none may be invented.

## Product Principles

1. Evidence over claims: show what the reviewer actually read and said, where it said it.
2. State at a glance, detail on demand: the plate answers "where is this run", the inspector answers
   "why".
3. Never act on the user's behalf: forge-ui reads and shows, it never changes runs.
4. Every visual has a meaning: colour, height, light and motion encode run state, never decoration.
5. Works for one run and for a hundred: views stay legible from a two-task solo run to a full cloud.

## Accessibility & Inclusion

Both themes are first-class. Keyboard access exists across the 3D stage, the files matrix, the diff
and quick search, and must be kept. `prefers-reduced-motion` is honoured in the 3D views. Status is
never shown by colour alone: text labels accompany every state.
