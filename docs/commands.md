# Command reference

[← README](../README.md) · [All docs](README.md)

Every flag of the `/forge` slash command, the `forge` command, and the scripts the skill runs underneath.

## The `forge` command

npm puts `forge` on your `PATH`; in a git checkout it is `./forge`. Runs start from your agent
harness (`/forge`); this command installs forge and drives its optional tools.

| Command | Does |
|---|---|
| `forge install [--dry-run]` | link forge into each agent harness on this machine; see [install](install.md) |
| `forge update` | fast-forward a git checkout and refresh every harness installation; on an npm install it names the npm command instead. See [updating](install.md#updating-all-harness-installations) |
| `forge version [--check]` | the installed version, and whether a newer release is out |
| `forge fractal …` | opt-in Fractal execution; see [the command group](fractal.md#forge-fractal-command-group) |
| `forge runs [--repo DIR \| --all] [--limit N] [--json]` | this repo's recent runs, newest first: run id, kind, age, task states, verification and final review, goal, and the run dir. Read-only; the same summaries forge-ui shows |
| `forge control show\|set\|pin\|unpin\|drain\|resume\|stop\|retry\|add PLAN_DIR …` | change a live plan: `set` takes `qa-threshold` (`P0`..`P3`, `none` or `default` for the plan, `inherit` per task), `timeout`, `escalate-after`, `escalate-effort` (`on`/`off`), `verify-retries`, `infra-backoff` (a backoff shape; `none` drops it), `pre-review-tests` (`on`/`off`), `pre-review-parallel` (1-64), `retry-budget`, `infra-retries` or `max-parallel` (1-64) (the last three only while a scheduler runs; `--task ID` for `qa-threshold` and `retry-budget`, value `inherit` drops the override); `pin PLAN_DIR TASK SPEC` sets the model the task's next full retry uses once, `unpin` drops it. While a scheduler runs: `drain` starts nothing new until `resume` (a run with nothing left to start ends as usual); `stop [--now]` ends the run once the running tasks finish (`--now` interrupts them; `run` exits 130 without verifying, unless every task had merged by then: it then verifies and exits 0 as usual); `retry PLAN_DIR TASK [--dwarf SPEC]` and `add PLAN_DIR TASK` (a row appended to `tasks.tsv`) queue a request the scheduler applies or refuses. `show` (`--json` for JSON) also says whether the run is drained or stopping and lists its infrastructure pauses. Every change and every request's outcome is logged in `control.log`. Exit 2 invalid input, 3 refused. See [live controls](decomposed-runs.md#changing-a-run-while-it-works) |
| `forge config show [--repo DIR] [--json]` / `forge config set <key> <value> [--repo DIR] [--json]` / `forge config unset <key> [--repo DIR] [--json]` | one view of every config source (registry, the legacy Jev file, decide, the repo's `.forge/verify`, `setup` and `verify-subset`, dataset, `FORGE_*` variables with secrets hidden); `set` / `unset` edit `repo.verify`, `repo.setup`, `repo.verify-subset` (must contain `{files}` or `{changed}`; `--repo` required) and `decide.gate.<gate>`, `decide.gate.<gate>.provider`, `decide.gate.<gate>.model` (`off`, `shadow` or `on`; those write the user level and do not need `--repo`). Unset of a `decide.gate.*` key is refused: set the mode to `off`. `-` as a value reads stdin for the per-repo files. Exit 2 on invalid input |
| `forge runs rm RUN_DIR [--dry-run] [--yes] [--no-archive] [--worktrees] [--branches] [--json]` | delete a run directory but keep what it taught the dataset; see [below](#forge-runs-rm) |
| `forge issues RUN [--apply] …` | file the review findings a run left open as GitHub issues; see [below](#forge-issues) |
| `forge ui open\|status\|stop` | forge-ui, the run viewer; see [the command group](forge-ui.md#forge-ui-command-group) |
| `forge dataset …` | every run's inputs and outcomes; see [run dataset](dataset.md) |
| `forge decide …` | decision-model gates, providers, budgets and the privacy floor; see [below](#forge-decide) |
| `forge jev …` | deprecated alias of the old decision-model commands, kept for one release; see [below](#forge-decide) |
| `forge --help` | list the commands; `forge <command> --help` shows each one's options |

### `forge runs rm`

Deletes a run directory (a plan dir or a solo run dir) without losing its traces. It holds the
run's own lock from the first step to the last (`schedule.lock` of a plan, `pipeline.lock` of a
solo run), so a run started meanwhile fails to start instead of racing the delete. In order:

1. **preserve**: a strict dataset import of the run; every event must be stored, or nothing is deleted;
2. **archive**: a self-describing `.tar.gz` under `<data home>/archive/` (`--no-archive` skips it);
3. **intent**: the dataset event `run_delete_started`;
4. `--worktrees` removes the run's clean worktrees; `--branches` runs `git branch -D` on its
   branches that no worktree has checked out;
5. the run directory is removed, `run_deleted` is recorded and the run leaves the registry.

What counts as the run's is decided from the run id, not read from the run directory: a worktree
under `<repo>/../.forge-worktrees/<run id>/` on `forge/<run id>/<task>` or
`forge/<run id>-integration`. Any other worktree there is `foreign`, one with uncommitted changes
is `dirty`; neither is touched, and a dirty one keeps the run directory only when `--worktrees`
was asked. Branches are reported
`merged` or `UNMERGED`, so you see what deleting them would lose. `--dry-run` changes nothing and
reports; `--yes` skips the prompt for the run id (needed without a terminal); `--json` prints one
object, on failure too. Exit 0 ok, 2 refused input, 3 busy or the run is active, 4 failed during
deletion or the run directory was kept (the same command finishes the job).

### `forge issues`

`forge issues <plan dir, task dir, review dir, known_issues.md or qa.last>` drafts one GitHub
issue per finding the run left open (`--group task|run` for fewer) and files them only with
`--apply`. The findings are the CONFIRMED ones the threshold tolerated (origin
`tolerated`), the CONFIRMED ones a person accepted with `accept` (origin `accepted`, with
their own issue text: a person accepted the work over the reviewer's verdict), PLAUSIBLE
ones with `--include-plausible`, and what the whole-run review left open. A whole-run
review the integration branch has moved past is outdated: the issue says so and names the
tasks that merged after it. Each issue carries a hidden
fingerprint, so a finding is never filed twice, by this command or by gh-proman. Also:
`--min-severity`, `--repo`, `--lang`, `--label`, `--confirm-new`, `--max-issues` (default 10; pass
the size of a `--only` selection).

| Flag | Does |
|---|---|
| `--json` | machine-readable output; `findings` lists each one with its fingerprint, severity, origin (`tolerated`, `accepted`, `review` or `plausible`), task, `review`, `swept` (`{task, finding}` when the run's finding sweep fixed it; such a finding is never drafted), `filed` state and `dismissed` mark. A whole-run finding's `review` is `{name, outdated, merged_since}` (`outdated` is null when the review has no verdict or the plan has no accepted integration commit; `merged_since` is empty unless the review is outdated). Every other finding has `review` null |
| `--offline` | never run `gh`: no filed state, commit publication judged from local refs |
| `--only FP` | keep only these 12-hex fingerprints (repeatable or comma-separated) |
| `--all-runs` | read `RUN` as a repository (or a plan dir naming one) and collect every run in its registry; the newest plan wins for a finding in several |
| `--dismiss FP`, `--undismiss FP`, `--reason TEXT` | mark findings as needing no work (or take it back), then exit; no network. The marks live outside the repo, in `${XDG_STATE_HOME:-~/.local/state}/forge/findings/<repo key>.json` |
| `--include-dismissed` | draft dismissed findings too |

Two filers for one repository at once would both see a finding as unfiled, so `--apply` takes a
repository lock from the issue lookup to the last `gh issue create`, and each dismissal takes it
around its write. A busy lock is retried for 10 s, then exit 3. Exit 0 ok, 2 usage or a `gh` or
dismissal-store error, 3 busy.

### `forge decide`

Per-gate decision models. The setup — precedence, what each gate does, budgets, the privacy
floor and migration — is in [Decision models](../references/decision-models.md#setup). How a
gate is measured before it turns on (labels, `report`, `replay`, the promotion rule and the
request cache) is in [Measuring a gate](../references/decision-models.md#measuring-a-gate).

| Command | Does |
|---|---|
| `status [--repo DIR] [--json]` | each gate's effective mode and which level set it, providers (`present` or `missing` key), caps and today's spend, privacy floor. A gate that is `on` without a passing replay is shown as `forced`. Never writes |
| `gate GATE off\|shadow\|on [--force] [--provider P] [--model M] [--profile NAME \| --repo DIR]` | set one gate. Gates: `drift`, `forward`, `prompt`, `memory-curate`, `memory-slice`, `memory-stale`, `verify-discover`. With no `--provider`, a gate uses the legacy transport (`jev.json`'s endpoint with `TYPESAFE_API_KEY` or `jev.json`'s key, or Cloudflare for a site `jev.json` maps there); name a provider to choose where requests go. Without `--repo`, writes the user file and migrates `jev.json` first. `--profile` writes that profile in the user file. `on` needs a passing replay for the provider and model it resolves to, else it is refused (exit 3). `--force` turns it on anyway, and it shows as forced (see [measuring a gate](../references/decision-models.md#promotion)) |
| `provider add jev\|clef --name N [--url U] [--account-id A] [--model M] [--context N] [--price X] [--deadline S] [--local] [--key-stdin \| --key-env VAR]` | add a hosted or local provider. A key is never an argument: one line on stdin or a terminal prompt is written to `keys/<N>.key` mode `0600`, or `--key-env VAR` stores the variable name and reads it at request time |
| `provider list [--json]` | configured providers |
| `provider rm N` | remove a provider no gate uses, and the `keys/<N>.key` it wrote |
| `budget [--run tokens=N,usd=X] [--daily tokens=N,usd=X] [--repo DIR]` | show the caps and today's spend, or set them. `--daily` is user-level; a repo file takes only `--run`. `none` clears a cap |
| `privacy never-send\|allow --repo DIR` | set or remove that repo's privacy floor. `never-send` turns every gate off for that repo and beats every other level |
| `doctor [--live] [--json] [--repo DIR]` | stale settings. Exit 1 on a problem. `--live` probes each provider unless the privacy floor is on |
| `migrate [--force] [--json]` | copy `jev.json` into the user `decide.json`, once. `--force` replaces that file from `jev.json` again |
| `report [--runs N] [--run DIR]... [--site S] [--all] [--json]` | per site, over the newest N runs of this repo (default 10; `--run` names directories, `--all` reads every repo): calls, cache hits, errors, skips, tokens, cost, and how often the gate fired and was right on the free labels. Read-only, asks no model. See [measuring a gate](../references/decision-models.md#forge-decide-report) |
| `replay --site S --provider P --model M [--runs N] [--send] [--workers 3] [--seed 7] [--margin 0.02] [--json]` | re-asks the site's labelled requests on provider P and model M. Without `--send` it is a dry run that sends nothing; with it, it asks the unanswered requests, judges the answers on the newer half of the runs, and records the result that `gate on` reads. Charged to no budget. See [measuring a gate](../references/decision-models.md#forge-decide-replay) |

`--repo` defaults to `FORGE_DECIDE_REPO`, else the git top level. Exit 0 ok, 2 usage, 1 a
failed doctor check or a config file that cannot be read, 3 a refused `gate on`.
`forge config set` writes the same user-level gate fields as `decide.gate.<gate>`, `decide.gate.<gate>.provider` and
`decide.gate.<gate>.model`.

A write to the user file migrates `jev.json` first: `gate` without `--repo`, `provider add`,
`provider rm`, and `budget` when `--run` or `--daily` changes the user file. `provider list`,
a bare `budget`, `status` and `doctor` do not.

`forge jev` is the deprecated alias, kept for one release. `setup`, `sites`, `enable`,
`disable`, `status` and `doctor` print the `forge decide` command and then run the old one.

Run flags, on `forge-solo.sh` and `forge-parallel.sh plan`, are frozen in `jev-selection.json`.
A re-plan replaces the file. Solo again on the same run dir keeps it. When the file exists,
`run` and `retry` keep it and say so when flags are passed again. A plan with no selection
file takes the flags on `run` or `retry` and freezes them then.

| Flag | Does |
|---|---|
| `--gate NAME=MODE[,NAME=MODE...]` | set those gates `off`, `shadow` or `on` for this run. Repeatable; a later one wins for the same gate |
| `--no-gates` | every gate off: no decision-model request |
| `--decide-budget tokens=N[,usd=X]` | cap this run's decision-model tokens, and optionally USD. Does not change the daily cap |

## The `/forge` slash command

```
/forge "<goal>" --dwarf <alias>[:<effort>[:<harness>]]
                [--qa <alias>[:<effort>[:<harness>]]]
                [--planner <alias>[:<effort>[:<harness>]]]
                [--yolo-dwarf] [--yolo-qa] [--repo <dir>] [--native-review]
                [--no-memory] [--gate NAME=MODE[,...]] [--no-gates]
                [--decide-budget tokens=N[,usd=X]]
                [--timeout <seconds>] [--ui | --no-ui] [--fractal | --no-fractal]
                [--fractal-auto-decompose] [--fractal-planner <spec>]
                [--fractal-depth <n>] [--fractal-children <n>] [--fractal-nodes <n>]
                [--fractal-iterations <n>] [--fractal-concurrency <n>] [--fractal-deadline <s>]
                [--decompose-level low|medium|high] [--max-parallel <n>]
                [--dwarf-high <spec>] [--dwarf-medium <spec>] [--dwarf-low <spec>]
                [--qa-high <spec>] [--qa-medium <spec>] [--qa-low <spec>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--dwarf <spec>` | *asks* | the model that implements the change |
| `--qa <spec>` | `opus` at `high` | the model that reviews the dwarf's diff |
| `--planner <spec>` | orchestrator plans | dispatch planning to a model; one dispatch per run, effort defaults to `xhigh` |
| `--no-memory` | memory on | write and inject nothing in `.forge/` |
| `--gate NAME=MODE` | the configured gates | set decision-model gates for this run (`off`, `shadow` or `on`); repeatable, a later one wins. Frozen in `jev-selection.json`. See [`forge decide`](#forge-decide) |
| `--no-gates` | gates as configured | every decision-model gate off for this run |
| `--decide-budget tokens=N[,usd=X]` | the configured cap | cap this run's decision-model spend |
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
| `--fractal-auto-decompose` | off | enables Fractal with automatic recursive split-or-atomic decisions |
| `--fractal-planner <spec>` | top-level planner, then task root spec | frozen decision model; requires automatic mode |
| `--fractal-depth <n>` | `2` | nested levels below each Forge implementation node |
| `--fractal-children <n>` | `3` | unsettled direct children per node |
| `--fractal-nodes <n>` | `12` | lifetime nodes per Forge task, including implementation |
| `--fractal-iterations <n>` | `6` | per node per execution attempt |
| `--fractal-concurrency <n>` | `3` | shared model slots across nested trees and QA |
| `--fractal-deadline <s>` | `2700` | task attempt deadline, excluding explicit pauses |
| `--fractal-max-cost <amount>` | unsupported | returns an error; the bridge cannot enforce dollar caps |

Fractal's task deadline still applies when `--timeout 0` disables the ordinary
per-dispatch timeout. Child routing uses the dwarf tier flags; QA stays at the Forge
task boundary. Pools and Fractal limits are frozen for an existing run.

Before an interactive run, forge asks **“Use Fractal for this run? [y/N]”** once.
Pass `--fractal` or `--no-fractal` to answer explicitly; unattended runs default off.
It also asks **“Open forge-ui to watch this run? [y/N]”** (`--ui` / `--no-ui`); on a yes,
the [forge-ui](forge-ui.md) page opens as soon as the run exists. Resume and retry never ask.

For what `--native-review` trades away, see [native review](reviews.md#native-review).

### Examples

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

## `scripts/forge-dispatch.sh`

Resolves a spec into a real CLI invocation and runs it. Needs only bash and coreutils, which
is the point: it behaves identically no matter which harness is orchestrating.

```
forge-dispatch.sh doctor
forge-dispatch.sh dwarf   <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
forge-dispatch.sh qa      <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
                                 [--native-review [--review-base <ref>]]
forge-dispatch.sh planner <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--dry-run]
forge-dispatch.sh efforts --spec <spec>
```

`efforts` prints the efforts the spec's pairing accepts, weakest first, one per line (nothing
when the pairing has no effort dial); a literal model id with a harness takes that harness's
whole ladder. It never dispatches and spends no quota. The retry policy's effort step reads it.

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
| `--timeout <s>` | kill this dispatch after `<s>` seconds; `0` disables. See the precedence below |

**Exit codes:** `0` ok · `2` spec or usage error · `3` harness missing or unusable ·
`4` the backend ran and failed · `7` the backend hit its timeout and was killed ·
`8` an **infrastructure failure** (quota or usage limit, auth, rate limit, network, or an
empty answer) — nothing about the work was judged, so callers pause instead of counting a
failure. On `8`, `<run-dir>/<role>.infra` holds `class=`, `retry_after=`, `detail=`, `rc=`,
`role=`, `harness=`, `model=`.

**Timeout precedence.** An explicit `--timeout`, then `FORGE_TIMEOUT` (an explicit `0` wins
and disables the watchdog), then the optional fifth `timeout` column of the model's
[registry](models.md#model-registry) row, then the built-in `2700` (45 min). A value that was *not*
explicit is scaled by the resolved (post-clamp) effort: up to `high` ×1, `xhigh` ×4/3,
`max`/`ultra`/`ultracode` ×2. The planner defaults to `xhigh`, so its built-in limit is 3600 s;
QA defaults to `high` and dwarves to `medium`, and both keep 2700 s. `<role>.resolved` records `timeout=` and
`timeout_source=` (`explicit`, `env`, `registry` or `default`, plus `+effort-scaled`).
`forge-parallel.sh run`/`retry --timeout S` sets it for a decomposed run (kept in the plan).
`FORGE_REGISTRY` points the dispatcher at an alternative registry file.

Only antigravity bounds its own runtime, and stock macOS has no `timeout` binary to wrap the
others in — so a hung backend used to block forever, and under a decomposed run one hung task
silently stalled the others. The default is deliberately generous: this is a backstop against
a wedged process, not a budget. A role killed by it has usually left a partial edit behind, so
`7` is worth reporting as such rather than as a clean failure.

On `3` or `4`, forge shows you the actual error rather than substituting a different model —
quietly swapping backends hides the fact that the one you picked is broken.

**Process hygiene.** Unless `FORGE_ORPHAN_GUARD=off` (or the run is a Fractal one), the harness
is started in its own session, and after it exits any process still alive in that session is
listed in `<role>.orphans` and stopped, so a backgrounded measurement cannot outlive the
dispatch. The timeout watchdog and the exit trap signal the whole session too.

**Log clock.** Beside the log, `<role>.clock` records when it grew (one `<epoch> <bytes>` line for
each second it did), so forge-ui can time the commands of a harness that logs no times (codex,
grok). It never changes a dispatch's outcome; `FORGE_LOG_CLOCK=off` turns it off.

```bash
# what would this actually run?
bash scripts/forge-dispatch.sh dwarf sol:ultra --prompt-file /tmp/p --dry-run

# check a clamp before committing to a long run
bash scripts/forge-dispatch.sh qa gemini-pro:medium --prompt-file /tmp/p --dry-run
# effort=low
# clamped=medium -> low        (gemini-pro offers low and high, but no medium)
```

## `scripts/forge-solo.sh`

The whole single-task run: memory injection, prompt assembly, the dwarf dispatch, the diff
capture, the QA dispatch, and the ledger writes.

```
forge-solo.sh <run-dir> --repo <dir> --dwarf <spec> [--qa <spec>]
              [--approach <file>] [--yolo-dwarf] [--yolo-qa]
              [--native-review] [--no-memory] [--timeout <s>] [--dry-run] [--ui | --no-ui]
              [--gate NAME=MODE[,...]] [--no-gates] [--decide-budget tokens=N[,usd=X]]
              [--fail-first] [--qa-threshold P0|P1|P2|P3|none]
              [--fractal | --no-fractal] [--fractal-<limit> <n>]
              [--dwarf-low <pool>] [--dwarf-medium <pool>] [--dwarf-high <pool>] [--retry]
```

`--qa-threshold` sets the blocking level for this run: `P0`..`P3`, or `none` (P3); the default is `P2`. The gate's decision is in `qa.gate` and the tolerated findings are in `known_issues.md`. See [the verdict rule](reviews.md#verdicts-and-failure).

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

With Fractal, per-step dispatch artifacts live in the managed run while the solo directory
retains the aggregate diff and Forge QA result. `--retry` starts a new Fractal attempt;
`forge fractal resume RUN_ID` continues preserved execution and pipeline checkpoints.

```bash
RUN="$(mktemp -d /tmp/forge-XXXXXX)"
echo "add retry with backoff to the HTTP client" > "$RUN/goal.txt"
echo "Add exponential backoff to src/http.py, capped at 5 attempts." > "$RUN/prompt.md"

bash scripts/forge-solo.sh "$RUN" --repo ~/dev/api --dwarf sol:high --qa opus
```

**Exit codes:** `0` reviewed · `2` usage · `3` not a git repository · `4` a dispatch failed ·
`5` **no changes or review invalidated** · `7` a dispatch timed out · `8` a dispatch hit an
**infrastructure failure** (quota, auth, rate limit, network, empty output): nothing was
judged, fix the cause and run again. A dwarf that exits 0 with an empty answer *and* changes
nothing is reported as `8`, not `5` — it never really ran.

The dwarf is given the same implementer rules as in a decomposed run (no commits, no
background processes, finish measurements before the last message). If it commits anyway, the
run still reviews its changes, says so, and prints the `git reset --soft <old HEAD>` that would
turn them back into uncommitted changes; forge never resets anything itself. Orphan processes
and promised-later-work notes appear under "implementer guard notes".

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

## `scripts/forge-parallel.sh`

Decomposed runs: many dwarves in isolated worktrees, per-task QA, only passing work merged.
Bash 3.2 entrypoints use a Python standard-library coordinator to schedule eligible tasks.

```
forge-parallel.sh plan      <plan-dir> --repo <dir> [routing flags] [--planner <spec>] [--no-memory]
                                     [--gate NAME=MODE[,...]] [--no-gates] [--decide-budget tokens=N[,usd=X]]
                                     [--decompose-level L] [--shadow-qa <spec>[,..]|none] [--shadow-rate R]
                                     [--reviewer-session | --no-reviewer-session]
                                     [--implementer-session | --no-implementer-session]
                                     [--explore E] [--ui | --no-ui] [--fail-first | --no-fail-first]
                                     [--pre-review-tests | --no-pre-review-tests] [--pre-review-parallel N]
                                     [--qa-threshold P0|P1|P2|P3|none|default]
forge-parallel.sh run       <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa] [--dry-run]
                                     [--verify <cmd> | --verify-lane 'name[opts]=cmd' ...]
                                     [--verify-parallel N] [--setup <cmd>] [--timeout S]
                                     [--retry-failed N] [--infra-retries M] [--infra-backoff SHAPE]
                                     [--infra-fallback | --no-infra-fallback]
                                     [--qa-threshold P0|P1|P2|P3|none|default] [--verify-retries N]
                                     [--escalate-after N] [--escalate-effort | --no-escalate-effort]
                                     [--sweep P0|P1|P2|P3 [--sweep-plausible] [--sweep-max N]]
                                     [--sweep-ahead | --no-sweep-ahead] [--attribute | --no-attribute]
                                     [--fractal | --no-fractal] [--fractal-<limit> <n>]
forge-parallel.sh retry     <plan-dir> <task-id> [--dwarf <spec>] [--answer <text> | --answer-file <file>]
                                     [--timeout S]
                                     [--qa-threshold P0|P1|P2|P3|none|default] [--verify-retries N]
                                     [--verify <cmd> | --verify-lane 'name[opts]=cmd' ...] [--verify-parallel N]
                                     [--escalate-after N] [--escalate-effort | --no-escalate-effort]
forge-parallel.sh accept    <plan-dir> <task-id> --reason "<why>" --approved
forge-parallel.sh review    <plan-dir> [--qa <spec>] [--yolo-qa] [--qa-threshold P0|P1|P2|P3|none|default]
forge-parallel.sh attribute <plan-dir> [--force]
forge-parallel.sh integrate <plan-dir> --approved [--final-review <qa-spec>] [--verify-retries N]
                                     [--verify-parallel N]
forge-parallel.sh split     <plan-dir> <new-plan-dir> --tasks <id,id> [--dry-run]
forge-parallel.sh combine   <new-plan-dir> <plan-dir> <plan-dir>... [--verify <cmd>] [--dry-run]
```

| Subcommand | Does |
|---|---|
| `plan` | validate `tasks.tsv`, resolve difficulty → concrete dwarf/qa specs, compute waves, render the approval table |
| `run` | schedule ready tasks; per task: worktree → dwarf → diff → QA → status; merge passing tasks onto the integration branch |
| `retry` | re-run **one** failed task in the worktree it already has, with its reviewer's findings in the prompt |
| `attribute` | after a failed combined verification, search the merge order for **a merge that breaks it**; merges, revises and accepts nothing. See below |
| `integrate` | **never automatic** — merge the integration branch into your branch |
| `accept` | **only on your explicit instruction** — merge a `FAIL`/`UNKNOWN` task's reviewed work anyway, recording why |
| `review` | **opt-in, spends QA quota** — one reviewer reads the whole combined result; read-only |
| `split` / `combine` | move tasks into a plan of their own / join fully merged plans into one |

| Flag | Subcommands | Remembered in the plan | Means |
|---|---|---|---|
| `--gate NAME=MODE[,NAME=MODE...]` | plan | `jev-selection.json` | set decision-model gates for this run; repeatable, a later one wins. When the file exists, `run` and `retry` keep it; with no file they apply the flags passed then and freeze them |
| `--no-gates` | plan | `jev-selection.json` | every decision-model gate off |
| `--decide-budget tokens=N[,usd=X]` | plan | `jev-selection.json` | cap this run's decision-model spend |
| `--answer TEXT` / `--answer-file F` | retry | no — this retry only | the user's answer to a `NEEDS_INPUT` task's question (`tasks/<id>/needs_input.md`), written to `answer.md`; on any other status `retry` exits `2` |
| `--timeout S` | run, retry | `timeout` | per-dispatch timeout in seconds (`0` disables); wins over `FORGE_TIMEOUT` |
| `--retry-failed N` | run | no — this run only | re-queue a task the reviewer rejected up to `N` more times inside the run |
| `--infra-retries M` | run | no — this run only | wait out up to `M` infrastructure stops per paused scope (a model, a harness, or everything) instead of ending the run |
| `--infra-backoff SHAPE` | run | `infra_backoff` | the waits between those stops when the provider gave no retry-after: `exp:B[,C]`, `linear:S,T[,C]` or `list:a,b,...` in seconds (default `exp:60`) |
| `--infra-fallback` | run | `infra_fallback` | a dwarf stopped by a rate limit moves to the next healthy model of its tier pool instead of waiting (off by default; `--no-infra-fallback` turns it off) |
| `--qa-threshold P0\|P1\|P2\|P3\|none\|default` | plan, run, retry, review | `qa_threshold` | the least severe CONFIRMED finding that still blocks (default `P2`); milder ones become known issues; `none` means P3, `default` removes the setting. Solo takes `P0`..`P3` or `none`. See [the verdict rule](reviews.md#verdicts-and-failure) |
| `--verify-retries N` | run, retry, integrate | `verify_retries` | reruns of a failed verify command, or of each failed lane (default `1`, `0` = strict single run) |
| `--verify-lane 'name[opts]=cmd'` | run, retry | `verify.d/<name>` | one verification lane; repeatable. Options as in a lane file, comma-separated (`group=g`, `first`, `retries=n`, `reports=dir`, `baseline=file`). The first one of an invocation replaces the plan's earlier lanes and `--verify`; see [verification lanes](../references/decompose.md#execution-checks-and-integration-verification) |
| `--verify-parallel N` | run, retry, integrate | `verify_parallel` | lanes run at once, 1 to 32 (default `3`) |
| `--escalate-after N` | run, retry | `escalate_after` | failed attempts on one model before the next goes a tier up (default `3`, `0` = off) |
| `--pre-review-tests` | plan | `pre_review_tests` | before each review, run only the tests the attempt affects; a red attempt goes back to its implementer unreviewed (off by default; `--no-pre-review-tests` turns it off). See [pre-review tests](../references/decompose.md#pre-review-tests-opt-in) |
| `--pre-review-parallel N` | plan | `pre_review_parallel` | how many of those checks run at once across tasks, 1 to 64 (default `2`) |
| `--sweep Pn` | run | no — this run only | once every task has merged, fix the open findings at `Pn` or worse in a few extra tasks, one per file, each with a narrow review; the plan goes to `sweep/plan.md`. Once per plan; what it does not fix stays for `forge issues`. See [the finding sweep](reviews.md#fixing-them-in-the-run-the-finding-sweep-opt-in) |
| `--sweep-plausible` | run | no — this run only | the sweep also takes PLAUSIBLE findings of passed tasks |
| `--sweep-max N` | run | no — this run only | at most `N` fix tasks (default `5`) |
| `--sweep-ahead` | run | `sweep_ahead` | a task is also told the open findings in files it owns, to fix on its way (off by default; `--no-sweep-ahead` turns it off) |
| `--attribute` | run | `attribute_on` | when the run's combined verification fails, search the merge order for a merge that breaks it, right after the failure is reported (off by default; `--no-attribute` turns it off). Without it the summary says how to run `attribute` later |
| `--escalate-effort` | run, retry | `escalate_effort` | at the top tier, retry the same model once more at its next accepted effort before stopping (off by default; `--no-escalate-effort` turns it off) |

**Exit codes:** `0` ok · `2` usage or validation · `3` precondition (not a git repo, …) ·
`5` some task failed QA, or the combined verification failed · `6` integration conflict ·
`8` **paused on an infrastructure failure** (quota, auth, rate limit, network): no attempt was
spent and nothing was judged — fix the cause and run the same command again ·
`130` **interrupted or stopped** (a signal, or `forge control stop`): the running tasks are
`INTERRUPTED` and verification was skipped or stopped; the summary prints the `retry` for each.

**`attribute`** answers the question a failed combined verification leaves open: every task passed
its own review, so which merge broke the whole? It verifies *prefixes* of the merge order in a
scratch worktree (`<worktree root>/_attribute`, detached at the prefix commit, after the plan's
setup command): prefix 0 is the integration base, prefix `n` the tip. The tip is known to fail
from the run itself; the search verifies the base and then halves the interval until it has a
merge `k` whose prefix `k − 1` verified PASS and whose prefix `k` verified FAIL. Verification is an
arbitrary command, so failure need not be monotonic over the merges: this is *a* merge that breaks
verification, not necessarily the earliest. Each step runs the verification the run used, frozen:
the lanes in `verification.lanes.d/` or the command in `verification.command`, never a config
changed since and never discovery. A run that recorded neither is inconclusive unless `--force`,
which uses today's source. On this repo one verification takes about 10 minutes, so a search of
`n` merges costs `1 + ⌈log₂ n⌉` of them.

It takes the plan lock like `retry`, so a second `attribute`, or one started while a run holds the
plan, is refused. It refuses (exit `2`) a plan whose `verification.status` is not `FAIL` and one
whose `verification.commit` is not `accepted.integration`, the commit being searched; `--force`
skips both checks and verifies the tip itself (a passing tip is inconclusive). A step that ends
`UNVERIFIED`, is interrupted or whose setup fails ends the search inconclusive with no culprit.

It writes `attribution.json` (`culprit`, `base_fails`, `inconclusive`, `merges`, `steps` of
`{prefix, commit, status, seconds, source}`, `verification_runs`, `gate`), each step's output in
`attribute/<prefix>-<sha>/`, the failing log's last 200 lines in `tasks/<culprit>/verification.md`,
and an `attribution` event in the [run dataset](dataset.md). It prints one line,
`integration failure: merge <id> breaks verification (prefix k−1 passes, k fails; M verification
runs)`, or the variant `the base already fails verification` or `attribution inconclusive — <reason>`.
Exit `0` when the search finished, culprit or not, `1` when a step could not run at all.

**`NEEDS_INPUT`** is a task status that is not a failure: the implementer ended with a question
only the user can answer, kept in `tasks/<id>/needs_input.md`. The run summary lists it under
`waiting for your answer:` with the first line of the question and the command to answer it, and
the retry hint does not count it as failed. Its dependents end `BLOCKED`, and neither a later
`run`, `--retry-failed` nor `forge control retry` resumes it. Only
`retry <plan> <id> --answer "<text>"` (or `--answer-file F`) does: the question and the answer
join the findings of the next attempt, a normal new attempt. Without an answer, or with one older
than the question, `retry` exits `2` and changes nothing; if escalation refuses the retry, the
question and the answer stay for the same `--answer` to be tried again.

The full plan → run → integrate walkthrough is in [decomposed runs](decomposed-runs.md#plan-run-integrate). `retry`, `accept` and `review` are covered in [reviews, retries and recovery](reviews.md); resuming, `split`/`combine`, scope drift and `tasks.tsv` in [decomposed runs](decomposed-runs.md).

## `scripts/forge-verify.py`

The verification lane runner `run`, `retry` and `integrate` use when lanes are declared. You can
run it by hand to check or try a lanes directory:

```
forge-verify.py lanes --lanes-dir DIR [--checkout DIR] [--json]
forge-verify.py run   --checkout DIR --out DIR --lanes-dir DIR [--parallel N] [--retries N]
                      [--fingerprint-cmd CMD]
```

`lanes` lists the lanes with their options and checks them (with `--checkout`, also that
`reports=` and `baseline=` stay inside it and the baseline exists). `run` runs them in the
checkout as a forge run does and writes `verification.lanes.json`, `verification.runs.tsv` and
the per-lane logs into `--out` (`--parallel` default 3, `--retries` default 1).

**Exit codes:** `0` every lane passed and the tree is unchanged · `1` a lane failed, or the tree
changed · `2` configuration error (nothing ran) · `130` interrupted.

## `scripts/forge-memory.sh` and `scripts/forge-install.sh`

See [project memory](memory.md#scriptsforge-memorysh) and [install](install.md#scriptsforge-installsh).
