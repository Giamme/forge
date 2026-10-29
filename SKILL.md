---
name: forge
description: Dispatch implementation and independent diff review to user-selected models through local agent CLIs. Use for /forge or explicit requests to delegate coding or review; explanation-only questions do not authorize dispatch.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <alias>] [--planner <alias>] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high] [--fractal|--no-fractal|--fractal-auto-decompose] [--fractal-planner <spec>] [--jev|--no-jev] [--jev-shadow] [--gate NAME=MODE] [--no-gates] [--decide-budget tokens=N[,usd=X]] [--no-memory] [--no-ripwire] [--ui|--no-ui] [--timeout <seconds>] [--retry-failed <n>] [--escalate-after <n>] [--qa-threshold P0|P1|P2|P3|none|default] [--file-issues ask|auto|off] [--shadow-qa <spec>[,<spec>]] [--shadow-rate <0..1>] [--reviewer-session] [--explore <0..0.5>] [--fail-first]'
allowed-tools: [Bash, Read]
---

# Forge

Run an implementer (**dwarf**) followed by an independent **QA** review of its actual
changes. Use the scripts for dispatch and capture; do not reconstruct harness commands.
For explanation requests, explain the workflow without spending model quota.

## Choices and boundaries

- A dwarf model must be chosen by the user. Ask when it is missing; never invent one.
- QA defaults to `opus`; effort defaults to `medium` for dwarf, `high` for QA and `xhigh` for the planner.
- Specs are `alias[:effort[:harness]]`, e.g. `sol:xhigh:openclaude`. Resolve using
  [registry.tsv](registry.tsv); report any effort clamp. Literal model IDs require a harness.
- Bypass flags apply only when explicitly requested for that role (`--yolo-dwarf`,
  `--yolo-qa`). Bash access can mutate files despite disabled editing tools.
- Ask **“Use Fractal for this run? [y/N]”** once before an interactive run, unless
  the user supplied `--fractal`, `--no-fractal`, or `--fractal-auto-decompose`. Pass the answer explicitly to
  the runner. Unattended runs default off. Installation never activates Fractal.
  Resume and explicit retry retain the recorded choice. Read [Fractal](references/fractal.md)
  when selected; Fractal completion never substitutes for Forge QA acceptance.
- Ask **“Open forge-ui to watch this run? [y/N]”** once at the start of every new
  interactive run, unless the user supplied `--ui` or `--no-ui`. Pass the answer to `plan`
  or `forge-solo.sh` and relay the `forge-ui:` URL they print. Never ask on resume or retry;
  unattended runs leave it off. A UI failure never changes the run. Read [forge-ui](references/ui.md).
- A finished solo run, `plan` or `run` may end with one stderr line, `forge X is available
  (you have Y): <command>` (checked at most once a day; the command is `forge update`, or npm's
  for an npm installation). Relay it once; run the update only when the user asks (that
  command, then restart the harness session). `FORGE_UPDATE_CHECK=off` silences it.
- `--fractal-auto-decompose` enables automatic split-or-atomic decisions before each
  eligible node implements. Include the effective planner and bounds in the approval
  preview; child decisions need no additional user approval inside those bounds.
  Forward `--fractal-planner <spec>` to the runner. When omitted, forward a selected
  top-level `--planner` as `--fractal-planner`; otherwise each task's initial root model
  plans all its descendants. Ordinary `--fractal` keeps worker-requested decomposition.
- One implementer per tree. Solo edits remain uncommitted. Decomposed runs may commit
  and merge on Forge task and integration branches. Updating the user's branch requires
  separate authorization and `integrate --approved`. Never push automatically.
- The QA threshold defaults to `P2`: a CONFIRMED finding at P2 or worse blocks, so a PASS that
  lists one becomes FAIL, and milder CONFIRMED findings are recorded as known issues. `--qa-threshold`
  changes it (see [the verdict rule](docs/reviews.md#verdicts-and-failure)).
- Show the decomposed task/model table and wait for approval before dispatch. Retry only
  when requested; preserve failed task branches and worktrees. `run --retry-failed N`,
  `--infra-retries M`, `plan --shadow-qa` (a second review of a
  sampled share of attempts, never read, to measure reviewer agreement) and
  `plan --reviewer-session` (continue the reviewer's session across rounds) and `run --sweep Pn`
  (once every task merged, fix the tolerated findings at Pn or worse in a few extra tasks, one per
  file; `--sweep-ahead` instead hands a task the open findings in its files) are opt-in: offer
  them, never add them unprompted. So is `--fail-first` (solo or `plan`): the change's new
  tests also run without its non-test changes, and the reviewer is asked to report any that
  still pass as P2; it needs `.forge/verify-subset` and never changes a result by itself. `plan` explores by default (`--explore 0.2`: now and then
  a row left to forge gets its neighbouring tier's model from the user's pools, odds
  recorded for `forge dataset routing`); say so with the table and pass `--explore 0` when
  the user declines. Retries follow a policy: after three failed attempts on one model the
  next goes a tier up in the user's pools, or the task stops with a split suggestion
  (`--escalate-after N`, `0` = off); an explicit `retry --dwarf` overrides it. `accept`
  overrides a reviewer, so run it (with the user's `--reason` and
  `--approved`) only on the user's explicit instruction; never self-accept.
- When you report a run, read `<plan>/control.log` and mention any human change made mid-run
  (a threshold, a budget, a model pin, a drain, stop or queued retry; `forge control show <plan>`
  lists them). Never set a control, pin, `drain|stop|retry` or delete a run with `forge control` /
  `forge runs rm` unless the user asks. Exit `130` / `INTERRUPTED`: the run was signalled or
  stopped; relay the `retry` lines it printed.
- Exit `8` / status `INFRA` is a quota, auth or network stop, not a task failure and no
  attempt was spent: report it, say what to fix, and re-run only when told. For a multi-task
  plan, offer the opt-in whole-run review (`review`, or `integrate --final-review <qa-spec>`);
  it spends QA quota, so never run it unprompted.
- **Leftover findings become issues.** A run can finish with review findings nobody will act on:
  - CONFIRMED findings the threshold tolerated, from a PASS or a FAIL (`tasks/<id>/known_issues.md`);
  - PLAUSIBLE findings in a passed task's `qa.last`;
  - whatever a whole-run review left open.

  When there are any, act on the user's `--file-issues` choice with one filer:
  - **gh-proman**, only when it is installed and set up for the repository: `gh-proman -C <repo> config show` exits 0. It also puts the issues on the team's board with a priority. Run `gh-proman findings --from <plan or run dir> --include-plausible --json`, then follow the gh-proman skill's review-findings steps.
  - **forge's own filer** otherwise (gh-proman missing, or not configured for this repo; never configure it yourself): `python3 <skill_dir>/scripts/forge-issues.py <plan or run dir> --include-plausible --json`, plain `gh issue create`. Pass `--lang` matching the repo's existing issues, and `--label bug` when the repo has that label. If `gh` is missing, unauthenticated or the repo has no GitHub remote, list the findings in the report and say why none were filed.

  The choice:
  - `ask` (the default): show the dry-run summary (count by severity, titles, anything already filed) and ask once: **"File these N findings as GitHub issues? [all / CONFIRMED only / none]"**. Then re-run with `--apply`, adding `--include-plausible` only for "all".
  - `auto`: file them without asking (`--apply`, PLAUSIBLE included as "to verify" issues) and report what was created and skipped.
  - `off`: never offer.

  `--file-issues` is an instruction to you: never forward it to a runner. Skip INFRA, interrupted and still-failing runs. Both filers mark each issue with the same finding fingerprint and skip a finding either one already filed, so offering again after a retry only brings up new ones. A draft flagged as a possible duplicate is skipped on `--apply`: show it, and file it (`--confirm-new`) only if the user says it is different.
- Decision models (Jev, Clef) are optional, off until a gate is opted in with `forge decide`,
  fail-open, and never change a task's tier, select a model, or substitute for QA acceptance
  or repository verification (`--jev-act` is still accepted and changes nothing). Read
  [Decision models](references/decision-models.md) when selected.

## Common workflow

1. Inspect the repository and existing work. State the goal, selected models and any bypass
   flags. Keep complete requirements in `prompt.md`; short titles are insufficient for QA.
2. Preflight every selected role before the first model call, including an optional planner:
   `bash <skill_dir>/scripts/forge-dispatch.sh doctor --spec <spec> --role <role>`.
   This checks resolution, executable and advertised flags without model quota. Authentication,
   quota and live model availability remain unverified; a live probe must be explicitly requested.
3. Use [solo runs](references/solo.md) for one implementation. For `--decompose-level
   low|medium|high`, read [decomposition](references/decompose.md), prepare the task table,
   obtain approval, then use `forge-parallel.sh plan/run`. Write the user's request verbatim
   to `request.md` in the run or plan dir, and pass `--decompose-level` to `plan` with the
   level you actually split at, even when the user named none. Keep each task small and end
   its `prompt.md` with an `## Acceptance criteria` section of checks the reviewer can verify;
   `plan` names tasks over 7K characters or 9 files, where first attempts mostly fail review.
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
- [forge-ui](references/ui.md): the opt-in local run viewer; `forge ui open|status|stop`,
  `FORGE_UI=off`, one shared server, privacy.
- [Dataset](references/dataset.md): every run's inputs and outcomes, kept outside the repo
  for training decision models; `forge dataset status|import|export|outcomes`, `FORGE_DATASET=off`.
- [Decision models](references/decision-models.md): optional TypeSafe Jev or Cloudflare Clef
  judgments (drift, forward dependency, prompt adequacy, memory curation, verify discovery);
  off until a gate is opted in with `forge decide`; the criteria a site must meet, and what
  was tried and removed.

The common invocation is `/forge "<goal>" --dwarf <spec> [--qa <spec>]`. Advanced flags and
commands live with their mode reference. Use [offline tests and evaluation cases](tests/README.md)
when maintaining this skill.
