---
name: forge
description: Dispatch implementation and independent diff review to user-selected models through local agent CLIs. Use for /forge or explicit requests to delegate coding or review; explanation-only questions do not authorize dispatch.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <alias>] [--planner <alias>] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high] [--no-memory] [--timeout <seconds>]'
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
- One implementer per tree. Solo edits remain uncommitted. Decomposed runs may commit
  and merge on Forge task and integration branches. Updating the user's branch requires
  separate authorization and `integrate --approved`. Never push automatically.
- Show the decomposed task/model table and wait for approval before dispatch. Retry only
  when requested; preserve failed task branches and worktrees.

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
4. Runners default to summary output. Read each `dwarf.last` and `qa.last` once; retrieve raw
   logs only when needed. Report findings faithfully, and identify verification
   limits. Source or review mutations invalidate acceptance. Missing verdicts are UNKNOWN.
   An absent repository verification command means UNVERIFIED, even when QA passes.

## Conditional references

- [Planning](references/planning.md): read for `--planner` or an agreed approach.
- [Solo](references/solo.md): command, snapshots, artifacts, exit codes and native-review limits.
- [Decomposition](references/decompose.md): task schema, routing, waves, retries and integration.
- [Harnesses](references/harnesses.md): invocation troubleshooting or adding a harness.
- [Memory](references/memory.md): `.forge/` learning and spend records; `--no-memory` disables
  them. Memory is written to the user's repository but excluded from implementation diffs.

The common invocation is `/forge "<goal>" --dwarf <spec> [--qa <spec>]`. Advanced flags and
commands live with their mode reference. Use [offline tests and evaluation cases](tests/README.md)
when maintaining this skill.
