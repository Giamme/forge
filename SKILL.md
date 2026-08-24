---
name: forge
description: Dispatch a coding task to a "dwarf" model to implement it, then an independent "qa" model to review the dwarf's actual diff — routing either role to any model, at any reasoning effort, through any local agent CLI (codex, claude, openclaude, opencode, antigravity). Invoked as /forge "<goal>" --dwarf <alias>[:effort[:harness]] --qa <alias>[:effort[:harness]] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high]. With --decompose-level it splits the goal into tasks, runs several dwarves in parallel in isolated git worktrees routed by task difficulty (--dwarf-high/--dwarf-medium/--dwarf-low), and reviews each task separately. With --planner it dispatches the planning stage to a named model instead of planning itself, and it keeps a small project memory in .forge/ of what its runs learned about the repo. Use this whenever the user runs /forge, or asks to hand a coding task to another model / another CLI to build while a second model reviews it — phrasings like "have sol implement this", "dispatch this to codex", "run it through openclaude", "send it to gemini/antigravity", "get a second model to review the diff", "split this across several models", "run these in parallel", or any mention of dwarf/qa roles.
argument-hint: '"<goal>" --dwarf <alias>[:effort[:harness]] [--qa <alias>] [--planner <alias>] [--yolo-dwarf] [--yolo-qa] [--decompose-level low|medium|high] [--no-memory]'
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
                [--no-memory]
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

Two things to tell the user rather than assume: forge **writes `.forge/` into their working
tree**, which is the only thing it ever writes outside its own branches, and it does not
commit it. Committing it is what makes the memory travel with the repo to their team.

## Orchestration

1. **Check the tree.** Run `git status --porcelain` in the target repo. Pre-existing
   uncommitted work is not a blocker, but note it: the dwarf's changes will land on top of
   it and qa will see both mixed together, so the user needs to know whose code is whose.
2. **State the plan in one sentence** before dispatching — which dwarf at which effort on
   which harness, what qa will do, and whether either is yolo. These runs cost real quota
   and can take minutes; a user who wanted a different model should find out now.
3. **Dispatch the dwarf** (see below). Long runs are normal — run it in the background if
   your harness supports that, otherwise let it block.
4. **Capture the real diff.** `git diff` plus `git status --porcelain` for new untracked
   files, written into the run directory — both excluding `.forge/`, which is forge's own
   memory and not the dwarf's work. This is qa's input.
5. **Dispatch qa** against that diff.
6. **Report.** Summarize what the dwarf changed (files touched, one line on the substance),
   then pass qa's findings through faithfully. Ask whether to apply fixes, send it back for
   another pass, or stop. Do not auto-loop — one dwarf → qa pass is the run.

### Dispatching

Create one run directory outside the repo and reuse it for both stages, so the prompts,
logs and diff of a run stay together:

```bash
FORGE_RUN="$(mktemp -d "${TMPDIR:-/tmp}/forge-XXXXXX")"
```

It must live outside the working tree. Anything forge writes inside the repo shows up in
the dwarf's own diff and ends up in front of qa as if the dwarf had written it.

**Dwarf:**

```bash
bash <skill_dir>/scripts/forge-memory.sh inject "$REPO" dwarf > "$FORGE_RUN/dwarf.prompt"

cat >> "$FORGE_RUN/dwarf.prompt" <<'EOF'

<goal, restated as a direct implementation instruction>

<the approach, if you or --planner produced one>

Edit the files in this repository directly. When you are done, run whatever tests or
checks the project already has and report the result.

You are running headless: nobody is at the other end and no one can answer a question. If
something is ambiguous, pick the most reasonable interpretation, proceed, and state the
assumption in your final message.
EOF

bash <skill_dir>/scripts/forge-dispatch.sh dwarf <spec> \
  --repo "$REPO" --run-dir "$FORGE_RUN" --prompt-file "$FORGE_RUN/dwarf.prompt" [--yolo]
```

That last paragraph is not boilerplate. Headless dwarves genuinely stop and ask a
clarifying question, which nothing can answer — the run then ends with an empty diff and a
question in the log, having spent the quota and changed nothing.

Close both role prompts with the learning note, and record afterwards:

```bash
bash <skill_dir>/scripts/forge-memory.sh note dwarf >> "$FORGE_RUN/dwarf.prompt"
# ... dispatch ...
bash <skill_dir>/scripts/forge-memory.sh record "$REPO" --last "$FORGE_RUN/dwarf.last" \
     --role dwarf --model <spec>
```

**QA:**

```bash
cd "$REPO" && git diff -- . ':(exclude).forge' > "$FORGE_RUN/changes.diff"
# new files too — and .forge/ excluded from both, or forge's own memory reaches qa
# as if the dwarf had written it.
git status --porcelain -- . ':(exclude).forge' >> "$FORGE_RUN/changes.diff"

{
  bash <skill_dir>/scripts/forge-memory.sh inject "$REPO" qa
  echo "The implementer was asked to: <goal>"
  echo
  echo "Review the diff below for correctness bugs: logic errors, broken edge cases,"
  echo "wrong behavior versus what was asked. Also say if it solved a different problem"
  echo "than the one stated, or changed files outside the goal's scope."
  echo
  echo "Report findings only — do not edit any file. For each finding give the file and"
  echo "line, what breaks, and a concrete input that triggers it. Mark each CONFIRMED if"
  echo "you traced it in the code, or PLAUSIBLE if you could not fully verify it. If the"
  echo "diff is correct, say so plainly rather than inventing something to report."
  echo
  echo '```diff'
  cat "$FORGE_RUN/changes.diff"
  echo '```'
  bash <skill_dir>/scripts/forge-memory.sh note qa
} > "$FORGE_RUN/qa.prompt"

bash <skill_dir>/scripts/forge-dispatch.sh qa <spec> \
  --repo "$REPO" --run-dir "$FORGE_RUN" --prompt-file "$FORGE_RUN/qa.prompt" [--yolo]
```

Inlining the diff is what makes qa portable: it pins the review scope exactly, works the
same on all four harnesses, and does not depend on the reviewer being able to reach a file
outside the repo.

For a large diff that would be unwieldy inline, `--native-review` switches codex to its
purpose-built reviewer (`codex exec review --uncommitted`). Know the trade before using
it: codex refuses a custom prompt alongside its scope flags, so the reviewer never learns
what the dwarf was *asked* to do. It can still judge whether the code is correct, but not
whether it is the right change — so mention that limitation when you report its findings.

Each stage writes `<role>.resolved` (what the spec resolved to), `<role>.prompt`,
`<role>.cmd`, `<role>.log` and `<role>.last` (the final message) into the run directory.
Read `<role>.last` for the result and fall back to `<role>.log` when a run fails.

Exit codes: `0` success, `2` a spec or usage error, `3` the harness is missing, `4` the
backend ran and failed. On `3` or `4`, show the user the actual error from the log instead
of substituting a different model — quietly swapping backends hides the fact that the one
they picked is broken.

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
- `scripts/forge-memory.sh` — `inject` / `note` / `record` / `show` / `prune`.
- `scripts/forge-parallel.sh` — `plan` / `run` / `integrate` for decomposed runs.
- `scripts/forge-install.sh` — installs `/forge` into all five harnesses. Claude,
  OpenClaude, Codex and opencode reference this directory live, so edits take effect
  immediately; antigravity gets a copy and needs the installer re-run after any change.
