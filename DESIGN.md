---
name: forge-ui
description: A signal box route-setting desk for forge runs, read only.
colors:
  desk: "light-dark(#c9d0c7, #0d110f)"
  stage-bg: "light-dark(#c6cec4, #0e1210)"
  module: "light-dark(#f1f3ee, #171d1a)"
  module-2: "light-dark(#e4e8e1, #1e2521)"
  key: "light-dark(#f8f9f5, #222a26)"
  key-hover: "light-dark(#ffffff, #29322d)"
  field: "light-dark(#fbfcf9, #101513)"
  wash: "light-dark(#ffffff, #26302b)"
  glass: "light-dark(#121715, #070a08)"
  glass-ink: "#ece8d9"
  glass-dim: "#8f9a92"
  ink: "light-dark(#111613, #e4e8e2)"
  ink-2: "light-dark(#38413b, #bac3bd)"
  muted: "light-dark(#4b554e, #8f9b94)"
  rule: "light-dark(#c1c8bf, #29322d)"
  rule-2: "light-dark(#97a098, #3e4943)"
  plate: "light-dark(#151a17, #e2ddcd)"
  plate-ink: "light-dark(#eef1ea, #141916)"
  route: "light-dark(#18201b, #f4efdf)"
  lamp-off: "light-dark(#a3aca4, #3f4943)"
  lamp-green: "light-dark(#1d7a39, #5bd57d)"
  lamp-amber: "light-dark(#9c6c00, #f4c443)"
  lamp-red: "light-dark(#bf3127, #ff5f53)"
  lit-green: "light-dark(#2fae55, #5bd57d)"
  lit-amber: "light-dark(#d8b000, #f4c443)"
  lit-red: "light-dark(#e0392e, #ff5f53)"
  sev-p0: "light-dark(#a1145d, #ff52a8)"
  sev-p1: "light-dark(#783ebd, #c18dff)"
  sev-p2: "light-dark(#335dbd, #88a9ff)"
  sev-p3: "light-dark(#56635b, #a7b2ab)"
  warn-ink: "light-dark(#7a4f00, #f4c443)"
  warn-wash: "light-dark(#f6e7bf, #2b2412)"
  ph-1: "light-dark(#b9c1b8, #36403a)"
  ph-2: "light-dark(#8e988f, #59645d)"
  ph-3: "light-dark(#5f6a63, #86918a)"
  ph-4: "light-dark(#323b35, #b7c0ba)"
  stage-body: "light-dark(#f3f5f0, #161c19)"
  stage-line: "light-dark(#454f48, #4f5b54)"
  stage-major: "light-dark(#6f7a72, #36413b)"
  stage-edge: "light-dark(#9aa49b, #242c28)"
  stage-recess: "light-dark(#dce1d9, #0b0e0c)"
typography:
  display:
    fontFamily: "\"D-DIN Condensed\", \"Arial Narrow\", \"Roboto Condensed\", system-ui, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.08em"
  headline:
    fontFamily: "\"D-DIN Condensed\", \"Arial Narrow\", \"Roboto Condensed\", system-ui, sans-serif"
    fontSize: "1.424rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0.07em"
  title:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "1.125rem"
    fontWeight: 650
    lineHeight: 1.3
  body:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "\"D-DIN Condensed\", \"Arial Narrow\", \"Roboto Condensed\", system-ui, sans-serif"
    fontSize: "0.857rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0.07em"
  key-cap:
    fontFamily: "\"D-DIN Condensed\", \"Arial Narrow\", \"Roboto Condensed\", system-ui, sans-serif"
    fontSize: "0.929rem"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "0.05em"
  mono:
    fontFamily: "ui-monospace, \"SF Mono\", SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.786rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "\"tnum\""
rounded:
  plate: "3px"
  small: "4px"
  key: "6px"
  module: "12px"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "20px"
  s-6: "24px"
  s-8: "32px"
  gap: "12px"
components:
  button-key:
    backgroundColor: "{colors.key}"
    textColor: "{colors.ink}"
    typography: "{typography.key-cap}"
    rounded: "{rounded.key}"
    padding: "0 12px"
    height: "32px"
  button-key-hover:
    backgroundColor: "{colors.key-hover}"
  button-primary:
    backgroundColor: "{colors.route}"
    textColor: "{colors.desk}"
    typography: "{typography.key-cap}"
    rounded: "{rounded.key}"
    padding: "0 12px"
    height: "32px"
  push-bank:
    backgroundColor: "{colors.module-2}"
    rounded: "{rounded.key}"
    padding: "2px"
  push-button:
    textColor: "{colors.ink-2}"
    typography: "{typography.key-cap}"
    rounded: "{rounded.small}"
    padding: "0 10px 0 9px"
    height: "28px"
  push-button-pressed:
    backgroundColor: "{colors.key}"
    textColor: "{colors.ink}"
  plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.plate-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.plate}"
    padding: "2px 7px 1px"
  chip:
    backgroundColor: "{colors.key}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.small}"
    padding: "1px 7px 0 6px"
  describer:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.glass-ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.key}"
    padding: "3px 9px"
  model-plate-name:
    backgroundColor: "{colors.module-2}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    padding: "0 6px"
  model-plate-effort:
    textColor: "{colors.muted}"
    typography: "{typography.mono}"
    padding: "0 6px"
  input:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.key}"
    padding: "0 10px"
    height: "32px"
  module:
    backgroundColor: "{colors.module}"
    textColor: "{colors.ink}"
    rounded: "{rounded.module}"
  nav-link-current:
    backgroundColor: "{colors.key}"
    textColor: "{colors.ink}"
    typography: "{typography.key-cap}"
    rounded: "{rounded.small}"
    padding: "0 10px"
    height: "28px"
---

# Design System: forge-ui

## Overview

**Creative North Star: "The Signal Box"**

forge-ui is a relay interlocking route-setting desk, after the Italian ACEI panels, built for code. A run is an inlaid track diagram (the 3D plate) set into an enamelled grey-green desk. Tasks are track sections, and their lamps show clear, occupied or failed before anyone reads a word. Everything around the plate is desk furniture. Region headings are engraved plates, toggles are push buttons with their own lamp, and numbers sit in black-glass describer windows. The desk is dense and quiet. The only saturated colour on it comes from lamps, and each lamp means one thing.

Both themes are finished to the same standard. Every colour token is a `light-dark()` pair: pale enamel with black plates in light, near-black green enamel with bone plates in dark. The system setting chooses the theme, and `<html data-theme>` overrides it. The 3D plate reads its colours from these same tokens through a probe element, so the desk and the diagram never drift apart. Depth comes in three plies (panel, module, popover) and nothing in between. Motion is relay motion: chrome changes state in 80ms with no easing, and running lamps blink at 1Hz.

The world rejects the dark dashboard of cards, sidebars and one blue accent, and it rejects skeuomorphic hardware. There are no screws, levers, rails or brushed metal. The desk is suggested by its typography, its lamps and its plies, not by drawn hardware.

**Key Characteristics:**
- Enamelled grey-green desk in both themes, with three plies of depth only.
- Engraved plates in condensed DIN capitals on every region heading.
- Push-button banks whose lamp lights when a button is pressed in.
- Black-glass describer windows for counts, ids and times, set in tabular mono digits.
- Five lamps with one meaning each. A hollow ring marks the family's non-plain case.
- Review severity shown as shape on its own colour ramp, never as a status lamp.
- One name plate per model everywhere it appears.
- Relay motion: 80ms linear, a 1Hz blink, and every state kept under reduced motion.

## Colors

The palette is grey-green enamel and ink, with lamp colours reserved for state.

### Primary
- **Route White / Route Black** (route): the lamp for a set route and for selection. It marks the pressed push button's lamp, the current nav lamp, the focus outline (2px, 2px offset), the selected run's ring, the selected table row's left rule, column drop markers and the primary key's face. It is near-black in light and bone-white in dark, so it is the strongest contrast on either desk.

### Secondary
- **Clear Green** (lamp-green): merged, passed, accepted, no changes. On the Live control it shows that polling is on.
- **Occupied Amber** (lamp-amber): running and interrupted. It is also the stage accent (`--stage-accent`), the model's own time in the phase bar, the replay range, the text caret, selection highlight, search-match underline and the leaderboard's confidence band.
- **Failed Red** (lamp-red): failed, error, conflict, infra, timeout, blocked. It is also the notification count and bad notifications.
- **Unlit** (lamp-off): pending, none, unknown, invalidated, and the resting lamp on every push button.
- **Lit Green / Lit Amber / Lit Red** (lit-green, lit-amber, lit-red): the same lamps lit on the 3D plate (`--st-lit`). On the pale light-theme panel a lit lamp is saturated, not darkened for text. Amber starts yellower because blending toward the pale panel pulls it toward orange. In dark they equal the text lamps.

### Tertiary
- **Severity Ramp** (sev-p0 magenta, sev-p1 violet, sev-p2 blue, sev-p3 slate): review findings P0 to P3, in the DOM views (always paired with a shape) and on the 3D plate's roof chips and final-review lamps. The ramp sits outside the lamp hues on purpose.
- **Warning Wash / Warning Ink** (warn-wash, warn-ink): the error banner, the leaderboard caveat, and over-limit lanes in the execution chart.
- **Phase Greys** (ph-1 to ph-4): a grey ramp for the time an attempt spent in each phase. The model's own phase is lit amber.

### Neutral
- **Desk Enamel** (desk): the page ground, the desk the modules sit on, and the Files view's ground.
- **Stage Panel** (stage-bg): the panel the 3D plate is inlaid in, a shade off the desk.
- **Module Enamel** (module, module-2): module and popover faces. The second shade is for push-button bank wells, table headers, hover rows, attempt and review boxes, and the model plate's name.
- **Key Face** (key, key-hover): button and chip faces, and the face of a pressed push button.
- **Field** (field): inputs, selects and diff file bodies.
- **Wash** (wash): selected rows, the selected jump item and selected cloud chips.
- **Black Glass** (glass, glass-ink, glass-dim): describer windows, code blocks and the stage note. It is dark in both themes, and its ink is a warm bone.
- **Engraving** (plate, plate-ink): plate grounds and their letters. Black with pale letters in light, bone with black letters in dark.
- **Ink** (ink, ink-2, muted): text in three steps.
- **Rules** (rule, rule-2): hairlines between rows and sections (rule), and edges of keys, fields and banks (rule-2).
- **Stage Hairlines** (stage-body, stage-line, stage-major, stage-edge, stage-recess): the 3D plate's block bodies, hairline drawing, major lines, edges and recessed wells.

### Named Rules
**The One Lamp, One Meaning Rule.** There are five lamps: green for clear (merged or passed), amber for occupied (running), red for failed, unlit for pending, and route white for a set route or selection. Never use a lamp colour for decoration or for a second meaning.

**The Hollow Lamp Rule.** A lamp drawn as a ring with a transparent fill belongs to its family but is not the plain case. Accepted and no changes are hollow green, interrupted is hollow amber, and infra, timeout and blocked are hollow red.

**The Separate Ramp Rule.** Review severity has its own colour ramp and always comes with a shape. It never borrows a status lamp.

**The Single Source Rule.** Every colour the 3D plate draws (`--stage-*`, `--st`, `--st-lit`) is a token in style.css and is read through the stage probe. Do not hard-code colours in the 3D code.

## Typography

**Display Font:** D-DIN Condensed Bold (self-hosted woff2, OFL), falling back to Arial Narrow, Roboto Condensed and system-ui
**Body Font:** system-ui (with -apple-system, Segoe UI and sans-serif fallbacks)
**Label/Mono Font:** ui-monospace (SF Mono, Menlo, Consolas)

**Character:** Engraved condensed DIN capitals do the labelling, the way engraved plates label a desk. A neutral system sans carries run goals and prose, and tabular mono carries every number, id and path.

### Hierarchy
- **Display** (D-DIN 700, 1.15rem, 0.08em, uppercase): the `forge` brand plate only.
- **Headline** (D-DIN 700, plate role at 1.424rem): page-level plates such as MODELS.
- **Title** (system-ui 650, 1.125rem, 1.3): run goals, inspector heads, card titles. These are user content, so they are not engraved and they truncate with an ellipsis.
- **Body** (system-ui 400, 14px, 1.5): prose, fields and table cells (0.893rem). Lead copy is capped at 92 to 96ch.
- **Label** (D-DIN 700, 0.857rem, 0.07em, uppercase): plates, table headers, chips and the model plate's alias. Filter captions use 0.786rem.
- **Key cap** (D-DIN 700, 0.929rem, 0.05em, uppercase): buttons, push buttons, nav links and the Live control.
- **Mono** (0.786rem to 0.893rem, tabular-nums): describers, counts, ids, effort levels and diffs.

The type scale runs xs 0.786, sm 0.893, base 1, md 1.125, lg 1.266 and xl 1.424 rem, a step ratio of about 1.125 on a 14px root.

### Named Rules
**The Engraving Rule.** Every region heading is an engraved plate: D-DIN capitals on the plate ground at 3px radius. Do not set a region heading as plain bold sans.

**The Describer Rule.** Numbers that describe a run (counts, wall time, ids) go in tabular mono, and on a describer window when they sit in a header.

## Layout

The app fills the window and does not scroll as a page. A 52px desk header (brand plate, MAP / CLOUD / MODELS bank, search, panel bank, shell and theme banks, alarm bell, Live control) sits above a layout that comes in two shells over the same modules. Keys 1, 2 and 3 toggle the Runs, Inspector and Tasks modules.

- **Immersive** (default): the stage fills the layout edge to edge. Runs (300px), Inspector (380px) and Tasks (208px tall) float over it at a 12px gap on ply 1, and the run header floats top-left at up to 1040px.
- **Workbench**: the same modules dock into a grid with hairline borders and square corners. When the Runs module is closed it becomes a 48px lamp rail.
- Splitters (10px hit area with a 2px route-coloured line on hover or focus) resize every module, keyboard included.
- At 1280px and below, the panel bank keeps its icons and drops its labels. At 760px and below everything goes into one scrolling column: modules stack with full borders and no shadow, the stage is clamp(240px, 72vw, 62vh) tall, the map filters fold behind a key, and splitters are hidden. The cloud panels rearrange at 900px.
- The spacing rhythm is a 4px base (4, 8, 12, 16, 20, 24, 32). Module heads are 40px tall, keys are 32px, and keys inside banks and modules are 28px with 40px hit areas.

## Elevation & Depth

Depth is a matter of rank, not decoration. There are three plies: the desk (and its inlaid stage), a module on the desk, and a popover over both. Shadows are soft and ambient, tinted toward green-black in light and pure black in dark. Inside a ply, sections are separated by hairline rules and tonal shifts (module and module-2), never by further shadows. Docked Workbench and mobile modules have no shadow.

### Shadow Vocabulary
- **Ply 1** (`0 1px 0 light-dark(rgb(17 26 20 / .06), rgb(0 0 0 / .4)), 0 12px 30px -14px var(--shadow-c)`): floating modules in Immersive and the cloud's floating panels.
- **Ply 2** (`0 2px 6px light-dark(rgb(17 26 20 / .1), rgb(0 0 0 / .45)), 0 24px 54px -18px var(--shadow-c)`): popovers, menus, dialogs, toasts and the stage tooltip.
- **Key bevel** (`inset 0 -1px 0 light-dark(rgb(0 0 0 / .08), rgb(0 0 0 / .35))`): the lower lip of a key cap. This is the only bevel in the system.

### Named Rules
**The Three Plies Rule.** Use the panel, a module or a popover, and nothing else. A box inside a module is a tonal well with a hairline, not a raised card.

## Shapes

Corners are gentle and nest by rank: 12px for modules, popovers and dialogs, 6px for keys, fields, describers and inner boxes, 4px for chips, push buttons inside a bank and the model plate, and 3px for engraved plates. Lamps are 7 to 9px circles with a 1.5px inset edge ring. The severity shapes are clip paths: a triangle spike for P0, a diamond for P1, a downward cone for P2 and a round pin for P3. Inline findings use a rotated square that is filled when the finding is confirmed and hollow when it is only plausible. Disclosures use a drawn 1.5px chevron, never the browser's triangle. Icons are a set authored for forge-ui on a 24-unit grid with a 1.5 stroke, round joins and currentColor, built with createElementNS.

## Components

### Buttons (key caps)
Key caps are flat and firm, and they press in.
- **Shape:** gently rounded (6px), 32px tall, 1px rule-2 edge, key bevel on the bottom lip.
- **Default:** key face, ink text, D-DIN key-cap type in capitals, 16px icons at stroke 2.
- **Hover / Focus / Active:** key-hover face and an ink-2 edge. Focus is the 2px route outline. Active scales to 0.96. Every transition is relay timing (80ms linear).
- **Primary:** a route-coloured face with desk-coloured text, used for the one committing action in a dialog.
- **Linkish:** an underlined text button with no face, for inline actions.

### Push-button banks
This is the signature control: a bank of push buttons where each button's lamp lights when it is pressed.
- **Bank:** a module-2 well with a rule-2 edge, 6px radius and 2px padding.
- **Button:** 28px tall, no face at rest, ink-2 text, and a 7px unlit lamp before the label.
- **Pressed (`aria-pressed=true`):** key face, ink text, a 1px inset rule-2 ring, and the lamp turns route-coloured.
- **Icon banks:** the same well without lamps, for shell and theme switches.
- The desk nav (MAP / CLOUD / MODELS) is the same bank built with links: `aria-current=page` lights the lamp.

### Engraved plates
D-DIN capitals on the plate ground (0.857rem, 0.07em, 3px radius) for module titles, inspector sections, legends, jump groups and tooltip meta. The brand plate is the larger display version.

### Status chips and lamps
- **Chip:** key face, rule edge, 4px radius, D-DIN label type, and a 7px lamp from the state's `--st-fill` and `--st-ring`.
- **Lamp:** a 9px circle. Running lamps blink at 1Hz with a stepped animation (`steps(1, end)`, dropping to 0.35 opacity at the half cycle). A selected run's lamp gets a route-coloured outer ring.
- **Severity chip:** mono label after a 9px clip-path shape in the severity colour.

### Describer windows
Black glass (6px radius, 3px 9px padding) with glass-ink tabular mono at 0.786rem and dim secondary text. Counts inside carry a 7px lamp. The window stays dark in both themes (`color-scheme: dark`).

### Model name plate
The only way a model is shown anywhere. It is a bordered 4px pill in two parts: the alias engraved in D-DIN capitals on module-2, then the effort in muted mono after a hairline. The colon is kept as visually hidden text so copying and screen readers still get it.

### Modules
A module face with a hairline rule edge and 12px radius. The 40px head holds a plate, a mono count and right-aligned tools at 28px. Modules float on ply 1 in Immersive and dock square in Workbench.

### Inputs / Fields
- **Style:** field ground, rule-2 edge, 6px radius, 32px tall. The select draws its own chevron from two gradients.
- **Focus:** the global 2px route outline. Hover shifts the edge to ink-2. The caret is amber, and checkboxes take the route colour.

### Tables
Sticky module-2 headers in D-DIN label type with sortable, draggable and hideable columns. Cells use body-sm type with tabular figures. Hover rows go to module-2, and a selected row gets the wash colour plus a 3px route rule on its leading edge. Scroll edges fade in a 32px module-coloured gradient.

### Motion
Chrome uses relay timing: 80ms linear on colour, border, opacity and scale. Disclosure chevrons, scroll-edge fades and the finding flash use 150ms with `cubic-bezier(.2, 0, 0, 1)`. Under reduced motion, animations and transition durations are cut to zero, and every lit, pressed and selected state stays.

### Planned, not yet built
The route-setting signature interaction (lineage focus, with lamps sweeping along the track and Esc releasing the route), finding beacons on the plate, the plate's legend and view controls, layer toggles, readable sprite labels and event animations are scheduled for the next PR. They are not part of the shipped system and have no tokens yet.

## Do's and Don'ts

### Do:
- **Do** take every colour from a `light-dark()` token on `:root`, so both themes stay equally finished and the 3D plate can read it through the probe.
- **Do** put every region heading on an engraved plate (D-DIN 700, 0.857rem, 0.07em, uppercase, plate ground, 3px radius).
- **Do** make every toggle a push button in a bank whose lamp lights when it is pressed (`aria-pressed` or `aria-current`).
- **Do** use exactly one lamp meaning per state, and use the hollow ring for a family's non-plain case.
- **Do** show review severity as shape plus the severity ramp (P0 spike, P1 diamond, P2 cone, P3 pin).
- **Do** show every model through the name plate (alias engraved, effort in mono).
- **Do** use relay timing (80ms linear) for chrome state changes, and keep every state when motion is reduced.
- **Do** draw new icons in the authored set: 24-unit grid, 1.5 stroke, round joins, currentColor.

### Don't:
- **Don't** add a fourth ply. A box inside a module is a tonal well with a hairline, not a shadowed card.
- **Don't** use a lamp colour as decoration or as a brand accent. Amber means occupied, and its non-state uses are limited to the stage accent, the caret, selection and the measured band.
- **Don't** colour review severity with a status lamp.
- **Don't** draw hardware: no fake screws, levers, rails or brushed metal.
- **Don't** use inline styles or `innerHTML`. The page CSP refuses them.
- **Don't** use emoji or text glyphs as icons.
