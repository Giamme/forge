---
name: forge
description: Dispatch implementation and independent diff review to user-selected models through local agent CLIs. Use for /forge or explicit requests to delegate coding or review; explanation-only questions do not authorize dispatch.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <alias>] [--planner <alias>] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high] [--fractal|--no-fractal|--fractal-auto-decompose] [--fractal-planner <spec>] [--jev|--no-jev] [--jev-act] [--jev-shadow] [--no-memory] [--no-ripwire] [--timeout <seconds>] [--retry-failed <n>] [--qa-threshold P0|P1|P2|P3]'
allowed-tools: [Bash, Read]
---

# Forge

Run an implementer (**dwarf**) followed by an independent **QA** review of its actual
changes. Use the scripts for dispatch and capture; do not reconstruct harness commands.
For explanation requests, explain the workflow without spending model quota.

## Choices and boundaries

- A dwarf model must be chosen by the user. Ask when it is missing; never invent one.
- QA defaults to `opus`; effort defaults to `medium` for dwarf and `xhigh` for QA/planner.
- Specs are `alias[:effort[:harness]]`, e.g. `sol:xhigh:openclaude`. Resolve using
  [registry.tsv](registry.tsv); report any effort clamp. Literal model IDs require a harness.
- Bypass flags apply only when explicitly requested for that role (`--yolo-dwarf`,
  `--yolo-qa`). Bash access can mutate files despite disabled editing tools.
- Ask **“Use Fractal for this run? [y/N]”** once before an interactive run, unless
  the user supplied `--fractal`, `--no-fractal`, or `--fractal-auto-decompose`. Pass the answer explicitly to
  the runner. Unattended runs default off. Installation never activates Fractal.
  Resume and explicit retry retain the recorded choice. Read [Fractal](references/fractal.md)
  when selected; Fractal completion never substitutes for Forge QA acceptance.
- `--fractal-auto-decompose` enables automatic split-or-atomic decisions before each
  eligible node implements. Include the effective planner and bounds in the approval
  preview; child decisions need no additional user approval inside those bounds.
  Forward `--fractal-planner <spec>` to the runner. When omitted, forward a selected
  top-level `--planner` as `--fractal-planner`; otherwise each task's initial root model
  plans all its descendants. Ordinary `--fractal` keeps worker-requested decomposition.
- One implementer per tree. Solo edits remain uncommitted. Decomposed runs may commit
  and merge on Forge task and integration branches. Updating the user's branch requires
  separate authorization and `integrate --approved`. Never push automatically.
- Show the decomposed task/model table and wait for approval before dispatch. Retry only
  when requested; preserve failed task branches and worktrees. `run --retry-failed N`,
  `--infra-retries M` and `--qa-threshold Pn` are opt-in: offer them, never add them
  unprompted. `accept` overrides a reviewer, so run it (with the user's `--reason` and
  `--approved`) only on the user's explicit instruction; never self-accept.
- Exit `8` / status `INFRA` is a quota, auth or network stop, not a task failure and no
  attempt was spent: report it, say what to fix, and re-run only when told. For a multi-task
  plan, offer the opt-in whole-run review (`review`, or `integrate --final-review <qa-spec>`);
  it spends QA quota, so never run it unprompted.
- Jev is optional, off unless configured and enabled, and advisory unless `--jev-act`.
  It never selects a model the user did not authorize and never substitutes for QA
  acceptance or repository verification. Read [Jev](references/jev.md) when selected.

## Common workflow

1. Inspect the repository and existing work. State the goal, selected models and any bypass
   flags. Keep complete requirements in `prompt.md`; short titles are insufficient for QA.
2. Preflight every selected role before the first model call, including an optional planner:
   `bash <skill_dir>/scripts/forge-dispatch.sh doctor --spec <spec> --role <role>`.
   This checks resolution, executable and advertised flags without model quota. Authentication,
   quota and live model availability remain unverified; a live probe must be explicitly requested.
3. Use [solo runs](references/solo.md) for one implementation. For `--decompose-level
   low|medium|high`, read [decomposition](references/decompose.md), prepare the task table,
   obtain approval, then use `forge-parallel.sh plan/run`.
   Pass the recorded Fractal choice and requested limits to the execution runner; tier
   pools go to solo directly or decomposed `plan`. Fractal does not replace planner dispatch.
   `--timeout` applies to decomposed runs too (kept in the plan).
4. Runners default to summary output. Read each `dwarf.last` and `qa.last` once; retrieve raw
   logs only when needed. Report findings faithfully, and identify verification
   limits. Source or review mutations invalidate acceptance. Missing verdicts are UNKNOWN.
   An absent repository verification command means UNVERIFIED, even when QA passes.

For a Fractal run, report its managed run ID with the Forge result. Use
`<skill_dir>/forge fractal runs --repo <repo> --json` and `tree RUN_ID --json` to discover
run/task/node IDs; use `status`, `logs` or `open` for inspection. Run-level `resume`
recovers the recorded pipeline; scoped controls affect only the selected execution scope.
Preserve failed work and state the distinction between execution completion, QA acceptance
and verification. Read the Fractal reference before retrying, changing limits or exporting
a portable report.

Ripwire context is automatic when compatible; `--no-ripwire` or `FORGE_RIPWIRE=off`
disables it. Pass opt-out to every role, including a separately dispatched planner.
Runners offer installation once before agents; dispatch never prompts. See
[Ripwire](references/ripwire.md) for installation, limits and fallback behavior.

## Conditional references

- [Planning](references/planning.md): read for `--planner` or an agreed approach.
- [Solo](references/solo.md): command, snapshots, artifacts, exit codes and native-review limits.
- [Decomposition](references/decompose.md): task schema, routing, waves, retries and integration.
- [Harnesses](references/harnesses.md): invocation troubleshooting or adding a harness.
- [Fractal](references/fractal.md): opt-in nested execution, installation, limits,
  recovery, managed run commands, read-only dashboard and portable reports.
- [Memory](references/memory.md): `.forge/` learning and spend records; `--no-memory` disables
  them. Memory is written to the user's repository but excluded from implementation diffs.
- [Jev](references/jev.md): optional TypeSafe System One judgments for routing, test
  selection and memory curation; off by default.

The common invocation is `/forge "<goal>" --dwarf <spec> [--qa <spec>]`. Advanced flags and
commands live with their mode reference. Use [offline tests and evaluation cases](tests/README.md)
when maintaining this skill.
