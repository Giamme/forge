# forge

**One model builds it. A different model reviews what it actually built.**

`forge` is a skill for agent CLIs. You hand it a coding task and two models: a **dwarf**
that implements the change, and a **qa** reviewer that reads the dwarf's real diff — not
the dwarf's summary of its own work. A model grading its own homework isn't a check; the
entire value of the second stage is that it sees the code rather than the claim.

Either role can be any model, at any reasoning effort, through any of five agent CLIs:

| | codex | claude | openclaude | opencode | antigravity |
|---|---|---|---|---|---|
| binary | `codex` | `claude` | `openclaude` | `opencode` | `agy` |

So `--dwarf sol:xhigh --qa opus` runs GPT through Codex and Claude Opus through Claude
Code, in one command, from whichever CLI you happen to be sitting in.

With `--decompose-level` it goes wider: the goal is split into tasks, several dwarves work
**concurrently in isolated git worktrees**, each task is reviewed on its own diff, and only
the tasks that pass get merged.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [The spec: `alias:effort:harness`](#the-spec-aliaseffortharness)
- [Effort, ceilings and clamping](#effort-ceilings-and-clamping)
- [Yolo mode](#yolo-mode)
- [Decomposed runs](#decomposed-runs)
- [Planning](#planning)
- [Project memory](#project-memory)
- [Full command reference](#full-command-reference)
- [Model registry](#model-registry)
- [Run artifacts](#run-artifacts)
- [Safety guarantees](#safety-guarantees)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

---

## Install

```bash
git clone https://github.com/Giamme/forge.git ~/.claude/skills/forge
bash ~/.claude/skills/forge/scripts/forge-install.sh
```

Clone anywhere you like — the installer works out its own location and links from there.
`~/.claude/skills/forge` is just the tidiest home, since one of the five harnesses reads
that path directly.

Then check what's actually reachable on your machine:

```bash
bash ~/.claude/skills/forge/scripts/forge-dispatch.sh doctor
```

```
HARNESS      STATUS     DETAIL
codex        found      /Users/you/.nvm/versions/node/v24.18.0/bin/codex
claude       found      /Users/you/.local/bin/claude
openclaude   found      /Users/you/.nvm/versions/node/v24.18.0/bin/openclaude
opencode     found      /Users/you/.opencode/bin/opencode
antigravity  MISSING    agy not on PATH
```

A `MISSING` harness is fine — forge only needs the ones you actually route models through.

**Binary presence is not reachability.** A harness can be installed and still fail on auth or
billing (opencode in particular fails this way), and that failure only surfaces mid-dispatch.
To prove a pairing end to end before spending a real run:

```bash
bash scripts/forge-dispatch.sh dwarf <spec> --prompt-file /dev/stdin --repo <scratch dir> \
    <<< 'Reply with exactly: FORGE_OK'
```

### What the installer does per harness

Each CLI discovers skills differently, and the differences are not guessable — so verify
with the listed check rather than trusting file placement.

| Harness | Mechanism | Verify with |
|---|---|---|
| claude | `~/.claude/skills/forge/` | `/forge` appears in the skill list |
| openclaude | symlink into `~/.openclaude/skills/` | `openclaude skills list --json` |
| codex | symlink into **`~/.codex/skills/`** | `codex exec "do you have a skill named forge?"` |
| opencode | **nothing** — it scans `~/.claude/` itself | `opencode debug skill` |
| antigravity | `agy plugin install` of a generated wrapper | `agy --print='/forge'` |

> **The one ongoing caveat:** `agy plugin install` *copies* the files instead of referencing
> them. The other four track the source directory live, so an edit reaches them instantly —
> antigravity keeps running whatever it copied. **Re-run `forge-install.sh` after any edit**,
> or agy silently runs a stale version.

`forge-install.sh --dry-run` prints what it would do and touches nothing. It never clobbers
a real directory that isn't a symlink.

---

## Quick start

Inside any installed harness, invoke the skill:

```
/forge "add retry with exponential backoff to the HTTP client" --dwarf sol:high
```

That's the whole minimum. `sol` builds it at `high` effort on Codex, `opus` reviews the
resulting diff at `xhigh` (the default reviewer), and you get back a summary of the change
plus qa's findings.

**Pick both sides explicitly:**

```
/forge "fix the off-by-one in the pagination cursor" --dwarf luna:max --qa sol:xhigh
```

**Send a model through a different CLI** — same model, different harness:

```
/forge "port the config loader to TOML" --dwarf sol:high:openclaude --qa opus
```

**Let the dwarf off the leash** so it can install deps and run the suite:

```
/forge "add integration tests against a real Postgres container" --dwarf sol:xhigh --yolo-dwarf
```

**Split it across several dwarves working in parallel:**

```
/forge "add auth: middleware, token model, login route, and docs" \
  --decompose-level medium \
  --dwarf-high sol:xhigh --dwarf-low gemini:low:antigravity --dwarf luna:high
```

forge decomposes the goal, rates each task's difficulty, routes it, then **shows you the
table and stops** before spending anything.

---

## The spec: `alias:effort:harness`

Everywhere forge takes a model, it takes the same three-field spec. Each field after the
first is optional:

| Spec | Resolves to |
|---|---|
| `sol` | alias `sol`, default effort for the role, its default harness (codex) |
| `sol:xhigh` | `sol` at `xhigh` effort on its default harness |
| `sol:xhigh:openclaude` | same model and effort, run through the `openclaude` CLI |
| `opus::claude` | explicit harness, effort left to the role default — the middle field may be empty |
| `gpt-5.6-sol::codex` | not in the registry? passed through as a literal model id — but only with an explicit harness |

An alias with no registry row still works, provided you name the harness. forge won't guess
a default harness for a model it has never seen; that guess would silently spend quota on
the wrong provider.

**Role defaults when you leave things out:**

- `--dwarf` missing → forge **asks**. Every dispatch spends real quota on your account, so
  picking for you isn't its call.
- `--qa` missing → `opus`, which runs locally through `claude` and adds no cost surprise.
- effort missing → dwarf builds at `medium`, qa reviews at `xhigh`. A reviewer thinking
  *less* than the implementer tends to wave things through, which wastes the stage entirely.

---

## Effort, ceilings and clamping

Every harness has its own effort ladder:

| Harness | Ladder |
|---|---|
| codex | `low medium high xhigh max ultra` |
| claude | `low medium high xhigh max` |
| openclaude | `low medium high xhigh max ultracode` |
| antigravity | `low medium high` |
| opencode | provider-specific; forwarded verbatim |

And support is more irregular than one ceiling per backend:

- **Ceilings** — `luna` tops out at `max` on codex, so `luna:ultra` clamps down.
- **Gaps** — `gemini-pro` offers `low` and `high` but *not* `medium`, so `gemini-pro:medium`
  clamps to `low`.
- **None at all** — Claude models on antigravity take no effort flag; passing one errors, so
  forge reports it and drops it.

When forge clamps, it says so (`clamped=ultra -> max`) and the orchestrator passes that on
to you. Getting less thinking than you paid for, silently, is exactly the failure worth ten
lines of validation to prevent — and it's a real one: **codex accepts an invalid effort
without complaint and quietly runs at some other level.** forge validates first.

A word that isn't a recognized effort at all is an error, not a clamp.

---

## Yolo mode

`--yolo-dwarf` and `--yolo-qa` drop that role's sandbox and approval gates:

| Harness | Flag used |
|---|---|
| codex | `--dangerously-bypass-approvals-and-sandbox` |
| claude / openclaude / antigravity | `--dangerously-skip-permissions` |
| opencode | `--auto` |

Each flag applies to **one role only**, so `--yolo-dwarf` alone is the normal combination:
the implementer gets a free hand, the reviewer stays fenced in and still can't edit the code
it's judging.

You mostly don't need it. On codex, claude and openclaude the sandboxed roles are already
useful — the dwarf edits files and runs tests, qa reads and runs commands but cannot write.
Reach for yolo when a role genuinely must escape the sandbox: installing dependencies,
writing outside the repo, touching the network.

> **Antigravity is the exception worth knowing.** Without yolo it cannot run *any* shell
> command, and a single denied command **aborts the whole run** rather than degrading — a
> dwarf that tries to run the test suite ends with an error and an empty diff. forge detects
> this and appends a note telling that role not to attempt commands, which is why an
> antigravity dwarf reports its work as unverified by execution. If you want a Gemini dwarf
> that can actually run the tests it writes, it needs `--yolo-dwarf`.

---

## Decomposed runs

`--decompose-level low|medium|high` turns one dwarf into many. Without the flag, nothing
changes — one dwarf, one QA pass.

### Two independent axes

Easy to conflate, so: **decompose-level** is how finely the *goal* is split. **difficulty**
is how hard each resulting *task* is.

| `--decompose-level` | Split |
|---|---|
| `low` | only where the goal has obviously independent pieces; 2–3 coarse tasks |
| `medium` | one task per coherent unit (module, feature slice); typically 4–6 |
| `high` | finest split where each task is still independently reviewable *and* independently correct; typically 7–12 |

| `difficulty` | Means |
|---|---|
| `low` | mechanical and local: rename, move, docs, a test for behaviour that already exists |
| `medium` | self-contained implementation against a clear spec; bounded blast radius |
| `high` | needs design judgment, cross-cutting behaviour, subtle edge cases, state or concurrency |

Difficulty is about **judgment required, not diff size**. A five-hundred-line mechanical
rename is `low`. A ten-line concurrency fix is `high`. Getting this backwards sends the cheap
model at the subtle problem and the expensive one at the boilerplate — worse than not routing
at all.

### Routing dwarves to tasks

Per-task assignment can't be expressed when you type the command, because the tasks don't
exist yet. Naming a dwarf per task would mean guessing how the goal will split. So you map
**model tiers to difficulty tiers**, once, in advance — three layers, each optional:

```bash
# 1. one dwarf for everything (today's syntax, unchanged)
--dwarf sol:xhigh

# 2. per-tier routing, with plain --dwarf as the fallback for any tier not named
--dwarf-high sol:xhigh --dwarf-medium luna:high --dwarf-low gemini:low:antigravity
--dwarf sonnet:medium

# 3. pool several models within one tier — round-robined across tasks
--dwarf-high sol:xhigh,terra:xhigh
```

`--qa`, `--qa-high`, `--qa-medium`, `--qa-low` mirror it exactly. QA has no tier rules by
default and falls back to `opus`.

Resolution order per task: **explicit column value → the tier's rule → plain `--dwarf` →
`UNASSIGNED`**. An `UNASSIGNED` task makes `plan` exit `2` and `run` refuse outright. forge
does not invent a model when you haven't said which one should spend your quota.

Layer three is **the approval gate**: after decomposition, before any dispatch, you get the
task table with each task's difficulty and resolved dwarf/qa. Retier a task ("2 is actually
medium") and it re-routes per your rules, or override a single row's dwarf outright. This is
also the cost gate — `high` on a large goal is 20+ dispatches, which should be visible before
it's spent.

### Waves, and what actually bounds parallelism

A **wave** is the largest set of tasks whose dependencies are all satisfied by earlier waves
**and** whose declared `files` sets are pairwise disjoint.

Disjointness — not the task count — is the real bound. Two dwarves editing one file produce a
conflict no reviewer can untangle and no merge can resolve honestly, so they land in different
waves however the goal was decomposed. When that happens forge says so:

```
deferrals (serialized to protect the merge):
  wave 1: lint deferred (file overlap with a task already in this wave)
```

That line matters. A decomposition that looks parallel but serializes at run time should
announce it, rather than leaving you wondering why eight tasks took eight rounds. When most
tasks overlap, the fix is a better split — not more parallelism.

Each wave branches from the integration branch **as it currently stands**, and passing tasks
merge `--no-ff` onto it as their wave completes. So a wave-2 task that depends on a wave-1
task genuinely sees its dependency's merged code.

### Fresh session per task, plus a capsule

Every dispatch is a **clean slate** — forge never passes `--continue`, `--resume`, or
`codex exec resume`. One task's confused turn can't poison another, and each task's context
stays small.

Clean slates alone would leave every dwarf blind to the wider run, so a short `capsule.md` is
regenerated before each dispatch and prepended to both the dwarf and QA prompt:

```
# forge run <run-id>
Goal: <overall goal>
You are doing ONE task of <N>. Do not do the others' work.

## Your task
tokens — Add the token model   difficulty: medium
Files you own: src/auth/token.py

## Run status
id        diff     status       files
middleware high    MERGED       src/auth/middleware.py
tokens     medium  IN FLIGHT    src/auth/token.py
docs       low     PENDING      README.md

## Ground rules
- Your branch already contains every MERGED task's work — do not reimplement it.
- Files owned by other tasks are off limits; another dwarf is editing them now.
- If your task truly needs a change in someone else's file, say so in your final
  message instead of making it.
```

It's written **per task**, not once per run — tasks in a wave execute concurrently, so a
single shared capsule file is a race in which each task overwrites it and reads back whichever
sibling wrote last, handing a dwarf someone else's task description.

For QA the capsule additionally scopes the review: code from `MERGED` tasks is already in the
base and is not this task's bug. Without that, a wave-2 reviewer reports wave-1 code as
defects in the diff it was handed.

### Verdicts and failure

Decompose-mode QA must end with a sentinel line:

```
FORGE_VERDICT: PASS     no confirmed correctness bug (style nits are not failures)
FORGE_VERDICT: FAIL     at least one CONFIRMED correctness bug
```

The runner reads the last occurrence. A missing verdict is `UNKNOWN` and is treated exactly
like a failure — excluded from integration and flagged. Merging a diff whose reviewer never
reached a conclusion would defeat the point of reviewing it, so the ambiguous case fails safe.

A failing task is excluded and **its branch and worktree are preserved**; the rest of the run
proceeds. There is no automatic repair loop, consistent with forge's rule that a dwarf → qa
cycle does not loop on its own.

---

## Planning

A dwarf that is handed only an objective plans its own approach, silently, inside its own
run. Usually fine — but in a decomposed run the capsule prevents two dwarves editing one
*file* and does nothing about them inventing incompatible *interfaces* at a shared seam.
Both stay in their lane, both pass their own QA, and the mismatch surfaces at integration.

So forge plans before it dispatches. By default the orchestrator does it and writes the
approach into each task's prompt — no extra dispatch. `--planner <spec>` hands that stage to
a named model instead:

```bash
/forge "migrate the API layer to gRPC" --decompose-level medium \
  --planner sol:xhigh --dwarf-high sol:high --dwarf luna:high
```

**One planner dispatch per run, never one per task.** That is the whole point: a single mind
designs both sides of every seam, where N independent planners would recreate the problem.
Effort defaults to `xhigh`, since a bad plan is executed at full price by every dwarf
downstream of it. The planner runs with QA's permission profile — it reads the repo and
writes nothing to it.

The approach shows up in the approval table under each task, which is the last moment it can
be changed for free, and it is handed to QA as the *intended* approach — so a review can
finally say "this works, but it abandons the planned shape" rather than only judging against
the objective.

## Project memory

Every forge dispatch is a clean slate, so without help the tenth run in a repo rediscovers
what the first one learned. forge keeps that in the repo, in two files:

```
.forge/memory.md     small, capped, injected into every prompt
.forge/ledger.tsv    append-only, one row per dispatch, never injected
```

The split is the design. Injected context is paid for on every dispatch of every future run,
so "remember everything" and "stay small" pull against each other — the ledger remembers at
zero context cost and is read only when asked, while `memory.md` is capped at 40 lines / 4 KB.

```markdown
## Verify
- pytest -q runs the suite; make test also lints and is 4x slower

## Known traps
- tests/test_api.py::test_timeout is flaky under -n auto  [tests/test_api.py]

## Recurring QA findings
- 3x: route added without updating openapi.yaml  [src/routes]
```

### How a fact gets in

The role that just did the work writes it down, in its final message, next to the verdict:

```
FORGE_LEARNING: trap | src/proto/*_pb2.py is generated, never hand-edit [src/proto]
```

No extra dispatch and no separate model guessing what mattered — the one that hit the
landmine is the one that records it. Everything after that is deterministic bash.

**What stops it becoming a junk drawer:**

| Guard | Effect |
|---|---|
| recurrence threshold | a `finding` needs **two distinct runs** before it is injected. One occurrence is an incident, not a pattern |
| stale anchors | an entry ending in `[path]` is dropped once that path stops existing, so memory stays true as code moves |
| hard cap | over 40 lines / 4 KB, lowest-count and least-recent facts are trimmed; one entry is clamped to 180 chars |
| bias to silence | the prompt says most runs teach nothing durable and emitting nothing is correct. A false fact costs every future run; a missing one costs a single rediscovery |

Only `verify` and `trap` are kept on first sighting — those are facts, true the first time.

### Slices, and what forge writes

| Role | Gets |
|---|---|
| planner, dwarf | Verify + Known traps + Recurring findings |
| qa | Known traps + Recurring findings |

QA is not told the build command; it is not building anything. It *is* told the recurring
findings, so it can notice this is the fourth time the same bug shipped.

`.forge/` is the **only** thing forge writes into your working tree rather than onto a branch
of its own. It is excluded from every captured diff, so a reviewer never sees it as the
dwarf's work — but it *will* show up as untracked in `git status` after your first run.

### What to do with `.forge/` when it appears

forge never commits it. That decision is yours, and there are three sane answers:

| You want | Do this |
|---|---|
| memory shared with your team, and with future you | **commit `.forge/`** — the intended case; every teammate's runs then build on the same facts |
| memory kept to your machine | `echo '.forge/' >> .gitignore` |
| no memory at all | `--no-memory`, or `FORGE_MEMORY=off` in your environment |

Two things that are easy to get wrong:

**Gitignoring is not the same as turning it off.** An ignored `.forge/` is still written and
still injected into every prompt — it just stops travelling. Only `--no-memory` /
`FORGE_MEMORY=off` actually disables the feature.

**Never commit one file and ignore the other.** `memory.md` is *rebuilt from* `ledger.tsv` on
every write, so a clone that has the memory but not the ledger loses everything the moment
anyone runs forge there — the rebuild regenerates `memory.md` from that machine's own short
ledger and the shared facts are gone, silently. Verified behaviour, not a theoretical risk:

```
teammate A commits memory.md only, ledger.tsv gitignored
  memory.md: - pytest -q runs the suite
             - src/a.py is generated [src/a.py]

teammate B clones, runs forge once, learns one unrelated thing
  memory.md: - b learned something new        ← A's two facts destroyed
```

Commit both, or neither. If you commit them, expect `ledger.tsv` to gain a line per dispatch
— small, but it will appear in your diffs; that growth is the price of the recurrence
counting that keeps `memory.md` honest.

If you contribute to someone else's repo, gitignore it. A pull request containing forge's
notes to itself is noise to a reviewer who does not use forge.

## Full command reference

### The `/forge` slash command

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

| Flag | Default | Meaning |
|---|---|---|
| `--dwarf <spec>` | *asks* | the model that implements the change |
| `--qa <spec>` | `opus` at `xhigh` | the model that reviews the dwarf's diff |
| `--planner <spec>` | orchestrator plans | dispatch planning to a model; one dispatch per run, effort defaults to `xhigh` |
| `--no-memory` | memory on | write and inject nothing in `.forge/` |
| `--timeout <s>` | `2700` (45m) | kill any single dispatch that runs longer. `0` disables |
| `--yolo-dwarf` | off | drop the dwarf's sandbox and approval gates |
| `--yolo-qa` | off | drop the reviewer's sandbox |
| `--repo <dir>` | cwd | repository to work in |
| `--native-review` | off | use `codex exec review --uncommitted` instead of an inlined diff |
| `--decompose-level <l>` | off | split the goal into parallel tasks |
| `--max-parallel <n>` | `3` | concurrent dwarves per wave |
| `--dwarf-high/-medium/-low` | — | per-difficulty routing; comma-list pools models |
| `--qa-high/-medium/-low` | — | same, for reviewers |

**On `--native-review`:** for a diff too large to inline, this switches codex to its
purpose-built reviewer. Know the trade — codex refuses a custom prompt alongside its scope
flags, so the reviewer never learns what the dwarf was *asked* to do. It can still judge
whether the code is correct, but not whether it's the right change.

#### Examples

```bash
# minimum
/forge "make the cache key include the tenant id" --dwarf sol:high

# both roles named, reviewer thinking harder than the implementer
/forge "rewrite the rate limiter as a token bucket" --dwarf luna:max --qa sol:ultra

# same model, different CLI
/forge "extract the retry helper into its own module" --dwarf sol:high:openclaude

# Gemini as the dwarf, unsandboxed so it can actually run the tests it writes
/forge "add table-driven tests for the parser" --dwarf gemini:high:antigravity --yolo-dwarf

# a cheap dwarf with an expensive reviewer
/forge "update all the docstrings to match the new signatures" --dwarf haiku --qa opus:max

# work in another repo
/forge "bump the pinned deps and fix the fallout" --dwarf sol:xhigh --repo ~/dev/other-project

# large refactor, reviewed by codex's native reviewer
/forge "split the god object into three services" --dwarf sol:ultra --qa sol:ultra --native-review

# parallel, one dwarf for every task
/forge "add CRUD endpoints for projects, users and teams" \
  --decompose-level medium --dwarf sol:high

# parallel, routed by difficulty, wider fan-out
/forge "migrate the whole API layer from REST to gRPC" \
  --decompose-level high --max-parallel 5 \
  --dwarf-high sol:ultra --dwarf-medium luna:high --dwarf-low haiku:medium \
  --qa-high opus:max --qa sonnet:high

# pool two providers on the hard tier so neither quota carries it alone
/forge "implement the new billing rules across the codebase" \
  --decompose-level medium \
  --dwarf-high sol:xhigh,terra:xhigh --dwarf luna:high

# a model that thinks for a long time; raise the per-dispatch backstop
/forge "prove the scheduler cannot deadlock, and fix it if it can" \
  --dwarf sol:ultra --timeout 7200
```

After a decomposed run, a task that failed review can be re-run on its own — with the
findings that failed it, on a stronger model:

```bash
bash scripts/forge-parallel.sh retry /tmp/forge-billing pricing --dwarf sol:ultra
```

### `scripts/forge-dispatch.sh`

Resolves a spec into a real CLI invocation and runs it. Needs only bash and coreutils, which
is the point: it behaves identically no matter which harness is orchestrating.

```
forge-dispatch.sh doctor
forge-dispatch.sh dwarf   <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
forge-dispatch.sh qa      <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
                                 [--native-review [--review-base <ref>]]
forge-dispatch.sh planner <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--dry-run]
```

| Option | Meaning |
|---|---|
| `--prompt-file <f>` | file holding the prompt (required for `dwarf`/`qa`) |
| `--repo <dir>` | repository the role operates in |
| `--run-dir <d>` | where prompts, logs and results are written |
| `--yolo` | bypass sandbox and approvals for this role |
| `--dry-run` | resolve and print the command; run nothing |
| `--native-review` | qa only; use codex's built-in reviewer |
| `--review-base <ref>` | qa only; base ref for the native review |
| `--agy-timeout <s>` | antigravity `--print-timeout` (long runs need this raised) |
| `--timeout <s>` | kill this dispatch after `<s>` seconds. Default `2700` (45m); `0` disables; `FORGE_TIMEOUT` sets it in the environment |

**Exit codes:** `0` ok · `2` spec or usage error · `3` harness missing or unusable ·
`4` the backend ran and failed · `7` the backend hit `--timeout` and was killed.

Only antigravity bounds its own runtime, and stock macOS has no `timeout` binary to wrap the
others in — so a hung backend used to block forever, and under a decomposed run's `xargs -P`
one hung task silently stalled its whole wave. The default is deliberately generous: this is
a backstop against a wedged process, not a budget. A role killed by it has usually left a
partial edit behind, so `7` is worth reporting as such rather than as a clean failure.

On `3` or `4`, forge shows you the actual error rather than substituting a different model —
quietly swapping backends hides the fact that the one you picked is broken.

```bash
# what would this actually run?
bash scripts/forge-dispatch.sh dwarf sol:ultra --prompt-file /tmp/p --dry-run

# check a clamp before committing to a long run
bash scripts/forge-dispatch.sh qa gemini-pro:medium --prompt-file /tmp/p --dry-run
# effort=low
# clamped=medium -> low        (gemini-pro offers low and high, but no medium)
```

### `scripts/forge-solo.sh`

The whole single-task run: memory injection, prompt assembly, the dwarf dispatch, the diff
capture, the QA dispatch, and the ledger writes.

```
forge-solo.sh <run-dir> --repo <dir> --dwarf <spec> [--qa <spec>]
              [--approach <file>] [--yolo-dwarf] [--yolo-qa]
              [--native-review] [--no-memory] [--timeout <s>] [--dry-run]
```

You write two files into the run directory; forge does the rest:

| You write | Is |
|---|---|
| `prompt.md` | the implementation instruction (required) |
| `goal.txt` | one line, what was asked — given to the reviewer as intent (optional) |

| It writes | Is |
|---|---|
| `dwarf.last` / `qa.last` | each role's final message |
| `changes.diff` | exactly what the reviewer read |
| `verdict` | `PASS`, `FAIL`, `UNKNOWN` or `NOCHANGES` |
| `<role>.{input,log,resolved,cmd}` | the prompt, the transcript, the resolution, the command |

```bash
RUN="$(mktemp -d /tmp/forge-XXXXXX)"
echo "add retry with backoff to the HTTP client" > "$RUN/goal.txt"
echo "Add exponential backoff to src/http.py, capped at 5 attempts." > "$RUN/prompt.md"

bash scripts/forge-solo.sh "$RUN" --repo ~/dev/api --dwarf sol:high --qa opus
```

**Exit codes:** `0` reviewed · `2` usage · `3` not a git repository · `4` a dispatch failed ·
`5` **the dwarf produced no changes** · `7` a dispatch timed out.

`5` is the one worth recognising on sight. Headless dwarves genuinely stop to ask a
clarifying question that nothing can answer; the run ends having spent the quota and changed
nothing, with the question sitting in `dwarf.last`.

Two things it gets right that are easy to get wrong by hand, and that fail *silently* rather
than loudly:

- **`.forge/` is excluded from the capture.** Forge rewrites its own memory in the working
  tree at the end of every run, so left in, it reaches the reviewer as a file the dwarf
  appears to have touched — which a good reviewer correctly flags as scope creep.
- **New files are diffed against `/dev/null`.** A `git status` line says only `?? calc.py`.
  A dwarf whose entire task was to add a file would have had its actual code reviewed by
  nobody, while the run still reported a clean QA pass.

The run directory must live outside the repository — anything forge writes inside the working
tree shows up in the dwarf's own diff. `forge-solo.sh` refuses rather than letting that happen
quietly.

### `scripts/forge-parallel.sh`

Decomposed runs: many dwarves in isolated worktrees, per-task QA, only passing work merged.
bash 3.2 compatible, because that's what `/bin/bash` is on macOS — hence `xargs -P` for
concurrency rather than `wait -n`, and no associative arrays anywhere.

```
forge-parallel.sh plan      <plan-dir> --repo <dir> [routing flags] [--planner <spec>] [--no-memory]
forge-parallel.sh run       <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa] [--dry-run]
forge-parallel.sh retry     <plan-dir> <task-id> [--dwarf <spec>]
forge-parallel.sh integrate <plan-dir> --approved
```

| Subcommand | Does |
|---|---|
| `plan` | validate `tasks.tsv`, resolve difficulty → concrete dwarf/qa specs, compute waves, render the approval table |
| `run` | execute waves; per task: worktree → dwarf → diff → QA → status; merge passing tasks onto the integration branch |
| `retry` | re-run **one** failed task in the worktree it already has, with its reviewer's findings in the prompt |
| `integrate` | **never automatic** — merge the integration branch into your branch |

**Exit codes:** `0` ok · `2` usage or validation · `3` precondition (not a git repo, …) ·
`5` some task failed QA · `6` integration conflict.

```bash
PLAN=/tmp/forge-auth

# 1. plan and inspect the routing
bash scripts/forge-parallel.sh plan "$PLAN" --repo ~/dev/api \
  --dwarf-high sol:xhigh --dwarf-low gemini:low:antigravity --dwarf luna:high

# 2. see exactly what will be dispatched, without dispatching
bash scripts/forge-parallel.sh run "$PLAN" --dry-run

# 3. run it, four dwarves at a time, implementers unsandboxed
bash scripts/forge-parallel.sh run "$PLAN" --max-parallel 4 --yolo-dwarf

# 4. after reading the results, deliver the passing work
bash scripts/forge-parallel.sh integrate "$PLAN" --approved
```

`integrate` refuses without `--approved`, and refuses on a dirty working tree.

#### Retrying a failed task

```bash
# re-run one task with the findings that failed it, on a stronger model
bash scripts/forge-parallel.sh retry "$PLAN" middleware --dwarf sol:xhigh
```

The findings are the point. `retry` prepends the previous reviewer's report to the dwarf's
prompt — "your last attempt was reviewed and these were the findings; fix them" — and reuses
the worktree, so the dwarf continues from its own code rather than starting over. If the
verdict comes back `PASS` the task merges like any other.

`--dwarf` is written back into `tasks.tsv`, so the table shows the model that will actually
be spent and the escalation survives a re-plan.

Two things it deliberately does:

- **Reviews the task's cumulative diff**, against the commit the task was originally branched
  from — not just the fix. Reviewing the fix alone would let the first attempt's code through
  unread.
- **Refuses to re-review identical code.** If the retry produces a byte-identical diff, the
  task fails again without spending a QA dispatch.

`retry` is something you run. Forge never loops a dwarf against its own reviewer on its own
initiative.

#### Resuming an interrupted run

Run `run` again. Tasks already `MERGED` are skipped rather than re-dispatched, worktrees are
reused, and each wave re-bases on the integration branch as it now stands:

```
forge: wave 1: 3 task(s) already merged, resuming the rest
forge: wave 1: dispatching 2 task(s), max 3 in parallel
```

A killed process, a hung harness or a closed laptop costs you the tasks that were in flight,
not the ones that landed.

#### Scope drift

After each dwarf, the runner compares what the task **declared** in `files` against what it
actually touched (a declared directory covers the paths beneath it). Undeclared paths appear
in the summary and in the QA prompt:

```
scope drift (touched files the task did not declare):
  auth           src/db.py
```

Never a failure — a dwarf that genuinely needed one more file did the right thing. What it
buys is that a merge `CONFLICT` two waves later arrives with its cause already named.

#### `tasks.tsv`

Tab-separated, one row per task, written during decomposition:

```
# id  deps  difficulty  files  dwarf  qa  title
middleware	-	high	src/auth/middleware.py	-	-	Auth middleware
tokens	-	medium	src/auth/token.py	-	-	Token model
route	middleware,tokens	medium	src/routes/login.py	-	-	Login route
docs	-	low	README.md	-	-	Document the auth flow
```

| Column | Meaning |
|---|---|
| `id` | short unique slug; also the worktree and branch name |
| `deps` | comma-list of task ids that must land first, or `-` |
| `difficulty` | `low` \| `medium` \| `high` |
| `files` | expected touch set, comma-separated — **drives disjointness** |
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins |
| `title` | one line, shown in the approval table and the capsule |

Each task also needs `tasks/<id>/prompt.md`, written the same way a single-task forge goal is.

`files` is a **promise, not a prediction**: it's what the wave planner trusts when deciding
what may run concurrently, and what the capsule tells other dwarves not to touch. An
understated `files` is how two dwarves end up in the same file.

`plan` writes resolved specs back into the `dwarf`/`qa` columns, so `run` never consults the
routing rules — and an explicit value survives a re-plan, which is what makes a gate override
durable. (`UNASSIGNED` is treated as a leftover marker and re-resolved.)

### `scripts/forge-memory.sh`

```
forge-memory.sh inject <repo> <planner|dwarf|qa>   # the block to prepend to a prompt
forge-memory.sh note <planner|dwarf|qa>            # the instruction to append
forge-memory.sh record <repo> --last <file> --role <r> [--run-id X] [--task T]
                              [--model M] [--verdict V] [--duration S]
forge-memory.sh show <repo>
forge-memory.sh prune <repo>                       # re-apply staleness and the cap now
forge-memory.sh spend <repo> [--run <run-id>]      # what forge has cost this repo
```

`record` appends to the ledger and then rebuilds `memory.md` from the whole ledger — the
rebuild is idempotent, so a crashed run can leave the ledger a row short but can never leave
memory half-written. Editing `memory.md` by hand therefore does not stick; remove the ledger
rows instead.

```bash
# what would a dwarf actually be told about this repo?
bash scripts/forge-memory.sh inject ~/dev/api dwarf

# start over
rm -rf ~/dev/api/.forge
```

#### Spend

The ledger already holds a row per dispatch and is never injected into a prompt, so
accounting from it costs nothing:

```bash
bash scripts/forge-memory.sh spend ~/dev/api
```
```
14 dispatch(es) across 3 run(s), 1h12m of model time
Durations sum concurrent work, so they exceed the wall-clock of a parallel run.

  by model                     disp     total      mean
  sol:xhigh                       4     38m20s     9m35s
  opus                            7     22m10s     3m10s
  gemini:low:antigravity          3     11m30s     3m50s

  by role                      disp     total
  dwarf                           7     49m50s
  qa                              7     22m10s
```

**Time and dispatch counts, never tokens or money.** The five CLIs expose usage differently
or not at all, and a number forge cannot actually measure would be worse than no number.

`duration_s` is the ledger's tenth column, appended at the end on purpose: a ledger written
by an older forge — possibly already committed and shared — has nine fields, and awk yields
an empty string for the missing one instead of shifting every column. Those rows are counted
but not timed, and the output says so.

### `scripts/forge-install.sh`

```
forge-install.sh [--dry-run]
```

Links this directory into every installed harness. Skips harnesses that aren't present,
skips a destination that exists and isn't a symlink (it will never clobber a directory you
may have edited in place), and reports opencode as auto-detected.

---

## Model registry

`registry.tsv` is the single source of truth for alias resolution. Four tab-separated
columns:

```
# alias	harness	model	effort
sol	codex	gpt-5.6-sol	<=ultra
sol	openclaude	gpt-5.6-sol	<=max
gemini-pro	antigravity	gemini-3.1-pro	low,high
opus	antigravity	claude-opus-4-6-thinking	none
grok	opencode	github-copilot/grok-4.6	-
```

| `effort` form | Means |
|---|---|
| `<=X` | any effort on that harness's ladder up to and including `X` |
| `a,b,c` | exactly these efforts — gaps are real, and this is how they're expressed |
| `none` | takes no effort flag at all; passing one errors |
| `-` | no validated ladder; forwarded verbatim |

**Row order is meaningful:** the *first* row for an alias is that alias's default harness.
`--dwarf sol` means sol on codex; `--dwarf sol:high:openclaude` moves the same model
elsewhere.

Adding a model is one line in this file. Nothing else needs to change.

---

## Run artifacts

Each stage writes into the run directory, which lives **outside** the repo — anything forge
writes inside the working tree would show up in the dwarf's own diff and land in front of qa
as if the dwarf had written it.

```
<run-dir>/
  <role>.resolved     what the spec resolved to (model, effort, harness, any clamp)
  <role>.prompt       the exact prompt sent
  <role>.cmd          the exact command line executed
  <role>.log          full transcript
  <role>.last         the final message — read this first
  changes.diff        the dwarf's real diff, which is qa's input
```

Plus, in the repo itself and surviving the run:

```
.forge/memory.md    what forge learned here, injected into future prompts
.forge/ledger.tsv   one row per dispatch, ever; never injected
```

A decomposed run adds:

```
<plan-dir>/
  goal.txt
  tasks.tsv
  waves.tsv, deferrals.txt
  tasks/<id>/{capsule.md,prompt.md,dwarf.*,qa.*,changes.diff,status}
  results.tsv                          id  status  branch
```

with worktrees and branches at:

```
<repo>/../.forge-worktrees/<run-id>/<task-id>    worktree
forge/<run-id>/<task-id>                         task branch
forge/<run-id>-integration                       integration branch
```

The worktree root is a **sibling of the repo, never `TMPDIR`**. Isolated worktrees are never
pushed, so a temp root that gets cleaned takes the only copy of that work with it.

---

## Safety guarantees

- forge **never** `git commit`s, `git push`es, `git reset --hard`s, or otherwise finalizes on
  your behalf in a normal run. It edits the working tree and reports; every irreversible
  decision is yours.
- In a decomposed run, commits and merges happen **only** on the `forge/<run-id>/*` task
  branches and the `forge/<run-id>-integration` branch — branches forge created for itself.
  Never your branch, never a push. Your branch and working tree are untouched for the whole
  run.
- Bypass flags are never added on forge's own initiative. `--yolo-dwarf` / `--yolo-qa` are the
  only route to an unsandboxed run, so that choice is always explicit and visible.
- **One dwarf per working tree, always.** Two agents editing one tree interleave their edits
  into a diff neither of them wrote and qa cannot untangle. Parallelism comes from separate
  worktrees, which is why worktrees are mandatory there rather than an optimization.
- A failed task's branch and worktree are **never** deleted — they're the only record of what
  went wrong and the only thing to retry from. A successful task's worktree is removed only
  after `results.tsv` is written and read back.
- qa cannot edit the code it is reviewing (unless you pass `--yolo-qa`).

### Preconditions

- Not a git repo → decomposed runs refuse. Worktrees are impossible and there's no honest
  fallback.
- Uncommitted changes → **warned prominently.** Worktrees branch from a commit, so your local
  edits are invisible to every dwarf. This is the likeliest route to a confusingly wrong
  result.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| skill missing in **codex** only | frontmatter isn't strict YAML. Codex refuses to load and logs `invalid YAML`; the other four are lenient and load it fine. Note codex's line numbers exclude the opening `---`, so its "line 3" is the file's line 4 |
| skill missing in **antigravity** | `agy` ignores `~/.gemini/skills` entirely — skills load only inside a plugin. Re-run `forge-install.sh` |
| antigravity runs an **old version** | `agy plugin install` copies. Re-run `forge-install.sh` after every edit |
| duplicate entry in **opencode** | opencode auto-scans `~/.claude/`; a `~/.config/opencode/command/forge.md` on top is redundant |
| agy: `Find command timed out` | missing `--add-dir <repo>` — forge passes it; a hand-built command must too |
| agy dwarf ends with an error and an empty diff | it tried to run a shell command without yolo. A single denial aborts the run — use `--yolo-dwarf` |
| dwarf produced no changes | usually an ambiguous prompt it couldn't resolve headlessly. Nobody can answer a question mid-run |
| task status `ERROR` | worktree creation or a dispatch failed — read `tasks/<id>/dwarf.out` / `qa.out` |
| task status `UNKNOWN` | QA never emitted a verdict line; read `tasks/<id>/qa.last` |
| task status `CONFLICT` | QA passed but the merge conflicted — `files` was understated somewhere. Check the run's scope-drift list: it usually names the file |
| task status `TIMEOUT` | that role exceeded the dispatch timeout and was killed. Raise `--timeout`, or `retry` — the partial edit is still in the worktree |
| a dispatch never returns | it can't any more: every backend is killed at `--timeout` (45m default) |
| a task won't re-run: "already exists" | a worktree directory was deleted by hand. `run` and `retry` prune stale registrations first, so re-run rather than deleting the branch |
| a failed task is expensive to redo | it isn't — `forge-parallel.sh retry <dir> <id> [--dwarf <spec>]` re-runs that one task with its reviewer's findings, in the worktree it already has |
| an interrupted run seems lost | run `run` again; merged tasks are skipped, not re-dispatched |
| every wave has exactly one task | `files` sets overlap across most tasks; the decomposition isn't actually parallel |
| dwarves reimplement already-merged work | `goal.txt` or `files` missing, so the capsule carries no useful status |
| several tasks report "produced no changes" | the decomposition made tasks that weren't independently actionable — use a coarser `--decompose-level`, not more retries |
| `retry` says "produced no new changes" | the dwarf returned a byte-identical diff, so no reviewer was paid to read the same code twice. Escalate with `--dwarf`, or fix the prompt |
| a new file's code never reached QA | fixed: untracked files are diffed against `/dev/null`. A hand-built capture using only `git status --porcelain` shows the reviewer a filename and no content |
| claude dwarf ends "I need your permission to edit" | a permission mode that prompts for Edit. forge uses `acceptEdits` plus an explicit Bash allowance, because `auto` allows shell but prompts for Edit, and `acceptEdits` alone allows Edit but prompts for real shell commands |
| `.forge/` turns up in a review | a diff capture is missing `':(exclude).forge'` |
| memory stays empty | nothing durable was learned — the common, correct case. The ledger still has a row per dispatch |
| a wrong fact keeps reappearing | `memory.md` is rebuilt from the ledger; delete the ledger rows, not the memory line |
| dwarves build incompatible interfaces | the seam was never agreed — plan the approach, or use `--planner` |

For per-harness invocation details, effort ladders and known CLI failure modes, see
[`references/harnesses.md`](references/harnesses.md). For decomposition mechanics, see
[`references/decompose.md`](references/decompose.md). For memory internals, see
[`references/memory.md`](references/memory.md).

---

## Repository layout

```
forge/
├── SKILL.md                     the skill itself — what the orchestrating model reads
├── registry.tsv                 alias → (harness, model, effort). Add a row to teach forge a model
├── README.md                    this file
├── scripts/
│   ├── forge-dispatch.sh        resolve a spec → run one role on one harness
│   ├── forge-solo.sh            the whole single-task run: dwarf → diff → qa
│   ├── forge-parallel.sh        plan / run / retry / integrate for decomposed runs
│   ├── forge-memory.sh          inject / note / record / spend project memory
│   └── forge-install.sh         install into all five harnesses
└── references/
    ├── harnesses.md             per-harness invocation, ladders, failure modes
    ├── decompose.md             tasks.tsv schema, difficulty criteria, wave algorithm
    └── memory.md                FORGE_LEARNING grammar, promotion and pruning rules
```
