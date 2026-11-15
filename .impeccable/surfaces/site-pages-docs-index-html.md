---
version: 1
slug: "site-pages-docs-index-html"
primary_target: "site/pages/docs/index.html"
related_targets: ["site/pages/docs"]
---

## Scope

forge's website docs (site/pages/docs/*.html). Visitor mode: **Read**. A surface inside the established Signal Box world (DESIGN.md); the world owns the frame, the reading column stays calm. The full reference stays in the repo's docs/ folder and every page links to its reference page.

## Audience, job, wayfinding

Newcomers learning how forge works and power users looking up a feature. The reader's question leads: what happens at this point of a run, what can I control, which command does it. Standard left sidebar for wayfinding, previous/next at the foot, a link to the full reference.

## Constraints

Same static build and assets as the home page. Plain language, short sentences, real commands and flags verified against docs/ and CHANGELOG. Covers what forge does today (0.18) and marks what is coming from open issues and the dev branch as coming, never as shipped.

## Direction contract

THESIS: the docs are a route map of a run. The index draws the run as an inlaid track diagram whose stations are the docs; every page carries the same track as a running head with its own station lit. It refuses the generic docs portal of cards and the wiki dump.

OWN-WORLD: the Signal Box frame: enamel ground, engraved plate group headings in the sidebar, black-glass code blocks, describer windows for flags and statuses, severity chips, lamps with fixed meanings. The reading column is a system sans at about 68ch on the module face, real contrast, no texture.

STORY: the reader sees the whole run at once, picks the station their question lives at, reads a plain explanation with the real commands, and moves along the track or out to the full reference.

FIRST VIEWPORT: header shared with home. Left sidebar of engraved plate groups. Index: title "How forge works", one framing paragraph, the route map (goal, plan, approval, dwarves, qa, retry, merge, integrate, plus memory, forge-ui and steering as side tracks) with an inspector column at right that explains the chosen station with its commands and links. Content pages: the route strip running head with the current station lit, then title, lede, column.

FORM: The route map (track diagram plus inspector), position 5 of 7 on my ordered list; seed:9bedd16a1cb2ffab. Signature move: the route strip running head, current station lit route white, visited stations hollow. Selecting a station on the map sets its route as forge-ui does: its lineage stays lit, the rest sinks.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
