---
version: 1
slug: "ui-forge-ui-static-index-html"
primary_target: "ui/forge_ui/static/index.html"
related_targets: []
---

## Scope

forge-ui, the whole app shell and every view: the map of all runs, the run cloud, a run (plan,
execution and files views, 3D and 2D), the inspector, diffs, the model leaderboard, quick search,
notifications and the export dialog. Visitor mode: **Operate**. Replaces the incumbent look; keeps
the plate-and-blocks 3D, the hairline-glow rendering and forge's vocabulary (PRODUCT.md).

## Audience and job

The person who launched forge runs, watching one live, digging into one afterwards, comparing models
or capturing the README hero. Success: run state and the reason for any failure readable without a
terminal, at a glance on the plate and in depth in the inspector.

## Constraints

Plain JS, no build step, strict CSP (no inline style or script, no innerHTML). three.js 0.160 from the
CDN. Both themes equally finished; the system setting decides, with a manual override. Two shells over
the same panels: Immersive (default, full-bleed stage, floating modules) and Workbench (docked panes,
splitters). Code-led build: no image generation in this environment.

## Direction contract

THESIS: forge-ui is a signal box route-setting desk for code. The run is an inlaid track diagram on an
enamelled panel; tasks are track sections whose lamps say clear, occupied or failed; selecting a task
sets its route and lights it end to end. It refuses the category's dark dashboard of cards, sidebars
and one blue accent.

OWN-WORLD: enamelled panel grounds (grey-green: pale in light, near-black green in dark) carrying the
3D plate as the inlaid diagram; lamp colours with fixed meanings (white route set, amber occupied and
running, green clear and merged, red failed, unlit pending); engraved label plates for every heading
(condensed DIN caps, white on black plates in light, black on bone in dark); square push buttons with
an integral round lamp for every toggle; recessed describer windows for numbers (tabular figures on
black glass). No fake screws, levers, rails or brushed metal.

STORY: the visitor sees which sections are occupied, clear or failed before reading a word, sets a
route on any task to see what it waits on and what waits on it, and reads why in the inspector module.

FIRST VIEWPORT: the run's plate fills the panel edge to edge under a thin desk header (forge plate,
MAP · CLOUD · MODELS push buttons, search, alarm lamp). A describer window top-left: goal, run id,
counters MERGED 5 · RUNNING 2 · PENDING 1, WALL 54:46. Runs module floats left, Inspector module right,
Tasks module docked at the bottom edge; a push-button bank centred under the plate: PLAN · EXEC · FILES,
3D · 2D, the layer lamps, FIT · ISO · TOP.

FORM: Signal Box (relay interlocking route-setting desks, the Italian ACEI panels), my top-ranked
grounded candidate (1 of 7), chosen as the pick card over the dealt Bench Instrument; seed key 71f3eac7.
Signature interaction: route setting (lineage focus) with lamps sweeping along the inlaid track in
order; Esc releases the route. Motion grammar: relay steps for chrome (80 ms, no easing), lamp flashes
at 1 Hz for running sections, sweeps for route setting; reduced motion keeps the lit state, drops the
sweep. Disciplines adopted from the round: exact encoding (finding shape = severity, fill =
confidence), ply is rank (panel, module, popover only), one name plate per model everywhere, fidelity
dropouts said on a describer window, one time base for replay.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the
verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved

- The engraving face: D-DIN (OFL) self-hosted as woff2, which needs `.woff2` serving and a `font-src`
  CSP; system condensed fallback.
- README media re-shoot after the build.
