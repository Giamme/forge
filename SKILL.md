---
name: forge
description: Dispatch implementation and independent diff review to user-selected models through local agent CLIs, and drive forge runs that already exist. Use for /forge, for explicit requests to delegate coding or review, and for questions about a forge run or plan in this repository - inspecting, resuming, retrying, accepting or reporting on it, a dwarf or QA verdict, a task at NEEDS_INPUT, NEEDS_DECISION or INFRA (exit 8), forge handoff, forge-ui, or the .forge/ directory. Explanation-only questions do not authorize dispatch.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <spec>] [--decompose-level low|medium|high] [flags…]'
allowed-tools: [Read, "Bash(forge *)", "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(${CLAUDE_SKILL_DIR}/forge *)"]
---

# Forge

Run an implementer (**dwarf**) followed by an independent **QA** review of its actual
changes. Use the scripts for dispatch and capture; do not reconstruct harness commands.
`<skill_dir>` below is the directory that holds this file (`${CLAUDE_SKILL_DIR}` in Claude Code). You
orchestrate: write prompts and plan files, run forge's commands, and report; do not edit the
repository's source yourself.

## References

Read the one that applies before acting; every flag is in [commands](docs/commands.md).

- [Planning](references/planning.md): `--planner` (and `forge-parallel.sh auto`) or an agreed approach.
- [Solo](references/solo.md): command, snapshots, artifacts, exit codes and native-review limits.
- Decomposition: [planning a plan](references/decompose.md) (task schema, routing, profiles, waves, size,
  gates, plan shape), [reviews and verdicts](references/decompose-review.md),
  [retries, stops and recovery](references/decompose-recovery.md),
  [integration and verification](references/decompose-integration.md),
  [opt-in modes](references/decompose-opt-in.md) (groups, retro, brief, summaries).
- [Leftover findings](references/findings.md): filing them as issues, triage, `--file-issues`.
- [Harnesses](references/harnesses.md): invocation troubleshooting, the fast tier, adding a harness.
- [Memory](references/memory.md): `.forge/` learning and spend records (`--no-memory` disables them).
- [forge-ui](references/ui.md): the opt-in run viewer; `forge ui open|status|stop`, `FORGE_UI=off`, privacy.
- [Dataset](references/dataset.md): every run's inputs and outcomes, outside the repo; `FORGE_DATASET=off`.
- [Decision models](references/decision-models.md): optional Jev or Clef gates, off until opted in with `forge decide`,
  fail-open; they never change a tier, pick a model or stand in for QA or verification (`--jev-act` changes nothing).
- [Ripwire](references/ripwire.md): automatic context when compatible; `--no-ripwire` or `FORGE_RIPWIRE=off`
  disables it for every role, a dispatched planner included. Runners offer installation once.

## Reporting

Runners default to summary output. Read each `dwarf.last` and `qa.last` once; retrieve raw logs only
when needed. Report findings faithfully and name verification limits. Source or review mutations
invalidate acceptance. A missing verdict is UNKNOWN. No repository verification command means
UNVERIFIED, even when QA passes. Read `<plan>/control.log` and mention any human change made mid-run
(`forge control show <plan>` lists them). Keep execution completion, QA acceptance and verification
apart. Offer the leftover findings as issues ([findings](references/findings.md)).

## Choices and boundaries

- **The user picks the models.** A dwarf must be chosen by the user; never invent one. A selected
  `--profile NAME`, or a default they set (`forge config profile use`), is that choice. When neither a
  flag nor a profile names models, offer one profile and wait: their default if set, else `balanced`
  ([recommended routing](docs/models.md#recommended-routing)); ask role by role only if they decline.
  The runner applies a configured default only when no flag is passed and the run has no frozen copy,
  never `balanced` by itself: pass `--profile balanced` when accepted, `--no-profile` when declined.
  A profile with no dwarf pool still needs `--dwarf`. QA defaults to `opus` when no profile names a
  reviewer; effort defaults to `medium` (dwarf), `high` (QA), `xhigh` (planner).
- Specs are `alias[:effort[:harness]]`, e.g. `sol:xhigh:openclaude`, resolved with
  [registry.tsv](registry.tsv); report any effort clamp. Literal model IDs require a harness.
- **Opt-in means opt-in.** Anything that spends quota, changes how a run behaves or posts outside the
  machine needs the user's explicit word: offer it, never add it. That covers every flag below,
  `--yolo-dwarf` / `--yolo-qa` (only for the role they name; Bash can still mutate files with editing
  tools disabled) and `--fast` (offer only when the user asks for speed).
- **Setup.** If `forge setup show --json` says `done: false`, offer `forge setup` once before the first
  run. Its saved answers ([setup](references/setup.md)) are the user's word for what they cover.
- Ask **"Open forge-ui to watch this run? [y/N]"** once when a new interactive run starts,
  unless a flag or setup answered it, and pass the answer on ([forge-ui](references/ui.md)). Never ask on
  resume or retry; unattended runs leave it off. Relay the `forge-ui:` URL; a UI failure never changes a run.
- One implementer per tree. Solo edits stay uncommitted; decomposed runs commit only on forge's task and
  integration branches. Updating the user's branch needs separate authorization and `integrate
  --approved`. Never push.
- QA threshold: `P2` by default (a CONFIRMED P2 or worse blocks; milder ones become known issues);
  `--qa-threshold` changes it ([verdicts](docs/reviews.md#verdicts-and-failure)).
- Show the decomposed task/model table, with its `profile:` line, and wait for approval before dispatch.
  When it says the plan runs sequentially, offer `--collapse-chains` or one task (`auto` collapses
  unless `--no-collapse`). `plan` [explores](references/decompose.md#exploratory-routing-on-by-default) by default (`--explore 0.2`): say so with the table; pass
  `--explore 0` if the user declines. Retries escalate a tier after three failures on one model, or stop
  at `NEEDS_DECISION` (`--escalate-after N`, `0` off); an explicit `retry --dwarf` overrides it.
- Retry only when asked, and keep failed task branches and worktrees. `accept` overrides a reviewer: run
  it (`--reason`, `--approved`) only on the user's explicit word; never self-accept.
- Never set a control, pin, `drain|stop|retry`, delete or compact a run (`forge control`, `forge runs
  rm|compact`) unless the user asks; `forge du` only reads.
- Relay a `forge X is available (you have Y): <command>` line once; update only when asked (then restart
  the harness session).

| Opt-in flag | Where | What it does |
|---|---|---|
| `--retry-failed N`, `--infra-retries M` | `run` | retry failed or infrastructure-stopped tasks automatically |
| `--shadow-qa SPEC` | `plan` | a second, never-read review of sampled attempts, to measure agreement |
| `--reviewer-session`, `--implementer-session` | `plan` | continue the reviewer's / implementer's session across rounds |
| `--sweep Pn` / `--sweep-ahead` | `run` | fix tolerated findings at Pn or worse once all merged, one task per file / hand a task the open findings in its files |
| `--fail-first` | solo, `plan` | new tests also run without the non-test changes; ones that still pass are P2; needs `.forge/verify-subset` |
| `--pre-review-tests` | `plan` | run the affected tests before review; a red attempt goes back unreviewed |
| `--auto-split`; `--split-depth\|children\|nodes N` | `run`; `plan` | the planner splits a task stopped on `split`; split limits, shown with the table ([subtasks](references/decompose.md#subtasks-a-task-split-at-any-level)) |
| `--groups`, `--group-review` | `plan` | one review for several small, related, low-risk tasks; keep high-risk tasks per task ([groups](references/decompose-opt-in.md#groups-and-group-review-opt-in)) |
| `--fast ROLES` | any | faster service tier, billed higher ([fast tier](references/harnesses.md#fast-service-tier)): a run without it on the account stays standard and prints a note, not a failure; openclaude, opencode, antigravity and grok exit 3; solo takes only `dwarf`, `qa`, `none` |
| retro, brief, summaries | `plan` | [opt-in modes](references/decompose-opt-in.md); brief and summaries need a model the user names |

## Existing runs

- When a session starts on a repo with an active or unfinished run, or the user names one, run `forge
  handoff` first and work from its output. Never start `run` while its scheduler is live: continue with
  `forge handoff resume <plan> --harness <this harness>`, which follows a live run and restarts a stopped
  one with its recorded flags. Take over another driver only on
  the user's word (`--take-over`). Record what is not on disk with `forge handoff note`.
- Exit `130` / `INTERRUPTED`: signalled or stopped; `forge handoff resume <plan>` carries it on; relay the
  `retry` lines it printed.
- `waiting for your answer:` is `NEEDS_INPUT`: relay each question verbatim and pass the reply with `retry
  <plan> <id> --answer "<reply>"`. Never answer for the user; nothing else resumes it.
- A task at `NEEDS_DECISION` waits for the user: present `tasks/<id>/decision.md`'s reasons and choices.
  Run `retry --decide`, `retry --dwarf` or `accept` (`--reason`, `--approved`) only on the user's explicit
  word, never to keep a run going; on a live run use `forge control retry … --decide` or `forge control
  accept`. The split choice is `--decide split` with the subtasks; the task is then reviewed on their work
  ([when reviews don't converge](references/decompose-recovery.md#when-reviews-dont-converge)).
- A live `run` prints `forge: needs you: <task> <STATUS> (<why>) -> <file>` when a task stops for the user:
  relay it at once; the run goes on. Can't read background output live? Run `forge watch <plan>`.
- `waiting for their subtasks:` a subtask failed or waits: act in its `tasks/<id>/sub` plan, then `run`.
- A group at `NEEDS_DECISION`: present `forge group show <plan>`; run `forge group retry <plan> <gid>
  --tasks <ids>` only on the user's word. An oversized group is split in `groups.tsv` and planned again,
  not retried.
- Exit `8` / `INFRA` is an infrastructure stop (`capacity`, `rate_limit`, `usage_limit`, `budget`, `auth`,
  `network`, `provider_down`, `empty`, `cancelled`; old runs say `quota`), not a task failure: no attempt
  was spent. Report each "needs you" item with its hint and re-run only when told
  ([infrastructure failures](references/decompose-recovery.md#infrastructure-failures)).
- Offer a multi-task plan's whole-run review (`review`, or `integrate --final-review <qa-spec>`) only when
  asked: it spends QA quota.

## Common workflow

1. Inspect the repository and existing work, unless this is a decomposed `--planner` run (the planner
   reads the repository). State the goal, selected models and any bypass flags. Keep complete
   requirements in `prompt.md`; short titles are insufficient for QA.
2. Preflight every selected role, an optional planner included, before the first model call:
   `bash <skill_dir>/scripts/forge-dispatch.sh doctor --spec <spec> --role <role>`. It checks resolution,
   executable and flags without quota; authentication, quota and live availability stay unverified
   unless the user asks for a live probe.
3. One implementation: [solo](references/solo.md). `--decompose-level low|medium|high`: read
   [planning a plan](references/decompose.md). With `--planner`, call `forge-parallel.sh auto` once, as
   one blocking command (in the background where the harness limits foreground commands; wait for its
   notice, do not poll or read its logs), without `--approved` unless the user pre-approved; show the
   table and wait. Otherwise write the task table, get approval, then `forge-parallel.sh plan/run`.
   `auto` preflights the planner and re-dispatches once on a send-back. Write the request verbatim to
   `request.md`, pass `--decompose-level` with the level you split at (even when the user named none),
   end each `prompt.md` with `## Acceptance criteria` the reviewer can check, and keep tasks small
   (`plan` names any over 7K characters or 9 files). `--timeout` and tier pools go to the
   runner. If `plan` exits 4, read `<plan>/plan.sendback.md`, fix each listed prompt and cover
   each listed requirement (with `--planner`, re-dispatch with that file appended), then run `plan`
   once more; forge never sends the same thing back twice.
4. After the run (the `run` that `auto` printed, or `auto --approved`), read `<plan>/results.tsv` and each
   failed task's `qa.last`, and report as above.

The common invocation is `/forge "<goal>" --dwarf <spec> [--qa <spec>]`. Use
[offline tests and evaluation cases](tests/README.md) when maintaining this skill.
