# Decomposed runs: integration and verification

How a decomposed run's work comes together: chained plans, branches, worktrees, verification.
Part of the decomposition reference; planning is in [decompose.md](decompose.md).

## Contents

- [Chained plans](#chained-plans)
- [Branches, worktrees, cleanup](#branches-worktrees-cleanup)
- [Failure modes](#failure-modes)
- [Execution checks and integration verification](#execution-checks-and-integration-verification)

## Chained plans

`plan <dir> --after <prev>` starts `<dir>` from an earlier plan's integration branch. `<prev>`
is that plan's directory. See the [plan flags](../docs/commands.md#scriptsforge-parallelsh).

`plan` writes `<prev>`'s absolute path to `after` and the starting commit to
`integration.base`, and creates `forge/<run>-integration` at `<prev>`'s
`accepted.integration`. A branch that already points at that commit is kept. The approval
table prints `after: <run> (<base>)`, with the first 12 characters of that commit. A later `plan` of the same directory without `--after` keeps the
files and the branch. A chained plan skips the warning about uncommitted changes in the
repository: its worktrees start from the previous plan's accepted tip.

`<prev>` has to be a plan of the same repository. Every refusal below is exit 2, and it leaves
the plan directory and the branches as they were:

- `<prev>` is not a directory, `--after` was given no directory, or the directory has no
  `tasks.tsv`, `run_id` and `repo`
- the plan would come after itself
- the two plans belong to different repositories
- a task of `<prev>` is not `MERGED`
- `forge/<prev-run>-integration` is missing, `<prev>` has no `accepted.integration`, or the
  branch tip is not that accepted commit. Extra commits on the branch are adopted into
  `<prev>` first (`forge-parallel.sh adopt <prev> --head --reason "…"`)
- `<prev>`'s scheduler is live (`scheduler.live` names a pid that is still running)
- this plan's own `forge/<run>-integration` is at another commit: it is checked out, it is
  not still at the recorded `integration.base`, or this plan already has merged work

Before any merge, a different `--after` moves the branch when it is still at its old
`integration.base` and is not checked out. After a task has merged, a different `--after`
is the same exit 2, and the recorded base stays.

`run` checks again while no task of this plan has merged. If `<prev>` has no
`accepted.integration`, `run` notes that and goes on. Otherwise that commit must be an
ancestor of this plan's `integration.base`. If `<prev>` moved, `run` exits 3, dispatches
nothing, and tells you to run `plan <dir> --after <prev>` again. Planning again with the
same `--after` records the new tip. A plan split from a chained plan starts later than
`<prev>` ended; the old tip is still an ancestor, so the check passes. Once a task of this
plan has merged, `run` does not check again.

`integrate` merges this plan's integration branch, which already holds every plan it follows,
and prints `this brings plans A → B → C` with the run ids, oldest first. A predecessor
that cannot be read, or a chain longer than 64, makes that line start with `…`. A plan
with no `after` file prints no such line.

`forge runs` puts `after <run>` on the plan's summary line. `<run>` is the predecessor's run
id, or its key when that directory is gone. forge-ui shows After, a link to the plan this run
follows, and Followed by, the plans whose `after` names this run.

## Branches, worktrees, cleanup

```
<plan-dir>/tasks/<id>/approach.md               intended approach, if planned
<plan-dir>/retry_per_model                      failures on each model before the next rung (1-99); absent leaves escalate_after
<plan-dir>/tasks/<id>/prompt.dispatched         prompt.md as given to the implementer; written last at dispatch
<plan-dir>/tasks/<id>/approach.dispatched       approach.md as given; empty when there was none
<plan-dir>/tasks/<id>/stale.diff                unified diff when prompt.md or approach.md changed during the attempt
<plan-dir>/tasks/<id>/outside.json              paths a plan stop named, with owner, owners and owner_status
<plan-dir>/tasks/<id>/risk                      low|medium|high, recorded only (#122), if rated
<plan-dir>/groups.tsv                           id, review, qa, members, title, goal; absent unless groups are on
<plan-dir>/groups.frozen                        sha256 of the six columns, then each member's deps and files
<plan-dir>/group_max_tasks, group_max_diff_kb   review-group bounds; defaults 6 and 256 when the file is absent
<plan-dir>/groups/<gid>/                        one review=group group's review; see Groups and group review
<plan-dir>/tasks/<id>/reviewed.commit           commit a review=group member stopped at (also a task review's)
<plan-dir>/tasks/<id>/source.fingerprint        worktree fingerprint beside that commit
<plan-dir>/tasks/<id>/settled.group             gid and round, once a group FAIL judged this member
<plan-dir>/tasks/<id>/retry_sources.tsv         attempt, source, epoch: why each retried attempt ran (#133)
<plan-dir>/run.flags.json                       flags of this run of the plan, rewritten at each real start; not copied by split or rerun
<plan-dir>/handoff.notes.jsonl                  session notes of what is not on disk: at, actor, text; newest first in forge handoff
<plan-dir>/handoff.driver.json                  harness, session, since, last_seen of the session driving the run
<plan-dir>/tasks/<id>/interrupted-<epoch>.diff  uncommitted work saved before forge handoff resume resets a stale RUNNING worktree
<plan-dir>/resume-<epoch>.{log,done}            log of the run forge handoff resume started, and its exit code once that run ends
<repo>/.forge/                                  project memory (survives the run)
<repo>/../.forge-worktrees/<run-id>/<task-id>   task worktree
<repo>/../.forge-worktrees/<run-id>/_integration integration worktree
<repo>/../.forge-worktrees/<run-id>/_group-<gid> group-review candidate; kept for the merge
forge/<run-id>/<task-id>                        task branch
forge/<run-id>-integration                      integration branch
```

A split task's subtasks are a run of their own, named after the task
([Subtasks](decompose.md#subtasks-a-task-split-at-any-level)):

```
<repo>/../.forge-worktrees/<run-id>--<task-id>/<subtask-id>   subtask worktree
forge/<run-id>--<task-id>/<subtask-id>                       subtask branch
forge/<run-id>--<task-id>-integration                        the subtasks' integration branch, merged into forge/<run-id>/<task-id>
refs/forge/kept/<run-id>/<task-id>                           the task's earlier work, after --decide split=fresh
```

A deeper level repeats the pattern (`<run-id>--<task-id>--<subtask-id>`). `forge runs rm
--worktrees --branches` on the root run removes all of them; a sub-plan is not deleted by itself.

### Worktree setup

A fresh worktree is a bare checkout — no `node_modules`, no venv, no build output. Without help,
every dwarf in the run rediscovers that the same way (run the tests, watch them fail, work out the
install) and pays it again, which is both slow and a source of near-identical "this worktree has no
dependencies" entries in project memory.

Tell forge what a worktree needs and it runs it verbatim, once per worktree, from the worktree
root, at creation time only:

- `.forge/setup` in the repo — executable, or any file whose contents are a shell command;
- `--setup <command>` on `run` or `retry`, which overrides it.

A resumed run or a `retry` reuses the existing worktree and does not pay it again. The integration
worktree gets the same treatment, since that is where a merged result is validated — without it the
one checkout holding every task's merged work is the one checkout that cannot build it.

Failure is reported and non-fatal: the dwarf gets a worktree it can still install into itself,
which is the status quo this replaces. Output lands in `tasks/<id>/setup.out`.

**Submodules.** `git worktree add` leaves every submodule directory empty. Before the setup step,
forge fills them offline from the source checkout (no fetch, no `git submodule`), so `.forge/setup`
sees a complete tree. Each submodule gets one state in `tasks/<id>/submodules.txt`:

- `exported` — the recorded commit was extracted from the source's submodule repository;
- `copied` — that commit was not available, so the source checkout's files were copied;
- `kept` — the directory already held files (a resume), left as they are;
- `missing` — nothing was available; the directory stays empty and the runner prints a note.

The files carry no `.git`, so git ignores them: they never appear in `changes.diff`, a commit, a
review snapshot or a fingerprint, and the worktree still moves and is removed normally. The capsule
tells the dwarf which submodules are present. Integration worktrees (`run`, `integrate`, `combine`,
`split`) get the same treatment. Out of scope: a task that moves a gitlink's recorded commit (its
worktree keeps the old content) and nested submodules (left empty).

**Reviewers get the same dependencies.** A review snapshot holds committed files only, so forge
links the directories package managers install into (`node_modules`, `bower_components`, `.venv`,
`venv`, `vendor`, `.bundle`, `Pods`, Mix's `deps` and `_build`, `.dart_tool`, `elm-stuff` and
SwiftPM's `.build`) from the checkout under review (the task's
worktree, or the integration worktree for a final review) into the snapshot, at the root and in
workspace packages down to five levels, unless the snapshot already holds that path or lacks its
parent. These are the directories this step filled, so they match the attempt. The links count for
no fingerprint and stay out of the reviewer's `git status`. The prompt names what was linked and what
is committed, or says there is none, says when forge ran no tests of its own, and tells the reviewer
to report rather than search or install. A directory that is only partly committed (`vendor/.gitkeep`)
keeps its committed files and gets no link. Nothing guards a write
through a link: forge commits whatever git does not ignore, so a linked directory is an ignored one
(a directory the attempt committed is already in the snapshot and gets no link), and a write there
changes nothing that merges. A link dangles once its worktree is removed, which harms nothing. Each
reviewer ends with `FORGE_TESTS: RAN`, `RAN — FAILED: <names>`, `NOT NEEDED — why` or
`COULD NOT RUN — why`; `FAILED` keeps the gate from accepting a FAIL, `COULD NOT RUN` prints a note,
and forge-ui and the dataset show it.

Forge does not guess this step, and the obvious guess is worth naming because it looks right and
is not: symlinking the source repo's `node_modules` into the worktree. In a workspace monorepo the
workspace links inside it point back at the source checkout, so the worktree builds against the
source repo's packages rather than its own — producing type errors naming files the task never
touched. Copying is correct but can be gigabytes per task. The repo knows which of those it wants;
forge does not.

The worktree root is a sibling of the repo and **never** `TMPDIR`. Isolated worktrees are
never pushed, so a temp root that gets cleaned takes the only copy of that work with it —
this has already destroyed real work in this user's `linear-spanks` runs.

Cleanup rules:

- A successful task's worktree is removed only after `results.tsv` is written and read back.
- A failed, conflicted or unknown task keeps **both** its branch and its worktree, so there
  is something to inspect and retry from.
- The integration branch is left in place. Merging it into the user's branch is a separate
  `integrate --approved` step and never happens automatically.

The user's own branch and working tree are untouched for the whole run.

## Failure modes

| Symptom | Cause |
|---------|-------|
| task status `ERROR` | worktree creation or a dispatch failed; see `tasks/<id>/dwarf.out` / `qa.out` |
| task status `TIMEOUT` | that role exceeded the dispatch timeout and was killed. It has usually left a partial edit in the worktree; `retry` continues from it |
| task status `FAIL` with "produced no changes" | the dwarf ended without editing anything — usually an ambiguous prompt it could not resolve headlessly |
| task status `UNKNOWN` | QA never emitted a verdict line; read `tasks/<id>/qa.last`. `retry` re-runs QA only, on the unchanged work |
| task status `INFRA`, `run` exits `8` | an infrastructure stop; the cause is in the summary and `tasks/<id>/infra.txt`. No attempt spent. Fix a "needs you" line before re-running; otherwise re-run, or wait with `--infra-retries M`. See [Infrastructure failures](decompose-recovery.md#infrastructure-failures) |
| task status `NEEDS_DECISION` | reviews are not converging, the retry policy has no stronger model, every blocking finding needs a file the task does not declare (reason `plan`), or the attempt cap is spent. Read `tasks/<id>/decision.md`. Only a decision resumes it |
| task status `STALE` | `prompt.md` or `approach.md` changed during the attempt. Not reviewed, and no attempt spent. The scheduler re-dispatches it with the new text; so does `forge control retry`. See [Retrying, and resuming an interrupted run](decompose-recovery.md#retrying-and-resuming-an-interrupted-run) |
| task status `IMPLEMENTED` | a `review=group` member finished implementing and waits for its group's review. `retry` and `accept` exit 3. See [Groups and group review](decompose-opt-in.md#groups-and-group-review-opt-in) |
| group status `NEEDS_DECISION` | the combined diff is over `group_max_diff_kb`, or a blocking finding names no member. Read `groups/<gid>/decision.md`. `forge group show` prints it; `forge group retry --tasks` only on the user's word, and only for an ambiguous decision |
| a group left `INFRA`, `TIMEOUT` or `INTERRUPTED` | the review did not finish. The next `run` reviews it again. `forge group retry` rearms it to `INTERRUPTED`. No member attempt was spent |
| a group left `UNKNOWN` | no verdict. The next `run` reviews it again while `--retry-failed` budget remains; otherwise it stays. `forge group retry` rearms it. No member attempt was spent |
| a group left `ERROR` | the dispatch failed, or the review ended while still `REVIEWING`. The next `run` leaves it. `forge group retry` rearms it to `INTERRUPTED`. No member attempt was spent |
| task status `INTERRUPTED`, `run` exits `130` | the run was signalled or stopped with `forge control stop --now`; verification was skipped. The summary prints `forge-parallel.sh retry <plan> <id>` for each one; a plain `run` does not dispatch them |
| a run that sits idle with tasks `PENDING` | `forge control drain` is in effect (`forge control show` says `drained`; `forge control resume` continues), every scope a task needs is paused on infrastructure (`infra.paused`), or `max-parallel` is lower than you think |
| a `FAIL` task merged anyway | the threshold (`P2` by default) tolerated its findings, or a person ran `accept`; see `known_issues.md` and `qa.gate` |
| a task `FAIL`ed although its reviewer said `PASS` | forge's gate: a CONFIRMED finding at or above the threshold was listed (`qa.gate` decision `fail`); the retry starts with a note saying so |
| task status `CONFLICT` | QA passed but the merge onto the integration branch conflicted — `files` was understated somewhere |
| every wave has one task | `files` sets overlap across most tasks; the decomposition is not actually parallel |
| dwarves ignore existing work | `goal.txt` or `files` missing, so the capsule carries no useful status |
| `.forge/` appears in a task's diff | the capture is missing `':(exclude).forge'` — see `memory.md` |
| dwarves build incompatible interfaces | no `approach.md`; the seam was never agreed. Plan it, or use `--planner` |
| "scope drift" in the summary | the task touched files it did not declare. Not a failure — but if a later task conflicts on one of those files, this is why |
| `retry` says "produced no new changes" | the dwarf returned a byte-identical diff. It has nothing to add; escalate with `--dwarf` or fix the prompt |
| a task will not re-run: "already exists" | a worktree directory was deleted by hand. `run`/`retry` prune stale registrations first, so re-run rather than deleting the branch |

A run where several tasks report "produced no changes" usually means the decomposition
produced tasks that were not independently actionable — the fix is a coarser
`--decompose-level`, not more retries.

## Execution checks and integration verification

Before dispatch, `run` preflights every task's dwarf and QA plus a configured planner.
`retry` repeats preflight and dependency checks. A dependency must be MERGED, not merely
planned or QA-passing; otherwise its consumer is BLOCKED without spending a dispatch.
Directory/child ownership paths overlap and cannot run concurrently. A blocked task can be
retried after its prerequisites merge. A fresh task then branches from current integration.

QA receives complete implementation input, approach, capsule and cumulative binary diff.
It runs in a disposable repository. Both source and review fingerprints must remain unchanged,
and the task branch must still point at the reviewed commit when merged; the review copy is a
commit, so only its tracked content counts (a test run's untracked `__pycache__/` there changes
nothing). INVALIDATED results never merge; `retry` re-runs only QA while the work is unchanged. Disposable snapshots detect mutation; they do not confine unrestricted shell access.

Configure combined executable checks with `run <plan> --verify '<command>'` (also accepted by
`retry`), or a shell command file at `<repo>/.forge/verify`. Commands execute under Bash from
the integration checkout. Forge records `verification.command`, `.log`, `.exit`, `.fingerprint`
and `.status` in the plan directory. Failed checks or source changes fail the run. With no
command, the result is explicitly UNVERIFIED. A `retry` that leaves tasks still to run
(PENDING or BLOCKED) skips its verification and says so: the `run` that dispatches them verifies
the integration branch at its end, and `integrate` verifies its candidate.

A failed command whose source fingerprint is unchanged is rerun (`--verify-retries N`, default
`1`, `0` = strict single run). When the repository has `.forge/verify-subset` (a test command
with `{files}` or `{changed}`) and the log names failing tracked test files (unittest
`FAIL:`/`ERROR:` lines, pytest `FAILED path::`, node's test runner `test at file:L:C` or TAP
`location:`), those files run alone first (`verification.failing.log`, run kind `failing`): if
they fail again the regression is confirmed and the whole command is not rerun. If they pass,
or nothing can be named, the whole command reruns as before, so a pass always comes from the
whole command. A rerun that passes leaves status `PASS` but writes
`verification.flaky` (both exit codes), keeps the first run in `verification.log` and the second
in `verification.retry.log`, and prints a loud `FLAKY` line in `run`/`retry`/`integrate` output;
a command that changes the tree is never retried.

**Verification lanes.** Instead of one command, verification can be several named **lanes**
that run side by side, each rerun on its own. A lane is a file whose name is the lane's name
(letters, digits, `_` and `-`, at most 64) and whose text is a Bash command, run from the
integration checkout like the single command; leading `#forge:` lines give it options. Declare
them in the repository as `.forge/verify.d/<name>` files, or for one plan with
`run --verify-lane 'name[opt,opt]=command'` (also `retry`; repeatable, written to
`<plan>/verify.d/`; the first one of an invocation replaces the plan's earlier lanes and its
`--verify`, as `--verify` replaces the plan's lanes). The first source that exists wins: the
plan's lanes, the plan's `--verify`, the repository's `.forge/verify.d/`, then
`.forge/verify` (ignored, with a note, when the repository also has lanes). The repository's lanes
and `.forge/verify` are read as committed on the branch being verified, not from the root checkout,
and forge says before a run which lanes differ from the root's (#151); `forge-parallel.sh verify <plan>`
runs that verification again on its own, with no model. `run` checks the
lanes before any model is paid for and exits 2 on an invalid one (an unknown or repeated
option, an empty command, a path outside the checkout, a missing baseline).

| Option | Means |
|---|---|
| `group=<name>` | lanes of one group never run at the same time (a shared database, a port) |
| `first` | runs before every other lane; if a `first` lane fails (after its reruns) or changes the tree, the rest are `skipped` |
| `retries=<0-9>` | reruns of this lane when it fails; otherwise `--verify-retries` (default `1`) |
| `reports=<dir>` | JUnit `*.xml` and TAP `*.tap` reports the lane writes under `<dir>` (inside the checkout). Only reports written during that run count; they give the lane's test counts and failing tests, and are re-read every 10 s while it runs |
| `baseline=<file>` | known failing test names, one per line (`#` comments), inside the checkout. A lane whose every failing test is in it ends `known`, which passes. A report that could not be read, or a failure no report names, is judged by the exit code |

Up to `--verify-parallel N` lanes run at once (`run`, `retry` and `integrate`, kept as
`verify_parallel`, 1 to 32, default 3), started in name order. A lane that fails is rerun alone,
after the round, up to its budget; a pass on a rerun makes it `flaky`, which passes but is named
in the run output (`FLAKY: lane <name> failed once and passed on rerun`), in
`verification.flaky` and in the whole-run review. Nothing reruns once the tree changed: the
checkout's fingerprint is compared after every round, and a change fails the verification
(`mutated`; with lanes in parallel it cannot be pinned to one of them). A lane's states are
`waiting`, `running`, `pass`, `fail`, `flaky`, `known`, `skipped` and `interrupted`. Lanes mode
replaces the whole-command rerun above; the single-command mode is unchanged.

A lane may print `::forge step name=<step> state=start` and `::forge step name=<step>
state=end [exit=<n>]`, each on a line of its own, to say which step it is in; forge-ui shows the
open step live. A signal stops the running lanes' process groups (TERM, then KILL after 5 s),
marks them and the verification `INTERRUPTED`, and exits 130 like a single command.

The runner is `scripts/forge-verify.py` (`lanes` lists and checks a directory's lanes; `run`
runs them). In lanes mode `verification.command` holds one `<lane>: <first line>` per lane,
`verification.log` the runner's summary plus the last 200 lines of each lane that failed, and
`verification.lanes.json` the live state of every lane; each lane writes
`verification.lane.<name>.log` and keeps each finished run as `verification.lane.<name>.run-<k>.log`.

```
.forge/verify.d/lint     #forge: first
                         npm run lint && npm run typecheck
.forge/verify.d/unit     #forge: reports=reports/unit
                         npm test -- --reporter=junit --outputFile=reports/unit/junit.xml
.forge/verify.d/db       #forge: group=postgres retries=2 reports=reports/db baseline=.forge/known-db-failures.txt
                         docker compose up -d postgres && npm run test:db
```

Here `lint` runs alone first; `unit` and `db` then run together, another lane in the
`postgres` group would wait for `db`, and a `db` run whose only failures are listed in the
baseline ends `known`.

`scripts/forge-verify-coverage.py` reads the project's own test configuration and **warns**
(never fails) about suites the command does not run — at `run` start before any model is paid
for, again in `verify_result`, and at `integrate` — recording the lines in
`verification.coverage.txt`. Without a verify command it lists what the project defines.

`integrate <plan> --approved` prepares a separate candidate combining the current user branch
and integration revision, runs worktree setup and the configured check there, then updates the
user branch only if the candidate passed and the source branch stayed unchanged. Logs live in
`<plan>/integrate-*/`. A failed candidate remains available for inspection. With no verification
command, integration retains the existing approval boundary and reports UNVERIFIED.

Optional [Ripwire context](ripwire.md) is enabled by default. Use `--no-ripwire`
or `FORGE_RIPWIRE=off` to disable preparation and installation offers.
