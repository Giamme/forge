# Development

[← README](../README.md) · [All docs](README.md)

The repository layout, the offline checks, and how forge measures its own runtime cost.

## Repository layout

```
forge/
├── SKILL.md                     the skill itself — what the orchestrating model reads
├── registry.tsv                 alias → (harness, model, effort). Add a row to teach forge a model
├── README.md                    the short overview
├── docs/                        the full documentation, one page per topic
├── LICENSE                      MIT
├── package.json                 the npm package: its name, version and the files it ships
├── forge                        launcher, on PATH from npm: `forge install`, `fractal`, `jev`, `dataset` and `ui`
├── scripts/
│   ├── forge-dispatch.sh        resolve a spec → run one role on one harness
│   ├── forge-solo.sh            the whole single-task run: dwarf → diff → qa
│   ├── forge-parallel.sh        plan / run / retry / integrate for decomposed runs
│   ├── forge-parallel-options.sh flags shared by several forge-parallel.sh subcommands
│   ├── forge-lib-task.sh        attempt log, infra stops, QA-only resume, guards, severity gate, report blocks
│   ├── forge-lib-verify.sh      verify command lookup and the coverage report
│   ├── forge-lib-plans.sh       accept / split / combine
│   ├── forge-lib-review.sh      the opt-in whole-run review (review, integrate --final-review)
│   ├── forge-schedule.py        readiness scheduler: in-run retries, infrastructure pauses
│   ├── forge-contract.py        the shared QA/dwarf prompt contract and the severity gate
│   ├── forge-guard.py           implementer guards: session wrapper, orphan reaping, promise scan
│   ├── forge-verify-coverage.py suites the project defines that the verify command skips
│   ├── forge-memory.sh          inject / note / record / spend project memory
│   ├── forge-fractal.py         Fractal CLI and internal adapter entrypoint
│   ├── forge-fractal-options.sh runner selection, limit flags and help
│   ├── forge-fractal-dispatch.sh bridge entrypoint for existing pipelines
│   ├── forge-install-fractal.sh isolated optional runtime installer
│   ├── forge_fractal/           nested execution, recovery, inspection and dashboard
│   ├── forge-ui.py              `forge ui open|status|stop`: the one shared forge-ui server
│   ├── forge-ui-options.sh      `--ui|--no-ui` and the fail-open launch, sourced by the runners
│   ├── forge_registry.py        the run registry: where run dirs are noted, read by forge-ui
│   ├── forge-dataset.py         `forge dataset` entry point
│   ├── forge_dataset/           the run dataset: capture hooks, readers, importer, status checks
│   └── forge-install.sh         install into every harness that needs it (opencode and grok find it themselves)
├── ui/                          forge-ui: read-only local run viewer (`forge_ui` package, tests, API contract)
└── references/
    ├── ui.md                    forge-ui: commands, env vars, shared server, state, privacy
    ├── harnesses.md             per-harness invocation, ladders, failure modes
    ├── decompose.md             tasks.tsv schema, difficulty criteria, wave algorithm
    ├── fractal.md               opt-in backend, limits, recovery and observability
    ├── memory.md                FORGE_LEARNING grammar, promotion and pruning rules
    └── dataset.md               the run dataset: what is captured, where, and its invariants
```

## Offline checks

Run `bash tests/check.sh` for offline regression checks. See [tests](../tests/README.md) for skill
invocation scenarios and verification limits. There is currently no CI workflow.

## Runtime efficiency and measurement

Shell entrypoints require Python 3 (standard library only) in addition to Bash and Git.
The [runtime helper decision](../references/runtime.md) records the module boundaries and tradeoffs.
Solo and parallel runners default to `--output summary`; use `--output full` to include raw
backend output. Direct dispatch keeps its full-output default. Read each `dwarf.last` and
`qa.last` once, and retrieve `.log` or `.out` only when investigating a problem.

QA receives complete requirements, approach, ownership, retry findings and distinct memory
facts once, with review instructions separate from implementation instructions. Prompts and
full binary diffs remain on disk. Review repositories receive self-contained tree object packs,
create baseline/final commits, and check out the final tree once. Exact tree comparisons and
all source, review and integration fingerprint checks remain acceptance boundaries.

`FORGE_CAPABILITY_CACHE` optionally selects an offline help cache. Runners share a run-local
`capabilities/` cache across preflights and dispatches. Keys cover the executable's resolved
path, device/inode, size and modification/change timestamps, help command mode, registry
contents and dispatch recipe. Every selected spec, effort and permission recipe is still
validated; cached help never proves authentication or live model availability.

Run `bash scripts/forge-dispatch.sh report <run-dir>` for JSON attempt records, including
failures and timeouts, with project memory enabled or disabled. `attempts/<role>-<id>/`
retains prompts, final responses, raw logs, resolution, command and `metrics.json` per dispatch.
Pipeline records capture preparation, preflight, snapshot, verification and total time;
dispatch records capture preparation, preflight, model and total time. Nested pipeline
`dispatch` time includes dispatch overhead: do not add nested totals together. Timing uses
monotonic seconds. A still-running or forcibly killed record has a null exit code and may
have incomplete timings. A dispatch that stopped on an infrastructure failure records its class
(`quota`, `auth`, `rate_limit`, `network`, `empty`) as `infra_class`, and keeps the `.infra` and
`.orphans` files beside the other attempt artifacts.

Prompt bytes are separate from native input, cached-input and output token counts. Codex
uses `turn.completed` usage; Claude uses result usage (input includes uncached, cache-read
and cache-creation tokens). Unsupported or absent usage stays null, never estimated from
bytes. See [measurement and live evaluation](../tests/README.md).

Measured results, including the live comparison's higher total token use and latency, are
recorded in [the September 7 evaluation](../tests/benchmark-2026-09-07.md).
