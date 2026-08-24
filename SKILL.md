---
name: forge
description: Dispatch a coding task to a "dwarf" model to implement it, then an independent "qa" model to review the dwarf's actual diff — routing either role to any model, at any reasoning effort, through any local agent CLI (codex, claude, openclaude, opencode, antigravity). Invoked as /forge "<goal>" --dwarf <alias>[:effort[:harness]] --qa <alias>[:effort[:harness]] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high]. With --decompose-level it splits the goal into tasks, runs several dwarves in parallel in isolated git worktrees routed by task difficulty (--dwarf-high/--dwarf-medium/--dwarf-low), and reviews each task separately. With --planner it dispatches the planning stage to a named model instead of planning itself, and it keeps a small project memory in .forge/ of what its runs learned about the repo. Use this whenever the user runs /forge, or asks to hand a coding task to another model / another CLI to build while a second model reviews it — phrasings like "have sol implement this", "dispatch this to codex", "run it through openclaude", "send it to gemini/antigravity", "get a second model to review the diff", "split this across several models", "run these in parallel", or any mention of dwarf/qa roles.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <alias>] [--planner <alias>] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high] [--no-memory] [--timeout <seconds>]'
allowed-tools: [Bash, Read]
---

# /forge

Forge splits a coding task into two roles run back to back:

- **dwarf** — implements the change in the working tree.
- **qa** — independently reviews *the diff the dwarf actually produced*, not the dwarf's
  own account of what it did. A model grading its own homework is not a check; the whole
  value of the second stage is that it sees the code rather than the claim.

You stay in the loop the entire time: you parse the command, dispatch each stage, read
the results back, and report to the user in plain language. Forge never commits, pushes,
resets, or otherwise finalizes anything — it edits the working tree and reports, leaving
every irreversible decision to the user.

## Running from any harness

Everything mechanical lives in `scripts/forge-dispatch.sh`, which needs only bash. That is
deliberate: forge is invoked from Claude Code, Codex, OpenClaude and opencode alike, and
those harnesses share no tool APIs. Anything expressed as a harness-specific tool call
would work in one and silently break in the other three, so drive every dispatch through
the script and use only shell plus file reads yourself.

The script also absorbs the parts that are easy to get wrong and hard to notice: codex
accepts an invalid reasoning effort without complaint and quietly runs at some other
level; several CLIs have variadic flags that swallow the prompt that follows them; codex
blocks forever on stdin unless it is redirected. Do not hand-assemble these commands.

## Command syntax

```
/forge "<goal>" --dwarf <alias>[:<effort>[:<harness>]]
                [--qa <alias>[:<effort>[:<harness>]]]
                [--planner <alias>[:<effort>[:<harness>]]]
                [--yolo-dwarf] [--yolo-qa] [--repo <dir>] [--native-review]
                [--no-memory] [--timeout <seconds>]
                [--decompose-level low|medium|high] [--max-parallel <n>]
                [--dwarf-high <spec>] [--dwarf-medium <spec>] [--dwarf-low <spec>]
                [--qa-high <spec>] [--qa-medium <spec>] [--qa-low <spec>]
```

The spec is up to three colon-separated fields, each optional after the first:

| Spec | Means |
|------|-------|
| `sol` | alias `sol`, default effort, its default harness (codex) |
| `sol:xhigh` | alias `sol` at `xhigh` effort on its default harness |
| `sol:xhigh:openclaude` | the same model and effort, run through the openclaude CLI |
| `opus::claude` | explicit harness, effort left to the role default — the middle field may be left empty |

Harnesses: `codex`, `claude`, `openclaude`, `opencode`, `antigravity` (the `agy` CLI,
which is how Gemini is reached). Aliases and their per-harness
model ids live in `registry.tsv`; run `bash scripts/forge-dispatch.sh doctor` or read that
file rather than guessing. An alias with no registry row is passed through as a literal
model id, but only when the harness is named explicitly — forge cannot guess a sensible
default harness for a model it has never seen.

Defaults when the user omits things:
- **`--dwarf` missing** — ask which dwarf to use. Do not guess: every dispatch spends real
  API quota on the user's account, and picking for them is not yours to do.
- **`--qa` missing** — default to `opus`, which runs locally through `claude` and adds no
  external cost surprise.
- **effort missing** — dwarf builds at `medium`, qa reviews at `xhigh`. A reviewer thinking
  less than the implementer tends to wave things through, which wastes the whole stage.
- an effort a pairing does not accept is clamped to the nearest lower one it does, and
  reported as `clamped=` in the script's output. Pass that on to the user; silently getting
  less thinking than they asked for is exactly the kind of thing they need to know.

Effort support is more irregular than a single ceiling per backend. Some models have gaps
rather than a ceiling (`gemini-pro` offers low and high but no medium, so `gemini-pro:medium`
clamps to low), and some take no effort setting at all (Claude models on antigravity), which
the script reports and then ignores. `registry.tsv` records which of these applies per
pairing, so trust its resolution over your own assumptions about a model.

## Yolo mode

`--yolo-dwarf` and `--yolo-qa` drop that role's sandbox and approval gates entirely:
`--dangerously-bypass-approvals-and-sandbox` on codex, `--dangerously-skip-permissions` on
claude, openclaude and antigravity. Each flag applies to one role only, so `--yolo-dwarf`
alone is a normal and sensible combination: the implementer gets a free hand, the reviewer
stays fenced in.

On codex, claude and openclaude the roles are already useful without it — the dwarf can
edit files and run tests, and qa can read and run commands but cannot edit what it is
reviewing. Reach for yolo when a role genuinely needs to escape the sandbox (installing
dependencies, writing outside the repo, touching the network), not as a default. When you
do use it, say so in your plan sentence and name the directory, because an unsandboxed
agent's blast radius is the user's machine.

**Antigravity is the exception worth knowing.** Without yolo it cannot run *any* shell
command, and a single denied command aborts the whole run rather than degrading — so a
dwarf that tries to run the test suite ends with an error and an empty diff. The script
detects this and appends a note telling that role not to attempt commands, which is why an
antigravity dwarf reports its work as unverified by execution. If you want a Gemini dwarf
that can actually run the tests it writes, it needs `--yolo-dwarf`; say so when you propose
the plan rather than quietly accepting unverified work.

## Decomposed runs

With `--decompose-level`, forge splits the goal into tasks, runs several dwarves concurrently
in isolated git worktrees, reviews each task's own diff separately, and merges only what
passes. Without the flag nothing changes — one dwarf, one QA pass, exactly as above.

Read `references/decompose.md` before running one. The mechanics live in
`scripts/forge-parallel.sh`; the summary here is enough to drive it, not enough to debug it.

### Routing by difficulty

Per-task dwarf choice cannot be expressed when the command is typed, because the tasks do not
exist until the goal is decomposed. So each task is *rated* `low`/`medium`/`high` by what it
demands of a model, and the user maps model tiers to difficulty tiers once, up front:

```
--dwarf-high sol:xhigh --dwarf-medium luna:high --dwarf-low gemini:low:antigravity
--dwarf sonnet:medium        # fallback for any tier without its own rule
```

`--qa-high`/`--qa-medium`/`--qa-low`/`--qa` work identically. A tier flag accepts a comma-list
(`--dwarf-high sol,terra`) to pool models within that tier. A plain `--dwarf sol:xhigh` with no
tier flags sends every task to the same dwarf, which is the simple case and stays the default.

Difficulty is about judgment required, not diff size: a large mechanical rename is `low`, a
ten-line concurrency fix is `high`. Getting that backwards sends the cheap model at the subtle
problem, which is worse than not routing at all.

### The flow

1. **Decompose.** Write `tasks.tsv` and one `tasks/<id>/prompt.md` per task into a run
   directory. Schema and the difficulty criteria are in `references/decompose.md`. Set each
   task's `files` honestly — it is what decides which tasks may run concurrently.
2. **Plan.** `forge-parallel.sh plan <dir> --repo <repo> <routing flags>` resolves difficulty
   into concrete dwarf/QA specs, computes waves, and prints the task table with a dispatch
   count.
3. **Show the table and stop.** This is the gate, and it is the only point where per-task
   choice is possible. The user retiers a task, overrides one row's dwarf, or approves. Do not
   dispatch before they answer — `high` on a large goal is 20+ dispatches of their quota.
4. **Run.** `forge-parallel.sh run <dir> [--max-parallel N]` (default 3). Each wave runs
   concurrently; passing tasks merge onto the integration branch as their wave lands, so
   dependent tasks in later waves see their dependencies' code.
5. **Report.** Give the results table, then each failing task's QA findings. The integration
   branch holds the passing work; the user's branch and working tree are untouched. Merging it
   into their branch is `forge-parallel.sh integrate <dir> --approved`, and is never automatic.
6. **Retry, if the user asks.** `forge-parallel.sh retry <dir> <task-id> [--dwarf <spec>]`
   re-dispatches one failed task in the worktree it already has, with its reviewer's findings
   in the prompt, and merges it if it passes this time. `--dwarf` escalates to a stronger
   model, which is the usual reason a retry is worth spending on. Offer it; never run it
   unasked — that would be the automatic repair loop forge deliberately does not have.

A `run` that was interrupted can simply be run again: already-merged tasks are skipped
rather than re-dispatched, so nobody's quota is spent twice on work that already landed.

### What makes it actually parallel

Two dwarves editing one file produce a conflict no reviewer can untangle, so the wave planner
puts tasks with overlapping `files` in *different* waves regardless of `--decompose-level`. A
decomposition whose tasks all touch the same file will run fully serial and say so in the
deferrals list. When that happens the fix is a better split, not more parallelism.

Each dispatch is a clean slate — no `--continue`/`--resume` anywhere — so one task's bad turn
cannot poison another. To stop clean slates making every dwarf blind to the run, a short
`capsule.md` (goal, this task, every task's status, ground rules) is prepended to each dwarf
and QA prompt. It is what keeps a dwarf from reimplementing an already-merged helper or
editing a file another dwarf currently owns.

## Planning

By default **you** plan: before dispatching, decide the approach and put it in the prompt,
so the dwarf builds the agreed thing rather than inventing a shape nobody has seen. In a
decomposed run that matters more, because the capsule prevents two dwarves from touching
one *file* but nothing stops them inventing incompatible *interfaces* at a seam they share.

`--planner <spec>` hands that stage to a named model instead — one dispatch for the whole
run, never one per task. A single planner is the point: one mind designs both sides of every
seam, where N independent planners would recreate the problem. Effort defaults to `xhigh`,
since a bad plan is executed at full price by every dwarf downstream of it.

```bash
bash <skill_dir>/scripts/forge-dispatch.sh planner <spec> \
  --repo "$REPO" --run-dir "$FORGE_RUN" --prompt-file "$FORGE_RUN/planner.prompt"
```

The planner reads the repo and writes nothing to it — it runs with qa's permission profile.
Ask it for the approach only: in a decomposed run, `tasks.tsv` rows plus a few lines of
approach per task; in a single-task run, just the approach. Write each task's approach to
`tasks/<id>/approach.md`, where the runner picks it up for both the dwarf and qa.

Show the approach before dispatching and let the user change it. That gate is the last
moment the plan is free; after it, changing the plan costs a whole run.

## Project memory

Forge keeps what its runs learned about a repo in `.forge/`, so the tenth run does not
rediscover what the first one learned:

- `.forge/memory.md` — small, capped, injected into every prompt.
- `.forge/ledger.tsv` — append-only, one row per dispatch, **never** injected.

That split is deliberate: the ledger remembers everything at no context cost, and only
memory.md is paid for on every future dispatch. Never inline the ledger into a prompt.

Everything is automatic and mechanical — `scripts/forge-memory.sh` does it:

```bash
bash <skill_dir>/scripts/forge-memory.sh inject "$REPO" dwarf   # prepend to the prompt
bash <skill_dir>/scripts/forge-memory.sh note dwarf             # append to the prompt
bash <skill_dir>/scripts/forge-memory.sh record "$REPO" --last "$FORGE_RUN/dwarf.last" \
     --role dwarf --model <spec>                                # after the dispatch
```

`note` asks the role to end with `FORGE_LEARNING: <verify|trap|finding> | <fact>`, and
`record` does the dedup, recurrence counting and pruning. A `verify` or `trap` is kept the
first time; a `finding` only after it has recurred in two separate runs, because one
occurrence is an incident and injecting it into every future prompt would be noise. Most
runs teach nothing durable and emit nothing — that is the normal outcome, not a failure.

`--no-memory` (or `FORGE_MEMORY=off`) disables all of it.

Because the ledger already holds a row per dispatch, `forge-memory.sh spend <repo>` reports
what forge has cost this repo in wall-clock and dispatch counts, grouped by model and role.
Time only, never tokens or money: the five CLIs expose usage differently or not at all, and
a number forge cannot actually measure would be worse than none. A decomposed run prints its
own line at the end.

Two things to tell the user rather than assume: forge **writes `.forge/` into their working
tree**, which is the only thing it ever writes outside its own branches, and it does not
commit it. Committing it is what makes the memory travel with the repo to their team.

When they ask what to do with it: commit `.forge/` to share, gitignore `.forge/` to keep it
local, or `--no-memory` to stop it existing. Gitignoring does not disable anything — the
file is still written and still injected, it just stops travelling. And never commit one of
the two files without the other: `memory.md` is rebuilt from `ledger.tsv`, so a clone with
the memory but not the ledger loses every shared fact the first time anyone runs forge
there, silently.

## Orchestration

1. **Check the tree.** Run `git status --porcelain` in the target repo. Pre-existing
   uncommitted work is not a blocker, but note it: the dwarf's changes will land on top of
   it and qa will see both mixed together, so the user needs to know whose code is whose.
2. **State the plan in one sentence** before dispatching — which dwarf at which effort on
   which harness, what qa will do, and whether either is yolo. These runs cost real quota
   and can take minutes; a user who wanted a different model should find out now.
3. **Run it.** `scripts/forge-solo.sh` dispatches the dwarf, captures the real diff and
   dispatches qa against it (see below). Long runs are normal — background it if your
   harness supports that, otherwise let it block.
4. **Report.** Summarize what the dwarf changed (files touched, one line on the substance),
   then pass qa's findings through faithfully. Ask whether to apply fixes, send it back for
   another pass, or stop. Do not auto-loop — one dwarf → qa pass is the run.

### Dispatching

`scripts/forge-solo.sh` owns the whole pipeline — memory injection, prompt assembly,
both dispatches, the diff capture and the ledger writes. Drive it rather than
reassembling those steps yourself: several of them fail silently rather than loudly
when they are slightly wrong, and the one that matters most is the diff capture. It
must exclude `.forge/` and it must diff new files against `/dev/null`, or the
reviewer either reads forge's own memory as the dwarf's work or never sees the
content of a file the dwarf created at all.

```bash
FORGE_RUN="$(mktemp -d "${TMPDIR:-/tmp}/forge-XXXXXX")"
echo "<the goal, one line>" > "$FORGE_RUN/goal.txt"

cat > "$FORGE_RUN/prompt.md" <<'EOF'
<goal, restated as a direct implementation instruction>
<the approach, if you or --planner produced one>
EOF

bash <skill_dir>/scripts/forge-solo.sh "$FORGE_RUN" --repo "$REPO" \
  --dwarf <spec> [--qa <spec>] [--yolo-dwarf] [--yolo-qa] [--native-review] [--timeout <s>]
```

The run directory must live outside the working tree — anything forge writes inside
the repo shows up in the dwarf's own diff. The script refuses a run dir inside the
repo rather than letting that happen quietly.

What it writes, and what you read afterwards:

| File | Is |
|---|---|
| `dwarf.last` / `qa.last` | each role's final message — the two things to report |
| `changes.diff` | exactly what qa reviewed |
| `verdict` | `PASS`, `FAIL`, `UNKNOWN`, or `NOCHANGES` |
| `<role>.log` | full transcript; read it when a dispatch fails |

Exit codes: `0` reviewed, `2` usage, `3` not a git repo, `4` a dispatch failed,
`5` **the dwarf produced no changes**, `7` a dispatch hit its timeout.

`5` is worth knowing on sight. Headless dwarves genuinely stop to ask a clarifying
question that nothing can answer; the run then ends having spent the quota and
changed nothing, and `dwarf.last` holds the question. Say that plainly rather than
reporting a failure of unclear cause.

For a large diff that would be unwieldy inline, `--native-review` switches codex to
its purpose-built reviewer. Know the trade: codex refuses a custom prompt alongside
its scope flags, so that reviewer never learns what the dwarf was *asked* to do and
cannot emit a verdict. It can still judge whether the code is correct, but not
whether it is the right change — mention that when you report its findings.

Every dispatch has a 45-minute timeout (`--timeout <seconds>`, `0` disables). It is
a backstop against a hung backend, not a budget; a role killed by it exits `7` and
has usually left a partial edit, so say so rather than reporting a clean failure.

## Reporting findings

Pass qa's findings through as qa wrote them, and keep its CONFIRMED/PLAUSIBLE labels
intact. Your job at this step is relay, not re-adjudication.

Two things are yours to add. If the dwarf's diff touches files clearly outside the stated
goal, flag it even when qa said nothing — a scope-creepy diff is a problem whether or not
it contains a bug. And if the dwarf's final message claims something the diff does not
support ("added tests" with no test file in the diff), say so; that gap is invisible to a
user reading only the summary.

## Safety

- Never `git commit`, `git push`, `git reset --hard`, or otherwise finalize on the user's
  behalf. The user decides what happens to the diff after they have seen it and read qa.
- Never add bypass flags on your own initiative. `--yolo-dwarf` / `--yolo-qa` are the only
  route to an unsandboxed run, and they exist so that choice is always the user's explicit,
  visible one.
- One dwarf per working tree, always. Two agents editing the same tree interleave their
  edits into a diff neither of them wrote and qa cannot untangle. Decomposed runs get their
  parallelism from a separate git worktree per task, which is why worktrees are mandatory
  there rather than an optimization.
- In a decomposed run, commits and merges are permitted **only** on the `forge/<run-id>/*`
  task branches and the `forge/<run-id>-integration` branch — branches forge created for
  itself. Never on the user's branch, and never a push. Merging the integration branch into
  their branch is an explicit `integrate --approved` step.
- Never delete a failed task's branch or worktree; they are the only record of what went
  wrong and the only thing to retry from.
- Never delete a failed task's branch or worktree. They are the only record of what went
  wrong, and they are what `forge-parallel.sh retry` acts on.
- `.forge/` is the one thing forge writes into the user's working tree rather than onto a
  branch of its own. Say so when it appears, do not commit it for them, and never let it
  into a captured diff — a reviewer handed forge's own memory will report it as the dwarf's
  unrelated change.

## References

- `registry.tsv` — alias → (harness, model, effort ceiling). Single source of truth; add a
  row here to teach forge a new model.
- `references/harnesses.md` — per-harness invocation details, effort ladders, and the
  known failure modes of each CLI. Read this when a dispatch fails or when adding support
  for a new backend.
- `references/decompose.md` — the `tasks.tsv` schema, difficulty criteria, wave algorithm,
  capsule format and cleanup rules. Read this before running with `--decompose-level`.
- `references/memory.md` — the `FORGE_LEARNING` grammar, promotion and pruning rules, and
  the ledger schema. Read this when memory looks wrong, empty or too large.
- `scripts/forge-solo.sh` — the whole single-task run: both dispatches, the diff capture
  and the ledger writes.
- `scripts/forge-memory.sh` — `inject` / `note` / `record` / `show` / `prune` / `spend`.
- `scripts/forge-parallel.sh` — `plan` / `run` / `retry` / `integrate` for decomposed runs.
- `scripts/forge-install.sh` — installs `/forge` into all five harnesses. Claude,
  OpenClaude, Codex and opencode reference this directory live, so edits take effect
  immediately; antigravity gets a copy and needs the installer re-run after any change.
