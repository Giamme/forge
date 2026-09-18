# Optional Jev advisory judgments

Jev is TypeSafe's "System One" model: it answers narrow, typed questions (a
yes/no probability, a single choice, an ordered score) over supplied state in
about 100ms, with calibrated probabilities. Many questions per request evaluate
in parallel, so asking a few extra costs little additional latency. Forge uses
Jev to *advise*; Forge keeps every existing decision and gate. Jev never
replaces model routing logic, test selection, QA acceptance or repository
verification — it only offers a probability alongside them.

## Privacy

Off by default, and this is the main reason why. Each enabled capability
transmits a narrow slice of state to an external API:

- **routing** — task prompts and candidate model identifiers
- **tests** — diff hunks and test names
- **gates** — diff hunks and file excerpts relevant to the gate
- **memory** — memory entry text being considered for curation

`.forge/` and `.git` are excluded from every payload, in every capability, with
no exception. The API key lives at
`${XDG_CONFIG_HOME:-$HOME/.config}/forge/jev.json`, mode `0600`. It is never
written to a run directory, a prompt, or a log.

## Setup

```sh
./forge jev setup                        # store an API key
./forge jev status                       # resolved config, enabled capabilities
./forge jev doctor [--live]              # config/key checks; --live also probes the API
./forge jev enable [--capability NAME]   # persist enabled (optionally scoped)
./forge jev disable [--capability NAME]
```

`TYPESAFE_API_KEY` in the environment overrides the stored key for that
invocation only. Keys come from `console.typesafe.ai`.

## Gating and precedence

Four steps, implemented identically by `scripts/forge-jev-options.sh` (shell)
and `scripts/forge_jev/` (Python) — keep them in sync:

1. `FORGE_JEV=off` wins over everything: everything off, full stop. This is
   the hard kill switch.
2. Any `FORGE_JEV_<CAP>=on` acts as an allowlist — only those capabilities
   run. `--jev-tests` alone works without `--jev`.
3. Otherwise `FORGE_JEV=on` (from `--jev`) or the stored config's `enabled`
   flag turns it on, and a capability explicitly set `off` is subtracted.
4. Default: off.

Flags: `--jev` / `--no-jev` (mutually exclusive), `--jev-act`, `--jev-shadow`,
and `--jev-<cap>` / `--no-jev-<cap>` for `routing`, `tests`, `gates`, `memory`.
A run directory freezes its resolved selection in `jev-selection.json`, the
same way Fractal freezes `fractal-selection.json`, so resume and explicit
retry make the same decisions as the original run.

## Advisory by default

`--jev-act` is required before any judgment is auto-applied. Even then, Jev
may only choose among models the user already authorized for the run — it
never selects a model on its own, never overturns a `FORGE_VERDICT`, and never
marks anything verified. `--jev-shadow` records judgments and changes nothing
at all; use it to observe calibration before trusting `--jev-act`.

## Fail-open

Any error, timeout, or missing key is logged as a skip reason and the run
proceeds exactly as it would without Jev. A Jev network call can never fail a
run.

## Observability

- `<run-dir>/jev.jsonl` — one row per request: site, question ids, answers,
  confidences, latency, tokens, and whether the judgment was applied.
- `<run-dir>/jev.skip` — present when a request was skipped, with the reason.

## Status

Phase 0 adds the configuration surface only: entry points, gating flags,
config storage and this document. **No Forge decision consults Jev yet.**
Routing, test selection, pre-dispatch gates and memory curation are the
planned capabilities; none is wired to a live decision in this phase.
