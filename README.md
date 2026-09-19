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

With optional [Fractal execution](#fractal-execution), an implementer can delegate bounded
child tasks inside its owned paths. Forge still chooses the models, reviews the combined
diff, verifies the result and controls integration. A local dashboard shows the task tree,
logs and separate execution/acceptance states; HTML reports capture them for later review.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Fractal execution](#fractal-execution)
- [The spec: `alias:effort:harness`](#the-spec-aliaseffortharness)
- [Effort, ceilings and clamping](#effort-ceilings-and-clamping)
- [Yolo mode](#yolo-mode)
- [Decomposed runs](#decomposed-runs)
- [Planning](#planning)
- [Project memory](#project-memory)
- [Jev advisory judgments](#jev-advisory-judgments)
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

The installer also offers optional Fractal support in an interactive terminal. Declining
or a Fractal installation failure does not prevent ordinary Forge setup. See
[Fractal installation](#install-the-optional-runtime) for prerequisites and a separate install.

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

### Updating all harness installations

Run the updater from your Forge checkout (or through one of its skill symlinks):

```bash
bash ~/.claude/skills/forge/scripts/forge-update.sh
```

It fetches the current branch's configured upstream, fast-forwards the checkout, and
runs Forge's installer to refresh Claude, OpenClaude and Codex links, OpenCode discovery,
and Antigravity's copied plugin. Harnesses that are not present are skipped. Restart
active harness sessions afterward to load the updated skill.

```bash
# Preview without fetching or changing files:
bash <forge-checkout>/scripts/forge-update.sh --dry-run

# Refresh harness installations after local edits, without fetching:
bash <forge-checkout>/scripts/forge-update.sh --local

# Point a copied installation's updater at the original checkout:
bash <path>/scripts/forge-update.sh --source <forge-checkout>
```

The default update requires a clean Git checkout and a branch with an upstream. Local
changes, detached HEAD, fetch failures and diverged branches stop the update without
resetting or stashing your work. `--local` deliberately allows current local edits.
Real skill directories are preserved; a conflicting directory or plugin installation
failure produces a nonzero exit and an explanation. After fixing an installation
failure, use `--local` to retry from the updated source. Updating Forge does not prompt
for optional Ripwire installation.

---

## Quick start

Inside any installed harness, invoke the skill:

```
/forge "add retry with exponential backoff to the HTTP client" --dwarf sol:high
```

That's the whole minimum. `sol` builds it at `high` effort on Codex, `opus` reviews the
resulting diff at `xhigh` (the default reviewer), and you get back a summary of the change
plus qa's findings.
Before an interactive run, Forge asks **“Use Fractal for this run? [y/N]”** once.
Pass `--fractal` or `--no-fractal` to answer explicitly; unattended runs default off.

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

## Fractal execution

Fractal is an optional implementation backend for both solo and decomposed runs. It uses
the existing Forge dispatcher for Codex, Claude, OpenClaude, OpenCode and Antigravity.
Each work step gets a fresh model invocation; preserved files and child results provide
continuity when a parent resumes. Independent QA reviews the complete Forge task diff.

`--decompose-level` plans top-level Forge tasks before execution. `--fractal` lets an
implementer request nested children during execution. They can be used together, and
solo runs can use Fractal without a top-level decomposition.

### Install the optional runtime

From the Forge checkout:

```bash
./forge fractal install --dry-run
./forge fractal install --yes --with-prerequisites
./forge fractal doctor --spec sol --spec opus --json
```

The managed runtime pins **Fractal 1.2.0** at revision
`18793200c0d7e8cdb2db369ea3abe5647a1e15e4`, checks the source archive checksum,
and includes the required `wiki` executable. It prefers an existing Python 3.12–3.14;
with prerequisite installation selected, it can provision uv, Python 3.13 and tmux.
tmux installation uses an existing Homebrew on macOS or apt on Debian/Ubuntu.
The [installation reference](references/fractal.md#installation-and-provenance) covers
consent, supported bootstrap platforms, provenance and failure diagnostics.

Installation never enables Fractal for a run. `./forge` is the repository-local launcher;
it does not replace a command already named `forge` or change your shell startup files.
Doctor checks the runtime and advertised harness capabilities without calling a provider.

### Start and inspect a run

Through the skill:

```text
/forge "add retry with exponential backoff to the HTTP client" --dwarf sol:high --fractal
```

Or prepare a solo run directory outside the product repository and invoke the runner:

```bash
FORGE_RUN="$HOME/.local/state/forge/examples/http-client"
mkdir -p "$FORGE_RUN"
printf '%s\n' 'Add bounded exponential backoff to the HTTP client and run its tests.' > "$FORGE_RUN/prompt.md"
bash scripts/forge-solo.sh "$FORGE_RUN" --repo /absolute/path/to/product \
  --dwarf sol:high --qa opus --fractal --fractal-concurrency 3
```

Choose a new run directory for unrelated work. Backend selection, routing pools and limits
are stored with the run; resume and explicit retry retain them. For a prepared, approved
decomposed plan, select the backend on `run`:

```bash
bash scripts/forge-parallel.sh run /absolute/path/to/plan --fractal --dry-run
bash scripts/forge-parallel.sh run /absolute/path/to/plan --fractal
```

Dry runs show resolved model pools, limits and proposed workspaces without installation
or provider calls. When a selected runtime is unavailable, interactive runs offer
installation; unattended runs return an actionable error instead of changing backends.

Discover the managed run ID, then inspect it from another terminal:

```bash
./forge fractal runs --repo /absolute/path/to/product --json
./forge fractal open                         # live runs index
./forge fractal status RUN_ID --json
./forge fractal tree RUN_ID --json
./forge fractal logs RUN_ID --follow
./forge fractal report RUN_ID --html /absolute/path/to/report.html
```

`RUN_ID` is the `run-…` identifier returned by `runs`, not the solo directory or plan name.
The dashboard refreshes every two seconds, loads logs on demand and offers copyable CLI
controls. Browser access is read-only, bound to `127.0.0.1` with an ephemeral access token.
Closing its server does not stop execution. Portable HTML embeds captured data and logs,
shows capture time and missing data, and needs no Fractal runtime to read.

### Bounds, recovery and acceptance

Defaults per Forge task are two nesting levels below implementation, three unsettled
direct children per node, twelve lifetime nodes, six iterations per node per attempt,
and a 45-minute attempt deadline excluding explicit pauses. Three model slots are shared
across the whole Forge run, including QA. `--max-parallel` bounds top-level task pipelines;
`--fractal-concurrency` bounds model invocations across their nested trees.

Child difficulty selects a frozen `--dwarf-low`, `--dwarf-medium` or `--dwarf-high` pool,
falling back to the configured general dwarf, then the parent. Dependencies and owned
paths constrain admission; overlapping ownership is serialized. Fractal never implies
`--yolo-dwarf`. Costs are observational; unavailable usage stays unknown and dollar-cap
requests are rejected.

```bash
./forge fractal pause RUN_ID
./forge fractal resume RUN_ID
./forge fractal stop RUN_ID
```

Pause and stop preserve work and diagnostics. Run-level resume recovers recorded execution
and the Forge pipeline without repeating accepted stages. Use explicit runner retry for
a new attempt, retaining work and the original Forge QA baseline. Controls also accept
`--task TASK_ID` and `--task TASK_ID --node NODE_ID`; those IDs come from `tree` output.

Managed data lives in `${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/`. Solo execution
uses an isolated snapshot and imports changes only after checking source drift, preserving
staging and leaving changes uncommitted. Decomposed candidates return to Forge's existing
review/integration pipeline. **Fractal completion is not Forge acceptance**: QA, repository
verification and authorization to update the user's branch remain distinct.

See the [full Fractal reference](references/fractal.md) for limits, child requests and
recovery details, and [verification evidence](tests/fractal-verification.md) for the tested
platforms and browser checks. No paid-provider or speed/cost claim follows from fake-CLI tests.

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

Waves are a planning preview. A Python 3 standard-library coordinator starts eligible tasks
as prerequisites merge and capacity becomes available, while excluding overlapping active
paths. Merges are serialized. Each task pins the integration commit at dispatch; retries keep
that baseline, and QA sees only baseline membership recorded for that task.

### Fresh session per task, plus a capsule

Every dispatch is a **clean slate** — forge never passes `--continue`, `--resume`, or
`codex exec resume`. One task's confused turn can't poison another, and each task's context
stays small.

Clean slates alone would leave every dwarf blind to the wider run, so a short `capsule.md` is
generated from the task's pinned baseline and preserved for both implementation and QA:

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
tokens     medium  THIS ONE     src/auth/token.py
docs       low     NOT IN BASE  README.md

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

## Jev advisory judgments

Optional, **off by default**, and advisory: [Jev](https://docs.typesafe.ai) is TypeSafe's
"System One" model. It answers narrow typed questions (a yes/no probability, one choice
from a set, a position on a rubric) over supplied state in about 100 ms, with calibrated
confidence. Forge asks it a second opinion at the points where it already has to judge
something, and keeps every existing decision and gate. Jev never picks a model you did
not authorise, never overturns a `FORGE_VERDICT`, and never marks anything verified.

```bash
./forge jev setup            # store an API key (mode 0600), validate it, enable Jev
./forge jev status           # what is on
./forge jev doctor --live    # config checks, plus one probe request
```

```
/forge "<goal>" --decompose-level medium --dwarf sol --jev            # advisory lines in the plan
/forge "<goal>" --decompose-level medium --dwarf sol --jev --jev-shadow   # record only, print nothing
```

What each capability does when it is on:

| Capability | Where | Says |
|---|---|---|
| `routing` | `plan` | a difficulty tier per task from five rubrics composed in code; the effort it would size the dwarf at. Both advisory until `forge jev calibrate --write` has enough outcomes from **this repo** |
| `gates` | `plan`, QA | a prompt too vague for QA to judge; undeclared files a task will probably edit; two same-wave tasks that share an interface despite disjoint `files`; on a `FAIL`, whether the review names a real bug and cites code that is in the diff |
| `tests` | verification | which existing command runs the suite when neither `--verify` nor `.forge/verify` exists; one re-run of a failure that looks like a flake **and** matches a recorded trap; which tests a diff probably breaks, run early in a throwaway export (`.forge/verify-subset`) |
| `memory` | `record`, `inject`, `prune` | whether a learning is durable enough to inject; a paraphrase of an existing fact; the ~8 facts relevant to this task; a fact whose file still exists but which went false |

Every request is one row in `<run-dir>/jev.jsonl`; a skipped one is named in `jev.skip`.
Any failure, timeout or missing key is a skip and the run continues exactly as it would
without Jev. `forge jev scope <run-dir>` reads those logs back and reports what each
rubric returned and the spread across repeated identical requests, which is the noise floor
any threshold has to clear.

**Privacy.** Each enabled capability sends a narrow slice of your repository to
`api.typesafe.ai`: task prompts, diff hunks, file excerpts, test names, memory entries.
`.forge/` and `.git` are never sent. That is why it is off until you turn it on.
`FORGE_JEV=off` is a hard kill switch that wins over every flag.

The [Jev reference](references/jev.md) records every threshold, the measurement behind
it, and the judgments that are known not to separate yet.

## Full command reference

### The `/forge` slash command

```
/forge "<goal>" --dwarf <alias>[:<effort>[:<harness>]]
                [--qa <alias>[:<effort>[:<harness>]]]
                [--planner <alias>[:<effort>[:<harness>]]]
                [--yolo-dwarf] [--yolo-qa] [--repo <dir>] [--native-review]
                [--no-memory] [--timeout <seconds>] [--fractal | --no-fractal]
                [--jev | --no-jev] [--jev-act] [--jev-shadow] [--jev-<cap> | --no-jev-<cap>]
                [--fractal-depth <n>] [--fractal-children <n>] [--fractal-nodes <n>]
                [--fractal-iterations <n>] [--fractal-concurrency <n>] [--fractal-deadline <s>]
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
| `--native-review` | off | use native Codex review against the recorded baseline instead of an inlined diff |
| `--decompose-level <l>` | off | split the goal into parallel tasks |
| `--max-parallel <n>` | `3` | concurrent task pipelines |
| `--dwarf-high/-medium/-low` | — | per-difficulty routing; comma-list pools models |
| `--qa-high/-medium/-low` | — | same, for reviewers |
| `--fractal` / `--no-fractal` | ask interactively; off unattended | mutually exclusive, per-run backend choice |
| `--fractal-depth <n>` | `2` | nested levels below each Forge implementation node |
| `--fractal-children <n>` | `3` | unsettled direct children per node |
| `--fractal-nodes <n>` | `12` | lifetime nodes per Forge task, including implementation |
| `--fractal-iterations <n>` | `6` | per node per execution attempt |
| `--fractal-concurrency <n>` | `3` | shared model slots across nested trees and QA |
| `--fractal-deadline <s>` | `2700` | task attempt deadline, excluding explicit pauses |
| `--fractal-max-cost <amount>` | unsupported | returns an error; the bridge cannot enforce dollar caps |
| `--jev` / `--no-jev` | off | ask Jev for advisory judgments, when a key is configured |
| `--jev-act` | off | let a calibrated judgment change a task's difficulty tier; inert until `forge jev calibrate --write` |
| `--jev-shadow` | off | record judgments and print nothing |
| `--jev-<cap>` / `--no-jev-<cap>` | — | per-capability allowlist: `routing`, `tests`, `gates`, `memory` |

Fractal's task deadline still applies when `--timeout 0` disables the ordinary
per-dispatch timeout. Child routing uses the dwarf tier flags; QA stays at the Forge
task boundary. Pools and Fractal limits are frozen for an existing run.

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

### `forge fractal` command group

Use `./forge fractal` from this checkout, or `<forge-checkout>/forge fractal` elsewhere.
This group manages Fractal runs; it does not replace the `/forge` skill or runner scripts.

| Command | Purpose |
| --- | --- |
| `install [--dry-run] [--yes] [--with-prerequisites]` | preview or install the isolated pinned runtime |
| `doctor [--spec SPEC] [--json]` | check runtime/hooks/tmux and optional repeated harness specs without provider calls |
| `runs [--repo DIR] [--json] [--html PATH]` | discover managed runs, optionally filtered by repository |
| `status\|tree\|activity RUN_ID` | inspect progress, hierarchy and history |
| `logs\|costs\|messages\|config RUN_ID` | inspect diagnostics, observed accounting, radio traffic and effective settings |
| `pause\|resume\|stop RUN_ID` | request a control and record its observed state |
| `open [RUN_ID] [--port PORT]` | open the read-only live dashboard or runs index |
| `report RUN_ID [--html PATH]` | capture a self-contained HTML report; default filename is `RUN_ID.html` |

Inspection commands accept `--task TASK_ID`, `--node NODE_ID`, `--json`, `--html PATH`,
`--offset N` and `--limit N` (1–1000). They currently return a common JSON run projection;
the command names do not imply separate output schemas. `logs --follow` follows changes.
HTML exports capture full history and logs. Controls require an explicit run ID, and a
node control also requires its task ID; they never guess the latest run. Use each command's
`--help` for its accepted options. Installation is also available through
`bash scripts/forge-install-fractal.sh`.

### `forge jev` command group

Use `./forge jev` from this checkout. Every subcommand works with no key configured and no
network; only `setup`, `doctor --live`, `backtest --execute` and the runners' own advisory
calls send a request.

| Command | Purpose |
| --- | --- |
| `setup [--key K] [--yes]` | store a key at `~/.config/forge/jev.json`, probe it, enable Jev |
| `status [--json]` \| `doctor [--live]` | resolved config and enabled capabilities; `--live` adds one probe request |
| `enable` \| `disable [--capability NAME]` | persist the switch, optionally for one capability |
| `backtest --repo DIR [--capability tests\|drift] [--execute]` | score a rubric against git co-change history; `--execute` is required before any request |
| `calibrate --repo DIR [--write]` | join shadow routing scores to QA outcomes; `--write` is the only thing that lets `--jev-act` route |
| `scope <run-or-plan-dir>... [--json]` | what each rubric returned across recorded runs, and the repeat noise floor |
| `parse-spec "<sentence>"` | map a sentence onto `--dwarf-<tier>` flags over the aliases in `registry.tsv`; prints, never applies |

`score-plan`, `coupling`, `verify-discover`, `verify-triage`, `test-subset`, `qa-effort`,
`review-triage` and the `memory-*` subcommands are called by the runners, not by hand.

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
              [--fractal | --no-fractal] [--fractal-<limit> <n>]
              [--dwarf-low <pool>] [--dwarf-medium <pool>] [--dwarf-high <pool>] [--retry]
              [--jev | --no-jev] [--jev-act] [--jev-shadow] [--jev-<cap> | --no-jev-<cap>]
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
| `fractal-selection.json` | persisted backend choice; enabled runs include their managed run ID |
| `jev-selection.json`, `jev.jsonl`, `qa.jev.json` | the frozen Jev choice, one row per request, and an annotation of a `FAIL` that looks unsound (never a changed verdict) |

With Fractal, per-step dispatch artifacts live in the managed run while the solo directory
retains the aggregate diff and Forge QA result. `--retry` starts a new Fractal attempt;
`./forge fractal resume RUN_ID` continues preserved execution and pipeline checkpoints.

```bash
RUN="$(mktemp -d /tmp/forge-XXXXXX)"
echo "add retry with backoff to the HTTP client" > "$RUN/goal.txt"
echo "Add exponential backoff to src/http.py, capped at 5 attempts." > "$RUN/prompt.md"

bash scripts/forge-solo.sh "$RUN" --repo ~/dev/api --dwarf sol:high --qa opus
```

**Exit codes:** `0` reviewed · `2` usage · `3` not a git repository · `4` a dispatch failed ·
`5` **no changes or review invalidated** · `7` a dispatch timed out.

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
Bash 3.2 entrypoints use a Python standard-library coordinator to schedule eligible tasks.

```
forge-parallel.sh plan      <plan-dir> --repo <dir> [routing flags] [--planner <spec>] [--no-memory]
forge-parallel.sh run       <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa] [--dry-run]
                                     [--fractal | --no-fractal] [--fractal-<limit> <n>]
forge-parallel.sh retry     <plan-dir> <task-id> [--dwarf <spec>]
forge-parallel.sh integrate <plan-dir> --approved
```

| Subcommand | Does |
|---|---|
| `plan` | validate `tasks.tsv`, resolve difficulty → concrete dwarf/qa specs, compute waves, render the approval table |
| `run` | schedule ready tasks; per task: worktree → dwarf → diff → QA → status; merge passing tasks onto the integration branch |
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
For Fractal execution, pass tier pools to `plan` and backend/limit flags to `run`.
`--max-parallel` limits task pipelines; the shared `--fractal-concurrency` limit includes
all their child invocations and QA. See [decomposed Fractal execution](references/decompose.md#fractal-within-decomposed-tasks).

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

Run `run` again to skip MERGED tasks, merge saved PASS results after fingerprint checks,
and dispatch eligible pending tasks. Failed and interrupted tasks need an explicit `retry`.
Their worktrees and pinned baselines remain available. Concurrent run/retry/integrate
operations for the same plan are excluded by an advisory lock.

For a Fractal-backed run, use `./forge fractal resume RUN_ID` to recover recorded execution
and pipeline checkpoints. A task/subtree selector resumes only that execution scope.
Explicit `retry` retains the backend and frozen pools; a replacement dwarf must already
belong to that pool. See [recovery](references/fractal.md#limits-and-lifecycle).

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

Fractal adds a managed workspace alongside the runner artifacts described here:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/
  runtime/                       isolated pinned Fractal and wiki environment
  provenance.json                installed versions and source checksum
  installs/                      installation attempts and diagnostics
  runs/<run-id>/run.json          effective routing, limits and runner binding
  runs/<run-id>/controls/         requested controls and observed results
  runs/<run-id>/tasks/<task-id>/
    request.json / state.json     task request and execution checkpoint
    initialization.json         control repository and Fractal ledger provenance
    control/                     Fractal metadata, separate from product changes
    nodes/<node-id>/product/      isolated product candidate
    nodes/<node-id>/steps/        fresh dispatcher attempts and raw logs
    outcome.json                 captured Forge acceptance and verification
```

The original solo/plan directory stores `fractal-selection.json`; decomposed plans also
store `fractal-routing.json`. Prefer `./forge fractal runs`, `tree`, `logs` and `report`
to direct ledger access. Inspection and stop never delete managed work.

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

- Solo runs leave source changes uncommitted. Forge creates private review commits, but
  does not commit on the solo source branch, push, or reset the user tree.
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
- QA runs in a disposable repository. Mutations to the source or review snapshot invalidate
  acceptance. Disabled editing tools alone do not prevent writes through Bash.

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
| Fractal was installed but a new unattended run uses ordinary execution | installation never activates it; pass `--fractal` explicitly |
| `Fractal selected but unavailable` | run `./forge fractal doctor`, then the suggested managed install; resume retains the selected backend |
| existing run rejects a new backend, pool or limit | selection and bounds are recorded; create a new run directory for changed settings |
| Fractal reports completion but Forge has not accepted the work | inspect QA, verdict and verification; execution completion does not establish acceptance |
| Fractal import stops with `source_drift` | the source changed after its snapshot; preserve both versions and inspect the candidate instead of forcing import |
| Fractal work was paused or its coordinator exited | inspect `status` and `logs`, then use explicit `resume RUN_ID`; retry starts a new attempt |
| browser dashboard closed while work is running | execution is independent; reopen with `./forge fractal open RUN_ID` |
| costs are unknown or a dollar cap is rejected | the bridge only reports available usage; v1 cannot enforce a spending cap |

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
├── forge                        repository-local `forge fractal` / `forge jev` launcher
├── scripts/
│   ├── forge-dispatch.sh        resolve a spec → run one role on one harness
│   ├── forge-solo.sh            the whole single-task run: dwarf → diff → qa
│   ├── forge-parallel.sh        plan / run / retry / integrate for decomposed runs
│   ├── forge-memory.sh          inject / note / record / spend project memory
│   ├── forge-fractal.py         Fractal CLI and internal adapter entrypoint
│   ├── forge-fractal-options.sh runner selection, limit flags and help
│   ├── forge-fractal-dispatch.sh bridge entrypoint for existing pipelines
│   ├── forge-install-fractal.sh isolated optional runtime installer
│   ├── forge_fractal/           nested execution, recovery, inspection and dashboard
│   ├── forge-jev.py             `forge jev` entrypoint
│   ├── forge-jev-options.sh     runner flags, precedence and frozen selection
│   ├── forge_jev/               opt-in TypeSafe/Jev client, rubrics, gates, calibration
│   └── forge-install.sh         install into all five harnesses
└── references/
    ├── harnesses.md             per-harness invocation, ladders, failure modes
    ├── decompose.md             tasks.tsv schema, difficulty criteria, wave algorithm
    ├── fractal.md               opt-in backend, limits, recovery and observability
    ├── jev.md                   advisory judgments, every threshold and its measurement
    └── memory.md                FORGE_LEARNING grammar, promotion and pruning rules
```

## Execution integrity and offline checks

Forge now captures solo changes against a recorded starting snapshot, including staged edits,
without altering the user's index. Existing work is recorded separately in `existing.diff`.
QA receives the full implementation requirements and reviews a disposable repository; source
or snapshot changes invalidate the result. Only the recorded reviewed task commit can merge.

Parallel execution rechecks dependencies before each task and retry. Failed prerequisites leave
consumers BLOCKED. Directory and child file declarations are serialized.

`forge-dispatch.sh doctor --spec sol --role dwarf` validates a selected model recipe using local
CLI help. Solo and parallel runners preflight the whole selected pipeline before implementation.
These checks do not establish authentication, billing or live model availability.

Use `forge-parallel.sh run <plan> --verify 'your check command'` or put the command in
`.forge/verify` to verify the combined result. Output and exit status are saved with the plan.
Integration checks a candidate containing the current user branch before updating it. A missing
command is reported as UNVERIFIED; a failing configured check blocks integration.

Run `bash tests/check.sh` for offline regression checks. See [tests](tests/README.md) for skill
invocation scenarios and verification limits. CI covers macOS and Linux.


## Runtime efficiency and measurement

Shell entrypoints require Python 3 (standard library only) in addition to Bash and Git.
The [runtime helper decision](references/runtime.md) records the module boundaries and tradeoffs.
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
have incomplete timings.

Prompt bytes are separate from native input, cached-input and output token counts. Codex
uses `turn.completed` usage; Claude uses result usage (input includes uncached, cache-read
and cache-creation tokens). Unsupported or absent usage stays null, never estimated from
bytes. See [measurement and live evaluation](tests/README.md).

Measured results, including the live comparison's higher total token use and latency, are
recorded in [the September 7 evaluation](tests/benchmark-2026-09-07.md).

## Ripwire context

Forge automatically adds optional Ripwire 0.4.0 repository evidence to planner, dwarf,
and QA prompts across all five harnesses. Setup and interactive runners offer a pinned,
verified installation; headless runs continue when it is missing. Use `--no-ripwire`
or `FORGE_RIPWIRE=off` to disable it. Decomposed plans preserve opt-out across resumes
and retries. See [installation, controls and attempt artifacts](references/ripwire.md).
