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
| `forge control show\|set\|pin\|unpin\|drain\|resume\|stop\|retry\|add PLAN_DIR …` | change a live plan: `set` takes `qa-threshold` (`P0`..`P3`, `none` or `default` for the plan, `inherit` per task), `timeout`, `escalate-after`, `escalate-effort` (`on`/`off`), `verify-retries`, `infra-backoff` (a backoff shape; `none` drops it), `infra-fallback` (`on`/`off`), `max-attempts` (a non-negative integer, `0` = no cap, and only with `--confirm`), `converge-window` (a non-negative integer), `pre-review-tests` (`on`/`off`), `pre-review-parallel` (1-64), `retry-budget`, `infra-retries` or `max-parallel` (1-64) (the last three only while a scheduler runs; `--task ID` for `qa-threshold` and `retry-budget`, value `inherit` drops the override); `pin PLAN_DIR TASK SPEC` sets the model the task's next full retry uses once, `unpin` drops it. While a scheduler runs: `drain` starts nothing new until `resume` (a run with nothing left to start ends as usual); `stop [--now]` ends the run once the running tasks finish (`--now` interrupts them; `run` exits 130 without verifying, unless every task had merged by then: it then verifies and exits 0 as usual); `retry PLAN_DIR TASK [--dwarf SPEC]` queues a retry, and a `NEEDS_DECISION` task instead needs `--decide retry=N` or `--decide threshold=Pn`, or `--dwarf SPEC`, each with `--reason TEXT --approved`; `add PLAN_DIR TASK` (a row appended to `tasks.tsv`) queues a request the scheduler applies or refuses. `show` (`--json` for JSON) also says whether the run is drained or stopping, prints an `INFRA` task's cause, and, while a scheduler is live, lists each infrastructure pause with its reset and "needs you". The text table lists each task's attempts against its cap (`9/8`, or `9/-` when there is no cap). Every change and every request's outcome is logged in `control.log`. Exit 2 invalid input, 3 refused. See [live controls](decomposed-runs.md#changing-a-run-while-it-works) |
| `forge config show [--repo DIR] [--json]` / `forge config set <key> <value> [--repo DIR] [--json]` / `forge config unset <key> [--repo DIR] [--json]` | one view of every config source (registry, the legacy Jev file, decide, the repo's `.forge/verify`, `setup` and `verify-subset`, dataset, `FORGE_*` variables with secrets hidden); `set` / `unset` edit `repo.verify`, `repo.setup`, `repo.verify-subset` (must contain `{files}` or `{changed}`; `--repo` required) and `decide.gate.<gate>`, `decide.gate.<gate>.provider`, `decide.gate.<gate>.model` (`off`, `shadow` or `on`; those write the user level and do not need `--repo`). Unset of a `decide.gate.*` key is refused: set the mode to `off`. `-` as a value reads stdin for the per-repo files. Exit 2 on invalid input |
| `forge config profile list\|show\|create\|edit\|set\|unset\|rm\|use` | named bundles of routing, policy, gates and budget in `decide.json`; see [below](#forge-config-profile) |
| `forge watch [RUN_DIR \| --repo DIR \| --all] [--interval S] [--settle S] [--until-done] [--no-notify] [--exec CMD]` | follow runs live: a line for every task status change, and a desktop notification, `--exec CMD` and the repo's `.forge/on-finish` when a run finishes or fails, a task fails or a final review gives its verdict; see [below](#forge-watch). Read-only |
| `forge runs rm RUN_DIR [--dry-run] [--yes] [--no-archive] [--worktrees] [--branches] [--json]` | delete a run directory but keep what it taught the dataset; see [below](#forge-runs-rm) |
| `forge runs rerun RUN_DIR --dwarf SPEC [--qa SPEC] [--to DIR] [--max-parallel N] [--dry-run] [--run --yes]` | set up the same plan again with another implementer, from the same starting commit; spends nothing unless `--run --yes`; never merges. See [below](#forge-runs-rerun-and-compare) |
| `forge runs compare RUN_A RUN_B [--json] [--no-record]` | two runs of one plan, side by side, per task; records the pair in the dataset |
| `forge issues RUN [--apply] …` | file the review findings a run left open as GitHub issues; see [below](#forge-issues) |
| `forge pr [RUN_DIR \| --repo DIR] [--texts] [--paths] [--out FILE] [--create [--base B] [--head B]]` | a draft pull-request description for a finished run, on stdout; `--create` opens it as a draft PR with `gh`; see [below](#forge-pr) |
| `forge ui open\|status\|stop` | forge-ui, the run viewer; see [the command group](forge-ui.md#forge-ui-command-group) |
| `forge dataset …` | every run's inputs and outcomes; see [run dataset](dataset.md) |
| `forge decide …` | decision-model gates, providers, budgets and the privacy floor; see [below](#forge-decide) |
| `forge jev …` | deprecated alias of the old decision-model commands, kept for one release; see [below](#forge-decide) |
| `forge --help` | list the commands; `forge <command> --help` shows each one's options |

### `forge watch`

Follows runs from the terminal, for when forge-ui is not open or you are away from it.

```
forge watch                       # every run registered for this repo, new ones too; Ctrl-C stops
forge watch RUN_DIR               # one run, until it ends: exit 0 done, 1 failed, 3 its process looks dead
forge watch --all --until-done    # every repo's runs; stop once nothing is running
```

Every task status change prints a line. These events also raise a notification and run the hooks:
a run finishing (`run-done`) or ending with failures (`run-failed`), a task newly failing
(`task-failed`) and a final review's `PASS`/`FAIL` (`review`). They are the events forge-ui's bell
reports, and the runs are read with forge-ui's own reader. The first look at each run is only a
baseline, so a run that was already over, or had not changed for two hours, says nothing. A run
that stops is announced once it has stayed stopped for `--settle` seconds (10), so a task that
fails and is retried automatically is not reported as a failed run (the `task-failed` event still is).

- **Desktop notification.** `notify-send` on Linux, `osascript` on macOS. With no notifier on the `PATH`, or on Linux
  with no desktop session (`DISPLAY`, `WAYLAND_DISPLAY` and `DBUS_SESSION_BUS_ADDRESS` all unset: a
  server, a container, CI), forge says so once on stderr and the events only print. `--no-notify` or
  `FORGE_WATCH_NOTIFY=off` turns notifications off.
- **`--exec CMD`** runs `CMD` through the shell on every event. **`.forge/on-finish`** in the run's repo
  runs on `run-done` and `run-failed` only, with the repo as its working directory.
- **The environment** of both: `FORGE_WATCH_EVENT`, `FORGE_WATCH_RUN_ID`, `FORGE_WATCH_RUN_DIR`,
  `FORGE_WATCH_REPO`, `FORGE_WATCH_GOAL`, `FORGE_WATCH_TASK` (a `task-failed` event), `FORGE_WATCH_VERDICT`
  (the review's verdict, the failing task's status, else the run's final review or verification status, else
  `PASS`/`FAIL`) and `FORGE_WATCH_MESSAGE`. A hook's output goes to stderr; one that fails or runs
  past 30 seconds is reported there and never stops the watch.
- **`.forge/on-finish` is run only when it is safe.** `.forge/` is usually committed, so a hook that arrived with
  a cloned repository would run code the first time you watch. It runs only when it is executable, owned by you,
  not writable by others, and not tracked by git; otherwise forge says why on stderr and skips it. A hook you want
  to share goes in your own checkout untracked, or is passed with `--exec`.

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

### `forge runs rerun` and `forge runs compare`

`forge runs rerun <run-dir> --dwarf SPEC` answers "would this plan have gone better on another
model?" with the same tasks, prompts and options. It copies the plan into a new directory
(`<run-dir>-<spec>`, or `--to`), puts SPEC on every task (`--qa` changes the reviewer too; by
default each task keeps its own, so one thing changes), and prepares it the way `forge-parallel.sh
plan` does. The new run's integration branch `forge/<new id>-integration` starts at the commit the
original run started from (`integration.base`), not at wherever your branch is now, so both runs
see the same code. Exploratory routing is off for it (`--explore 0`), or some tasks would run on
a third model.

It spends nothing: it prints a warning that a second run costs a second run's tokens, what the
original run recorded for these tasks, the [plan forecast](dataset.md) for the new model when the
dataset has history, and the command that starts it. `--run --yes` starts it (`--run` alone is
refused, so the cost is read first); `--dry-run` creates nothing at all. It never merges: the
rerun only writes its own `forge/<new id>…` branches and worktrees, and your branch, the original
run and its branches are not touched. A plan that has a task still `RUNNING`, a solo run (no task
table), a plan that never started, and a Fractal-backed plan are refused, and a rerun that fails to
set up is removed again. Exit 0 ok, 2 bad input, 3 refused.

`forge runs compare <run-dir-A> <run-dir-B>` sets two runs of one repository side by side per
task: status, attempts, tokens and time (from the dataset, when the runs are in it) and the lines
added and deleted on the task's branch. It writes the pair as one `comparison` event to the dataset
(`--no-record` skips that; the same pair twice is one event; a run that has not attempted a task
yet is shown but not recorded). `--json` prints everything. One pair of runs is one sample: a task
that passed on one model and failed on the other may have just been the model's day, so read it
as what happened, not as which model is better. Nothing routes on it.

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

### `forge pr`

Writes the pull-request description a finished run deserves, from what the run recorded, so the PR
that proposes its integration branch says what was built and what was not.

```
forge pr                                  # the newest finished run of this repo, on stdout
forge pr RUN_DIR --out pr.md              # a given run, also written to a file
forge pr | gh pr create --draft --body-file - --title "…"   # your own gh call
forge pr --create --base main             # gh pr create --draft, on forge/<run id>-integration
```

The body has the goal, a table of the tasks (status, difficulty, implementer, reviewer), the tasks that
did not merge, the verification status and the final review's verdict (marked when tasks merged after
it), the final review's open findings (severity, location and headline, worst first) and the integration
branch with the commit it forked from. The suggested title (the goal's first line) goes to stderr.
A run that still has running or pending tasks is refused; solo runs have no tasks or integration branch
to describe and are refused too.

- **Private text stays out**, as in `forge ui export`: the request, the prompts and the deferrals
  (`--texts` adds the request and the deferrals) and local paths, which are cut to their last component
  (`--paths` keeps them). The goal, task titles and finding headlines are kept: read the body before it
  goes anywhere public.
- **`--create` is the only thing that does more than print.** It runs `gh pr create --draft` in the
  repo, for `--head` (default `forge/<run id>-integration`) and `--base` (default gh's). It never pushes:
  the branch must already have an upstream (`git push -u origin <branch>`) or forge says so and stops.
  Without `--create` neither `gh` nor the network is used.

### `forge decide`

Per-gate decision models. The setup — precedence, what each gate does, budgets, the privacy
floor and migration — is in [Decision models](../references/decision-models.md#setup). How a
gate is measured before it turns on (labels, `report`, `replay`, the promotion rule and the
request cache) is in [Measuring a gate](../references/decision-models.md#measuring-a-gate).

| Command | Does |
|---|---|
| `status [--repo DIR] [--json]` | each gate's effective mode and which level set it, providers (`present` or `missing` key), caps and today's spend, privacy floor. A gate that is `on` without a passing replay is shown as `forced`. Never writes |
| `gate GATE off\|shadow\|on [--force] [--provider P] [--model M] [--profile NAME \| --repo DIR]` | set one gate. Gates: `drift`, `forward`, `prompt`, `coverage`, `retry-addressed`, `dwarf-message`, `attribution`, `findings`, `memory-curate`, `memory-slice`, `memory-stale`, `verify-discover` (what each does when `on`: [Gates and modes](../references/decision-models.md#gates-and-modes)). With no `--provider`, a gate uses the legacy transport (`jev.json`'s endpoint with `TYPESAFE_API_KEY` or `jev.json`'s key, or Cloudflare for a site `jev.json` maps there); name a provider to choose where requests go. Without `--repo`, writes the user file and migrates `jev.json` first. `--profile` writes that profile in the user file. `on` needs a passing replay for the provider and model it resolves to, else it is refused (exit 3). `--force` turns it on anyway, and it shows as forced (see [measuring a gate](../references/decision-models.md#promotion)) |
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

### `forge config profile`

A profile is a named bundle of routing, policy, gates and budget, stored in `decide.json`.
One name resolves user, then repo, then built-in: a user profile hides a repo profile of
the same name, which hides a built-in. The built-ins `balanced` and `lite` are read-only
and carry the [recommended routing](models.md#recommended-routing). `forge config profile
show balanced` prints `balanced`; `lite` is the same routing with the high tier on
`sonnet:high` for both dwarf and reviewer.

```
forge config profile list [--repo DIR] [--json]
forge config profile show NAME [--repo DIR] [--json]
forge config profile create NAME (--from OTHER | --from-run PLAN | --file F) [--repo DIR] [--json]
forge config profile edit NAME --file F [--repo DIR] [--json]
forge config profile set NAME KEY VALUE [--repo DIR] [--json]
forge config profile unset NAME KEY [--repo DIR] [--json]
forge config profile rm NAME [--repo DIR] [--json]
forge config profile use NAME [--repo DIR] [--json]
forge config profile use --none [--repo DIR] [--json]
```

Without `--repo` the command reads and writes the user file,
`${XDG_CONFIG_HOME:-~/.config}/forge/decide.json`, and `list` and `show` see that file plus
the built-ins. `--repo DIR` uses `<repo>/.forge/decide.json`. A repo-only profile is not
visible, and cannot be copied, without `--repo`.

The effective default is the user file's `default_profile`, else the repo file's. A user
default hides a repo default even when `--repo` is passed. `use --none` clears
`default_profile` only in the file that command writes, so clearing the user default
reveals a repo default.

A name is 1–64 characters of letters, digits, `.`, `_` or `-`, and starts with a letter or
a digit.

| Subcommand | Does |
|---|---|
| `list` | one line per winning profile: name, level (`user`, `repo` or `builtin`), `default` when it is the effective default, and `shadows` the levels it hides. `--json` is `{default: {name, level}, profiles: [{name, level, shadowed, default}]}`; `name` and `level` are null when no default is set |
| `show NAME` | the winning body, its level, what it shadows, and whether it is the default. `--json` adds `body` |
| `create NAME` | exactly one of `--from OTHER` (a copy of a profile `show` can see), `--from-run PLAN` (routing and policy taken from that plan's files, with a frozen `profile.json` filling policy keys no file has), or `--file F` (`-` reads stdin). The body must be at most 1 MiB. Refuses a name that already exists at this level |
| `edit NAME --file F` | replace the body stored at this level. `-` reads stdin |
| `set NAME KEY VALUE` | set one dotted routing or policy key. `routing.escalate_effort` is `on` or `off`. Pools, `policy.shadow_qa` and `policy.fast` are comma lists |
| `unset NAME KEY` | remove that key. An empty section is dropped |
| `rm NAME` | delete the profile stored at this level |
| `use NAME` | write `default_profile` at this level, after the body validates |
| `use --none` | clear `default_profile` at this level |

`set` and `unset` accept `routing.dwarf.<tier>`, `routing.qa.<tier>` (`tier` is `any`,
`low`, `medium` or `high`), `routing.planner`, `routing.fractal_planner`,
`routing.final_reviewer`, `routing.escalate_after`, `routing.escalate_effort`,
`policy.qa_threshold`, `policy.retry_failed`, `policy.infra_retries`,
`policy.verify_retries`, `policy.timeout`, `policy.explore`, `policy.shadow_rate`,
`policy.shadow_qa` and `policy.fast`. Gates and budget are not dotted keys: put them in
the JSON body with `create` or `edit`. Unknown keys inside `routing` or `policy` are
refused. Unknown top-level keys of a body are kept and ignored.

A built-in with no copy of its own cannot be edited, set, unset or removed:

```
built-in profile 'NAME' is read-only; copy it: forge config profile create NAME --from NAME
```

A user or repo copy of that name is what those commands change. `edit`, `set`, `unset` and
`rm` without `--repo` change the user file only.

On success, `create`, `edit`, `set`, `unset`, `rm` and `use` print `profile NAME: ACTION (LEVEL)`.
`use --none` prints `profile none: cleared (LEVEL)`. With `--json` that is
`{"ok": true, "name", "level", "body"}` (`name` is null for `use --none`). On error,
`--json` also prints `{"ok": false, "error": "..."}`. Exit 2 on
invalid input, including a read-only built-in. This is not `forge decide gate --profile`,
which writes that profile's gates and refuses `gate on` with exit 3.

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
                [--profile <name> | --no-profile] [--fast <roles>]
                [--yolo-dwarf] [--yolo-qa] [--repo <dir>] [--native-review]
                [--no-memory] [--gate NAME=MODE[,...]] [--no-gates]
                [--decide-budget tokens=N[,usd=X]]
                [--timeout <seconds>] [--ui | --no-ui] [--fractal | --no-fractal]
                [--fractal-auto-decompose] [--fractal-planner <spec>]
                [--fractal-depth <n>] [--fractal-children <n>] [--fractal-nodes <n>]
                [--fractal-iterations <n>] [--fractal-concurrency <n>] [--fractal-deadline <s>]
                [--decompose-level low|medium|high] [--collapse-chains]
                [--plan auto|always|never] [--max-parallel <n>]
                [--dwarf-high <spec>] [--dwarf-medium <spec>] [--dwarf-low <spec>]
                [--qa-high <spec>] [--qa-medium <spec>] [--qa-low <spec>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--dwarf <spec>` | *asks* | the model that implements the change |
| `--qa <spec>` | `opus` at `high` | the model that reviews the dwarf's diff |
| `--planner <spec>` | orchestrator plans | dispatch planning to a model; one dispatch per run, effort defaults to `xhigh`. A decomposed run calls [`forge-parallel.sh auto`](#forge-parallelsh-auto) |
| `--profile <name>` | the default profile, if one is set and the run has no frozen copy | that profile's routing and policy. Mutually exclusive with `--no-profile`. See [profiles on a run](#profile-and---no-profile) |
| `--no-profile` | — | use no profile, and remove a frozen copy |
| `--fast <roles>` | off | fast service tier for those roles. Opt-in. See [below](#the-fast-tier) |
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
| `--collapse-chains` | off; on inside `auto` unless `--no-collapse` | this `plan` only: merge a sequential chain into one task. See [below](#collapse-chains) |
| `--plan auto\|always\|never` | `auto`, and only on `forge-parallel.sh auto` | whether the pre-plan gate plans or writes one task. See [`auto`](#forge-parallelsh-auto) |
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
forge-dispatch.sh doctor --spec <spec> [--role dwarf|qa|planner] [--service-tier fast]
forge-dispatch.sh dwarf   <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
forge-dispatch.sh qa      <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
                                 [--native-review [--review-base <ref>]]
forge-dispatch.sh planner <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--dry-run]
forge-dispatch.sh efforts --spec <spec>
```

`efforts` prints the efforts the spec's pairing accepts, weakest first, one per line (nothing
when the pairing has no effort dial); a literal model id with a harness takes that harness's
whole ladder. It never dispatches and spends no quota. The retry policy's effort step reads it.

**Slow-effort note.** `doctor --spec` may also print one line on stderr, `forge: note: …`.
It is not part of the resolved block, a plain dispatch does not print it, and it never
changes the spec or the exit code. `plan` prints the same note under the table, once per
distinct dwarf or qa spec (`forge-dataset.py slow-notes`). The dataset supplies the note
when it is on and both this effort and a lower effort of the same model have at least five
first attempts: the nearest lower effort this one is at least twice as slow as, with a
first-try pass rate no better. A nearer pair with enough data that fails the rule does not
hide a farther pair that meets it. When some pair has enough data and none meet the rule,
there is no note: measured pairs override the built-in row. Otherwise the built-in row is
`haiku:max` against `haiku:xhigh` on claude's model (#86, 2026-10-28). openclaude's haiku is
that same model, so the row matches it; opencode's haiku does not. The line is
`alias:effort: no measured gain over alias:lower (first try k/n vs k/n) at about Nx the time (m vs m min; source). The spec is unchanged.`

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
| `--service-tier fast` | ask for the fast tier on this one invocation. `doctor` preflights it the same way. `fast` is the only value. codex and claude only; any other harness exits 3 before a model call. See [the fast service tier](../references/harnesses.md#fast-service-tier) |

**Exit codes:** `0` ok · `2` spec or usage error · `3` harness missing or unusable ·
`4` the backend ran and failed · `7` the backend hit its timeout and was killed (unless it had
already given its final result, below) ·
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

**A final result the CLI stays open after.** claude and openclaude stay up while a background
task their session started is still open, even after the model's final result. When the log
ends in that result (only background-task events after it) and has been silent for
`FORGE_RESULT_GRACE` seconds (default 1800; `0` turns it off), forge ends the harness and the
result stands: the role finished, its exit is the one the CLI gives for that result, and
`<role>.settled` says `ended_by=grace`. The same holds when the watchdog is what ended it
(`ended_by=timeout`), so the work goes to review instead of counting as a timeout. A result is
not always the end: a background task that completes wakes the session for another turn, and a
turn under way is never ended by the grace, however long it is silent. The grace is long
because that wake-up can come many minutes later. `<role>.resolved` records `result_grace=`.

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
              [--fast dwarf|qa|none] [--profile NAME | --no-profile]
              [--fractal | --no-fractal] [--fractal-<limit> <n>]
              [--dwarf-low <pool>] [--dwarf-medium <pool>] [--dwarf-high <pool>] [--retry]
```

`--profile` and `--fast` are the same flags as on `plan`, with two solo limits: `--fast`
accepts only `dwarf`, `qa` or `none` (`planner`, `review` and `all` are refused), and the
profile's dwarf and qa are its medium pool, else its `any` pool. See
[profiles on a run](#profile-and---no-profile) and [the fast tier](#the-fast-tier).

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
                                     [--profile NAME | --no-profile] [--fast ROLES] [--collapse-chains]
                                     [--max-attempts N] [--converge-window N] [--no-infra-fallback]
forge-parallel.sh auto      <plan-dir> --repo <dir> --goal-file <file> [--planner <spec>]
                                     [--decompose-level L] [--plan auto|always|never]
                                     [--one-task-difficulty low|medium|high]
                                     [--no-collapse] [--approved] [plan flags...] [-- run flags...]
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
                                     [--decide retry=N|threshold=Pn] [--reason "<why>" --approved]
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
| `auto` | from a goal file to that table in one blocking command; with `--approved`, also `run`. The orchestrator's job is mechanical, so a small model can drive it. See [below](#forge-parallelsh-auto) |
| `run` | schedule ready tasks; per task: worktree → dwarf → diff → QA → status; merge passing tasks onto the integration branch |
| `retry` | re-run **one** failed task in the worktree it already has, with its reviewer's findings in the prompt; with `--answer` or `--answer-file`, answer a `NEEDS_INPUT` task and run it; with `--decide` or `--dwarf` plus `--reason` and `--approved`, decide a `NEEDS_DECISION` task and run it |
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
| `--infra-fallback` | run | `infra_fallback` | turn lateral recovery on: a stop whose cause moves sideways goes to another model of the same tier (the dwarf pool, or a QA peer from `qa-routing.json`), never a stronger one. A new `plan` writes `on`; this flag writes `on` for a later `run` |
| `--no-infra-fallback` | plan, run | `infra_fallback` | turn lateral recovery off. A plan with no file stays off, and a re-plan keeps the setting it has |
| `--converge-window N` | plan | `converge_window` | reviews the non-converging signal looks at (default `4` when the file is absent; below `2` it never fires). Changing it later is `forge control set converge-window N` |
| `--max-attempts N` | plan | `max_attempts` | attempts a task may spend across runs before it stops for a decision (default `8` when the file is absent; `0` means no cap). Changing it later is `forge control set max-attempts N --confirm` |
| `--decide retry=N` / `--decide threshold=Pn` | retry | no — this retry only | resume a `NEEDS_DECISION` task: `N` more attempts (`N` from 1 to 20) or this task's blocking threshold (`P0`..`P3`) and then one retry. Needs `--reason` and `--approved`. Refused on any other status |
| `--qa-threshold P0\|P1\|P2\|P3\|none\|default` | plan, run, retry, review | `qa_threshold` | the least severe CONFIRMED finding that still blocks (default `P2`); milder ones become known issues; `none` means P3, `default` removes the setting. Solo takes `P0`..`P3` or `none`. See [the verdict rule](reviews.md#verdicts-and-failure) |
| `--verify-retries N` | run, retry, integrate | `verify_retries` | reruns of a failed verify command, or of each failed lane (default `1`, `0` = strict single run); with `.forge/verify-subset`, a failed command's failing test files run alone first and confirm a regression without a full rerun |
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
| `--profile NAME` | plan | `profile.json` | that profile's routing and policy, frozen at `plan`. A flag of this call replaces just that value. Mutually exclusive with `--no-profile`. See [below](#profile-and---no-profile) |
| `--no-profile` | plan | removes `profile.json` | no profile. `routed.json` stays |
| `--fast ROLES` | plan | `fast` | which of `dwarf`, `qa`, `planner`, `review` ask for the fast tier (`all` or `none`). Omitted on a re-plan, the file stays; `none` removes it. See [below](#the-fast-tier) |
| `--collapse-chains` | plan | no — this call only | merge a chain into one task named `combined`, after validation and before routing. A one-task or branching plan is unchanged. An already collapsed plan is not an error. See [below](#collapse-chains) |

**Exit codes:** `0` ok · `2` usage or validation · `3` precondition (not a git repo, …) ·
`4` **`plan` only: the plan gates sent work back** (see below); **`auto`:** the planner failed, its reply was refused, or the gates sent items back a second time ·
`5` some task failed QA, the combined verification failed, or a task is still waiting for an
answer or a decision · `6` integration conflict ·
`8` **paused on an infrastructure failure** (`capacity`, `rate_limit`, `usage_limit`, `budget`,
`auth`, `network`, `provider_down`, `empty` or `cancelled`; an old run may say `quota`): no
attempt was spent and nothing was judged — fix a "needs you" cause, or run the same command again.
**`auto`:** the planner stopped on an infrastructure failure, and nothing was dispatched after it ·
`130` **interrupted or stopped** (a signal, or `forge control stop`): the running tasks are
`INTERRUPTED` and verification was skipped or stopped; the summary prints the `retry` for each.

`auto` also exits `0` when it stops at the approval table (or when the run exits 0), `2` for
its own usage (no planner, a plan dir that already has `tasks.tsv`, a bad flag, an unknown
or invalid profile), and otherwise with the exit of the preflight, of `plan`, or of `run`.
The full list is under [`auto`](#forge-parallelsh-auto).

### Profile and --no-profile

`--profile NAME` and `--no-profile` are mutually exclusive. An unknown or invalid profile
exits 2 before anything is written. They apply on `plan` and on `forge-solo.sh`.

`plan` freezes the chosen profile into `<plan-dir>/profile.json`: `name`, `level` (`user`,
`repo` or `builtin`), `body`, `shadowed` (levels the winning copy hid), `frozen_at` (UTC,
`YYYY-MM-DDTHH:MM:SSZ`) and `overridden` (a sorted list of the dotted keys a flag of this
call replaced). The approval table prints:

```
profile: NAME (LEVEL[, shadows LEVEL]) — flags override: KEY, KEY
```

A flag of this call replaces just that value: the dwarf and qa pools, `--planner`,
`--shadow-qa`, `--shadow-rate`, `--explore`, `--fast` and `--qa-threshold`. What is not
overridden is applied as those flags would have been, including the plan files
`final_qa`, `escalate_after`, `escalate_effort`, `qa_threshold`, `timeout` and
`verify_retries`. QA stays `opus` when the profile names no reviewer pool. Gates and
budget stay in the frozen body and are exported as `FORGE_DECIDE_PROFILE_JSON`; daily
token and USD caps count only when `level` is `user`.

With neither flag, a plan that already has `profile.json` keeps it and does not apply it
again. A plan with no copy gets the default profile, if one is set. The runner does not
select `balanced` by itself. `--no-profile` removes `profile.json`. `routed.json` stays,
so a later profile can still tell a model routing wrote from one written by hand.

`run` and `retry` read three things from the frozen file when this invocation did not pass
the flag: `policy.retry_failed`, `policy.infra_retries`, and `routing.fractal_planner`
(the last only with `--fractal-auto-decompose` and no `--fractal-planner`). A flag of the
invocation wins, and those two retry budgets are not written back. `run`, `retry`,
`integrate`, `accept`, `review`, `split` and `attribute` export the frozen gates and
budget. `integrate` with no `--final-review`, when the profile names
`routing.final_reviewer`, reviews the whole run: the spec already in `final_qa` if that
file has one, else the profile's spec, and prints `final review: SPEC, from the plan's profile`.
The other routing and policy keys were copied into plan files when the profile was frozen.

Solo freezes the same file in the solo run dir. From a profile it takes the dwarf and the
qa at the medium tier, else the `any` tier, plus the QA threshold, the timeout and the
fast roles (`dwarf` and `qa` only) that no flag gave. Fractal tier pools, and
`routing.fractal_planner`, apply only when Fractal is on (the planner only with
`--fractal-auto-decompose` and no `--fractal-planner`). It prints the profile line before
it takes the run lock. It writes `profile.json` only under that lock, not on `--dry-run`,
and only when this call chose a new profile. With neither flag, a later run of the same
directory keeps the frozen copy. `--no-profile` removes it.

### The fast tier

`--fast ROLES` names which roles ask dispatch for `--service-tier fast`. On `plan`,
`ROLES` is a comma list of `dwarf`, `qa`, `planner` and `review`, or `all`, or `none`.
On solo, only `dwarf`, `qa` or `none`: `planner`, `review` and `all` are refused. The
plan or run dir keeps them in `fast`, one role per line, in the order `dwarf`, `qa`,
`planner`, `review`. Omitting the flag on a re-plan keeps the file. `none` removes it.
Dispatch adds `--service-tier fast` only for a listed role. Specs never grow a `+fast`
suffix. `<role>.resolved` gains `service_tier=fast` only when it was asked.

The approval table, when `fast` is non-empty, prints:

```
fast tier: ROLES — billed at a higher rate (codex service_tier=fast; claude fast mode needs extra usage enabled)
```

It is billed at a higher rate. Codex and claude are the only harnesses that take it.
Claude's fast mode needs extra usage enabled; without it the CLI does not refuse, the run
stays at standard speed, exits 0, and forge prints `forge: claude ran at standard speed: fast mode is unavailable (REASON)`.
Codex can do the same when the tier is unsupported (EU data residency for GPT-6 models,
for example): it continues at the default tier, exits 0, and forge prints one note. The
note never changes the exit. openclaude, opencode, antigravity and grok are refused,
exit 3, before anything runs. Fractal dispatches ignore the file; forge prints `fast tier is not applied to Fractal dispatches` once. A whole-run review still asks.
`auto` forwards `--fast` to `plan`, and the planner dispatch uses the tier when `planner`
is a fast role. The per-harness command lines are in
[the fast service tier](../references/harnesses.md#fast-service-tier).

### Collapse chains

`--collapse-chains` is a `plan` flag, this call only, and it is not kept. After the gates
(drift may have added files) and after the task rows are validated, a chain — two or more
tasks, every wave holding one — becomes one task. Routing then fills that task's `-`
columns. A plan whose shape is `one` or `branches` is not changed (`not collapsed: SHAPE`).
A plan that already has `tasks.precollapse.tsv` or `collapsed.json` is not an error. Any
other collapse failure stops `plan`.

The merged task is always id `combined`, deps `-`. Difficulty is the highest of the parts
(`low` < `medium` < `high`). Files are the union. Dwarf and qa are the value every part
shares, or `-` when they differ; routing fills a `-`, and a spec they all already named
is kept. The title is `N parts: FIRST TITLE`, cut at 100 characters. The prompt starts
"This task combines the N parts of a plan that could only run one after another…", then
`## Part i: TITLE (id)` with that part's headings demoted one level, then one
`## Acceptance criteria` with `### Part i: TITLE` and that part's criteria, or
`- (no criteria were written for this part)`. Approaches that exist become
`tasks/combined/approach.md` under `### Part i`.

The original `tasks.tsv` bytes are saved as `tasks.precollapse.tsv`. Each `tasks/<id>/`
moves to `tasks.precollapse/<id>/`. `collapsed.json` is `{from, into: "combined", shape}`
with `from` the ids in wave order and `shape` the `{shape, tasks, waves, widest}` of the
chain. The table announces a collapse only when this call did it:

```
collapsed a sequential plan of N tasks into one (tasks.precollapse.tsv keeps the original)
```

A chain left as a chain prints:

```
this plan runs sequentially: N tasks, at most 1 in parallel; each pays its own implementation and review. One task would run once: re-plan with --collapse-chains
```

`one` and `branches` say nothing. `plan-shape.json` (`shape` `one`, `chain` or `branches`,
plus `tasks`, `waves`, `widest`) is written at the end of `plan`. That write is advisory:
a failure is a note, and the table is still printed. `auto` passes `--collapse-chains`
unless `--no-collapse`.

### `forge-parallel.sh auto`

From a goal to the approval table in one blocking command. The orchestrator's job is
mechanical, so a small model can drive it. A new plan dir only: one that already has
`tasks.tsv` exits 2. The goal is copied verbatim to `<plan-dir>/request.md`, and `auto`
is written to `<plan-dir>/orchestrator`.

```
forge-parallel.sh auto <plan-dir> --repo DIR --goal-file FILE [--planner SPEC]
                      [--decompose-level low|medium|high] [--plan auto|always|never]
                      [--one-task-difficulty low|medium|high] [--no-collapse] [--approved]
                      [plan flags...] [-- run flags...]
```

A `--` splits the command. `auto` consumes `--goal-file`, `--plan`,
`--one-task-difficulty`, `--no-collapse` and `--approved`, and does not pass them to
`plan`. `--repo` starts the plan arguments. `--planner`, and `--decompose-level` when it
was set, are forwarded, as are `--profile`, `--no-profile`, `--fast` and `--no-ripwire`.
Every other flag before `--` goes to `plan`. Flags after `--` are `run` arguments only.
`auto` also adds `--collapse-chains` unless `--no-collapse`.

1. **Gate.** `--plan auto` (the default) runs `forge-planner.py gate`, which prints
   `one-task` when the request is at most 1500 characters and at most 6 requirements,
   otherwise `plan`, and records `plan-gate.json` (`decision`, `signals` of `chars`,
   `requirements` and `paths`, `thresholds`, `rule` `size`). `always` plans. `never` is
   one task. The gate only skips the planner for a small goal: on the 16 plans of
   benchmark #86, size did not predict shape.
2. **One task.** `forge-planner.py one-task --difficulty`, from `--one-task-difficulty`
   (default `medium`). No planner. The row is id `task`, deps `-`, files `.`, dwarf and
   qa `-`, and the title is the request's first line, cut at 80 characters. The prompt is
   the request plus one criterion: every requirement of the request is met, and the tests
   for the files you changed pass.
3. **Planning.** The spec is `--planner`, else `routing.planner` of the profile the plan
   flags select. With neither, exit 2. `doctor` preflights that spec before any model
   call. The planner is dispatched into the plan dir, with the fast tier when `planner`
   is a fast role, and `forge-planner.py parse` reads the reply strictly.
4. **`plan`**, with the forwarded flags and `--collapse-chains` unless `--no-collapse`.
   When `plan` exits 4 and `plan.sendback.md` is non-empty, the planner is re-dispatched
   once with that file appended, and `plan` runs again. A second send-back stops.
5. **Without `--approved`:** exit 0 after printing the exact `run` command. Nothing is
   dispatched after the planner. **With `--approved`** (the user's explicit
   pre-approval): `run` with the flags after `--`, and `auto` exits with `run`'s code.

**Exit codes:** `0` stopped at the approval table, or the run's `0` · `2` usage (no
planner, a plan dir that already has `tasks.tsv`, a bad flag, an unknown or invalid
profile, a gate or one-task input error) · `3` precondition (`die`: not a git repository,
the plan dir could not be created) · `4` the planner failed (its dispatch exited neither
0 nor 8), its reply was refused, or the plan gates sent items back a second time · `8`
the planner stopped on an infrastructure failure, and nothing was dispatched after it ·
otherwise the exit of the preflight, of `plan`, or of `run`.

**`plan` and the plan-time gates.** With the `prompt` or `coverage` gate `on`, `plan` may send work
back and exits `4` before it validates the tasks or prints the table. It writes `plan.sendback.md`,
listing the task prompts a reviewer could not judge, and the requirements of `request.md` (the first
30 with at least four words) that no task's `## Acceptance criteria` covers. Rewrite what the file
lists (a prompt, a task's criteria, or a new task), then run `plan` again. An item goes back at most
once, keyed by its task id (prompts) or its requirement (coverage). What is still flagged from that
list is printed in a `decision-model gates:` block under the table and is not sent back again. An
item the first list did not name, such as a new task whose prompt is flagged, is sent back in its
own round, so the second `plan` can exit `4` again; no item goes back twice. `drift`
and `forward`, when `on`, act without exiting: they add a predicted file to a task's `files`, or
make a task depend on a later one, and the block says what they added. In `shadow` they change
`tasks.tsv` and `waves.tsv` not at all, and their predictions go to `plan-gates.json` as `would_add`.

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

With the `attribution` gate `on` (see [`forge decide`](#forge-decide)), the search first asks the
decision model which merged task most likely broke the run, from the end of the failing log and
each merge's diff. It verifies that task's boundary first, prefix `k − 1` then prefix `k`: a
pass-then-fail pair settles it (`outcome: "confirmed"`), and otherwise the answer narrows the bounds
and the binary search goes on (`"refuted"`). In `shadow` the ranking is recorded and the binary
search alone decides. The gate only chooses which boundary to verify first; it never decides the
result.

It writes `attribution.json` (`culprit`, `base_fails`, `inconclusive`, `merges`, `steps` of
`{prefix, commit, status, seconds, source}`, `verification_runs`, `gate`), each step's output in
`attribute/<prefix>-<sha>/`, the failing log's last 200 lines in `tasks/<culprit>/verification.md`,
and an `attribution` event in the [run dataset](dataset.md). It prints one line,
`integration failure: merge <id> breaks verification (prefix k−1 passes, k fails; M verification
runs)`, or the variant `the base already fails verification` or `attribution inconclusive — <reason>`.
When the gate's first guess is the merge that was confirmed, the line ends `(the decision model's first guess)`.
Exit `0` when the search finished, culprit or not, `1` when a step could not run at all.

**`NEEDS_INPUT`** is a task status that is not a failure: the implementer ended with a question
only the user can answer, kept in `tasks/<id>/needs_input.md`. The run summary lists it under
`waiting for your answer:` with the first line of the question and the command to answer it, and
the retry hint does not count it as failed. Its dependents end `BLOCKED`, and neither a later
`run`, `--retry-failed` nor `forge control retry` resumes it. Only
`retry <plan> <id> --answer "<text>"` (or `--answer-file F`) does: the question and the answer
join the findings of the next attempt, a normal new attempt. Without an answer, or with one older
than the question, `retry` exits `2` and changes nothing; if escalation refuses the retry, the
question and the answer stay for the same `--answer` to be tried again. A run that ends with such a
task unmerged exits `5`, as it does for a task that failed. Only the `dwarf-message` gate, when `on`,
sets this status.

**`NEEDS_DECISION`** is the same kind of stop for a choice rather than a question. The reviews in
the last window all failed and kept finding new problems, or the retry policy has no stronger
model, or the task has spent its attempt cap (`plan --max-attempts N`, default 8 across runs;
`forge control set max-attempts N --confirm` changes it, and `0` means no cap). The reasons and
the `retry` and `accept` commands are in `tasks/<id>/decision.md`. The run summary lists them under
`waiting for your decision:`. Neither a later `run`, `--retry-failed`, a retry budget nor a pin
resumes that same task. Only `retry <plan> <id> --decide retry=N` or `--decide threshold=Pn`, or
`--dwarf <spec>`, each with `--reason "…" --approved`, or `accept`, does. Replacing it is a hand
edit of `tasks.tsv` before the next `run`, as the summary says (`forge control add` queues a new
row but does not replace the stopped one). A live scheduler takes the same flags as `forge control retry`. A run that ends with such a
task still in `tasks.tsv` and unmerged exits `5`. See
[when reviews don't converge](../references/decompose.md#when-reviews-dont-converge).

The full plan → run → integrate walkthrough is in [decomposed runs](decomposed-runs.md#plan-run-integrate). `retry`, `accept` and `review` are covered in [reviews, retries and recovery](reviews.md); resuming, `split`/`combine`, scope drift and `tasks.tsv` in [decomposed runs](decomposed-runs.md).

## `scripts/forge-jev.py`

The verbs the decision-model gates run. A person turns gates on with `forge decide`; the shell
calls these verbs, one per gate question. The `forge jev` verbs `setup`, `sites`, `enable`,
`disable`, `status` and `doctor` are the old names of `forge decide` commands and are not listed.

```
forge-jev.py score-plan      --plan P --repo R [--json]     prompt adequacy and drift, per task
forge-jev.py coupling        --plan P --repo R [--json]     forward dependencies, per warned pair
forge-jev.py coverage        --plan P --repo R [--json]     requirements no task covers
forge-jev.py retry-addressed --plan P --task T --attempt N --repo R
forge-jev.py dwarf-message   --plan P --task T --attempt N --repo R
forge-jev.py attribution     --plan P --repo R --log L      the merged tasks, most likely culprit first
forge-jev.py findings        --repo R --input F [--run-dir D]
forge-jev.py verify-discover --repo R [--json] [--run-dir D]
forge-jev.py memory-curate   --repo R --category C --text T [--json] [--run-dir D]
forge-jev.py memory-slice    --task T --lines F [--keep N] [--run-dir D]
forge-jev.py memory-stale    --text T --anchor A [--run-dir D]
```

The verbs `coverage`, `retry-addressed`, `dwarf-message`, `attribution` and `findings` check their
paths first: `--plan`, `--repo` and `--run-dir` must be directories, and `--log` and `--input`
files, for the verbs that take them, else the verb exits `2`. Exit `3` is no opinion: the gate is
off or in `shadow`, there is nothing to ask, or the model did not answer. A failed request is never a verdict. The rows the
shell reads, one per item:

- `coverage`: `r<i>`, the probability, and the requirement, for each uncovered requirement at or above `coverage_warn`.
- `retry-addressed`: `u<i>`, the probability, and the finding, for each finding at or above `untouched_warn`.
- `dwarf-message`: the first of `question`, `blocked`, `unfinished` that fires, as `<which>` and its probability; otherwise `ok` and the highest probability.
- `attribution`: `<id>` and its probability, highest first, `none` included.
- `findings`: a JSON list in input order, `[{"fp", "duplicate_of", "dup_p", "concrete"}]`. `--input` is a JSON object `{"findings": [...], "candidates": [...]}`; `--run-dir` is the plan the findings came from, for the dataset and the request cache.

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
