---
version: 1
slug: "site-pages-index-html"
primary_target: "site/pages/index.html"
related_targets: []
---

## Scope

forge's public website home page (site/pages/index.html), served by GitHub Pages. Visitor mode: **Persuade**. A new surface inside the established Signal Box world (DESIGN.md); forge-ui's look is inherited and rendered at full commitment. Keeps PRODUCT.md's brand commitments: plate-and-blocks 3D, hairline-glow drawing, forge's vocabulary, lowercase `forge`.

## Audience, job, action

Agent-CLI power users (Claude Code, Codex, OpenCode...) who want reliable results, and curious newcomers who must understand forge fast. They should leave believing two things: a second model reads the real diff and only what passes merges; and any model runs from any CLI. Action: install (`npm install -g @giamme/forge && forge install`), copyable in the first viewport; secondary: the docs.

## Proof and content

No screenshots or GIFs: every product visual is built in the page (three.js plate, HTML diffs, tables, the run cloud) from labelled demo data. Real facts only: registry aliases and effort ladders read from registry.tsv at build time, changelog dates, forge's own commit history (forge builds forge), open issues for what's next. No testimonials, customers or invented benchmarks.

## Constraints

Static site, no bundler; site/build.py assembles it. three.js 0.160 from cdn.jsdelivr.net through an import map, with an SVG/2D fallback when WebGL or the CDN is missing. Both themes finished; system decides, manual override. Reduced motion keeps every lit state and drops sweeps and auto-orbit. Code-led build: no image generation here.

## Direction contract

THESIS: the home page is one forge run replayed: a session transcript types itself out while the run's 3D plate lights each task as its line lands. It refuses the hero-plus-feature-grid template and screenshots of the product.

OWN-WORLD: Signal Box desk. Enamel grey-green ground in both themes, engraved D-DIN caps plates for every heading, black-glass describers and code, push-button banks with lamps, model name plates, three plies. Lamps keep one meaning: amber running, green merged, red failed, unlit pending, route white for selection. Severity ramp P0-P3 with shapes. Hairline-glow 3D plate with bloom on what is live.

STORY: the visitor watches a goal become four tasks routed to different models and CLIs, sees a reviewer fail one on a pinned P1 line, sees the retry pass and merge, then installs. Below, each act of the run opens into a section that proves it: the diff, the spec composer, the approval table, steering, the run cloud, memory, safety, what's next, forge built with forge.

FIRST VIEWPORT: desk header (brand plate, version describer, nav bank). The 3D stage fills the panel edge to edge under it, plate center-right with tasks in waves. Floating ply-1 module top-left: headline "One command. Many models. Every diff reviewed." in large D-DIN caps, one-line lede, install glass with Copy key (primary action), Docs key. Floating ply-1 session module bottom-right: the transcript typing line by line, synced to the plate. Name tags over blocks show model and CLI.

FORM: The session (a run transcript replayed beside its plate), position 4 of 7 on my ordered list; seed:ba82326f603f7c9e. Signature interaction: transcript and plate in lockstep, each line lights its block, the P1 beacon drops on the failing task, the retry relights it green, merge cables glow; Replay key restarts; hovering a block shows its hover card. Second 3D moment: forge-ui's run cloud, live, with a layout bank (helix, sphere, galaxy, timeline). Motion grammar: relay steps for chrome (80ms linear), lamp blink 1Hz, plate camera drifts slowly and follows the pointer.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
