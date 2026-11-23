# Decomposed runs: opt-in modes

Opt-in modes of a decomposed run: offer them, never add them unprompted. Part of the
decomposition reference; planning is in [decompose.md](decompose.md).

What the orchestrator does in each mode:

- **Groups**: offer `plan --groups` and `plan --group-review` for several small, related, low-risk tasks;
  keep a high-risk task on per-task review.
- **Brief and summaries** need a model the user names (`--brief-model`, `--summaries-model` or `forge
  config set`); forge never picks one. With brief on, relay the questions from `forge brief show <plan>`,
  pass the user's answers with `forge brief answer`, and never answer for the user. A strict brief run
  exits `3` before any task starts while questions are open.
- **Retro**: record each obstacle with `forge retro note <plan> --type T --fixable-by F --summary S`
  (`T`: `environment`, `sandbox`, `tooling`, `prompt`, `forge-bug` or `other`; `F`: `user`, `forge` or
  `unsure`), and at the end of the run relay the groups in `<plan>/retro.md`. `forge retro issues` posts
  only on the user's approval of the exact text it prints (`--apply --approve <hash>`).

## Contents

- [Groups and group review (opt-in)](#groups-and-group-review-opt-in)
- [Retro notes (opt-in)](#retro-notes-opt-in)
- [Brief mode (opt-in)](#brief-mode-opt-in)
- [Task summaries (opt-in)](#task-summaries-opt-in)

## Groups and group review (opt-in)

Two opt-ins share `groups.tsv`. A planner may name groups, and those are identity only
(`review=task`): they do not change dispatch or the estimate, and they are fine under Fractal.
`plan --groups` with `--group-review` is the other opt-in: one QA review covers several tasks.
Offer either; never add them unprompted. The user-facing tradeoff is in
[group review](../docs/reviews.md#group-review-opt-in). The data layer is `scripts/forge_groups`
and `scripts/forge-groups.py` (`validate`, `derive`, `resolve`, `freeze`, `check`, `of`, `show`).

### `groups.tsv`

Six tab-separated columns. `#` lines and blank lines are ignored.

```
# id	review	qa	members	title	goal
```

- `id` matches `[a-z0-9][a-z0-9-]{0,39}`, is unique, and is not also a task id.
- `review` is `task` or `group`.
- `qa` is a spec, or `-`.
- `members` is a comma list of task ids, in order. A task is in at most one group.
- `title` and `goal` are one line each, or `-` when absent. A planner's title and goal are
  at most 200 characters.

Where a row comes from:

- A planner reply that includes `groups` writes `groups.tsv` with `review` always `task` and
  `qa` always `-`. A planner group may have one member. The planner never sets `review=group`.
- `plan --groups waves` makes one group per computed wave that has at least two tasks, named
  `w1`, `w2`, and so on. A wave larger than the max splits in `tasks.tsv` order into `w3a`,
  `w3b`, and so on. A leftover single task is dropped. Each gets the title `Wave 3` (`Wave 3a`)
  and no goal: nothing in a computed wave says what it is for.
- `--group-review` is only valid with `--groups waves`. Without it, `plan` exits 2:
  `--group-review goes with --groups waves (it sets the review of the groups waves derives)`.
  The derived groups then have `review=group`. High-difficulty tasks stay out and keep their
  own review. A leftover single is dropped.
- `plan --groups FILE` copies that file. An existing `groups.tsv` and no `--groups` is used
  as it is.

`plan` computes waves, then validates, resolves each review group's `qa`, and freezes.
The approval table shows each group's title and goal, `(no goal)` when it has none, and one line
naming the groups without a goal. `forge group set PLAN GID --goal "…"` (or `--title`) writes it
and freezes the groups again, under the control lock and logged in `control.log` (actor `cli`).
It refuses (exit 3) when `groups.tsv` or a member's deps or files already differ from what `plan`
froze, since freezing again would accept those edits too. It works on a live run: a title or a
goal changes no membership, and the next group review or brief prompt uses the new text.

### Rules for `review=group`

These apply only when `review` is `group`. A `review=task` group is not checked this way.

- At least two members, and at most `--group-max-tasks` (default 6, plan file
  `group_max_tasks`). The flag accepts a positive integer. Deriving waves requires at
  least 2 (`max tasks must be at least 2`). Validating an existing file uses the same max,
  so 1 rejects every review group.
- No member depends on another member, directly or through a task outside the group.
- Declared `files` do not overlap. A directory covers the paths under it, `.` covers
  everything, and `-` declares nothing. Same rule as the scheduler.
- No sweep member (an id `sweep-N`, or `tasks/<id>/sweep.json`).
- `qa` of `-` becomes the QA spec of the highest-difficulty member (`high` over `medium`
  over `low`; a tie takes the first listed), as `tasks.tsv` has it after `plan`.

### The freeze, and what makes `run` refuse

`groups.frozen` is the sha256 of the freeze text: one line per group, all six columns, then
one line per member, `member<TAB>deps<TAB>files`. The dwarf column is not in it. No groups
removes `groups.frozen`. A `groups.frozen` with no `groups.tsv` fails the check.

`run` calls `forge-groups.py check` when `groups.tsv` or `groups.frozen` exists, a dry run
included, before any dispatch. A mismatch, a missing file, or groups that cannot be read
refuses with `groups.tsv or a member's deps/files changed since plan; run plan again`.

Group review and Fractal do not combine. `--fractal`, or a plan that is already fractal,
makes `plan` and `run` exit 2: `group review does not support Fractal yet`.

### The member lifecycle and `IMPLEMENTED`

A `review=group` member implements, writes `reviewed.commit` and `source.fingerprint`, and
stops at `IMPLEMENTED`. There is no per-task QA snapshot, no shadow review, no fail-first
check, and no QA-only resume (the `resume` marker is removed). `--pre-review-tests` still
runs, on a snapshot that exists only for that check. Red is `FAIL`, unreviewed, not
`IMPLEMENTED`.

`record_attempt` writes no `attempts.tsv` row while the status is `IMPLEMENTED`, and
`post_attempt` does nothing. The attempt cap counts `attempts.tsv` rows (an `INFRA` row
does not count), so that stop spends nothing. The `attempt` counter may already have moved.

`retry` and `accept` of an `IMPLEMENTED` task exit 3: `<id> is implemented and waits for
group <gid>'s review`. The scheduler does not dispatch that member again. A dependent waits
for `MERGED`, so it does not start.

### When a group is reviewed

The scheduler treats each `review=group` group as one unit, keyed `group:<gid>` in `active`.
It is ready when every member is `IMPLEMENTED`, nothing is running it, it has not merged, and
its status is empty or `WAITING`, or `FAIL` with `settled.round` equal to the current
`round`, or this run has marked it to review again. It starts as soon as the last member is
`IMPLEMENTED`. There is no wave barrier. It takes one slot, and it does not start while the
run is stopping, draining, or paused for infrastructure. The command is
`forge-parallel.sh _group`.

### The candidate, and the size bound

The candidate worktree is `<wt_root>/_group-<gid>`. It starts at the integration HEAD, then
merges each member's `reviewed.commit` with `--no-ff`, in member order.

- A member that is not `IMPLEMENTED`: the group is `WAITING`. Members stay as they are.
- A member whose branch tip is not `reviewed.commit`, or whose worktree fingerprint is not
  `source.fingerprint`: that member becomes `INVALIDATED` with `invalidated.reason`
  `group-candidate`, and the group is `WAITING`.
- A conflict between members: the group is `CONFLICT`, `conflict.files` lists the files, the
  merge is aborted, the members stay `IMPLEMENTED`, and nothing is dispatched.
- Any other merge failure, or no integration HEAD: `ERROR`.

The fingerprint is the sha256 of the sorted `task=commit` lines plus a final
`tree=<candidate tree>` line.

`combined.diff` is `git diff --binary --irreversible-delete` of the candidate against that
base, excluding `.forge`. Over `group_max_diff_kb` times 1024 bytes (default 256 when the
plan file is absent), the group is `NEEDS_DECISION` with reason `oversized`. Nothing is
dispatched, truncated, or sent to another reviewer. `decision.md` starts `reasons: oversized`
and offers two edits, then `plan` again: smaller groups in `groups.tsv`, or this group's
`review` set to `task`. `forge group retry` refuses an oversized group.
`--group-max-diff-kb N` is a positive integer, written only when given, so a re-plan keeps it.
The same is true of `--group-max-tasks`.

This is not a [whole-run review](../docs/reviews.md#whole-run-review-opt-in). That one reads
the combined result after the run and truncates. A group review that is over the bound never
builds a prompt.

### The prompt and `{tasks=…}`

One fresh QA dispatch. The prompt is the contract head for kind `group` (the task preamble,
plus that this review covers several tasks and must judge each against its own criteria and
judge the combined change), the group's title and goal, `Members, in merge order`, then any
findings from the previous round that named no task. Each member follows: title, `prompt.md`,
the declared files and the reviewed commit. When the task has them, it also has the intended
approach, that member's diff `--stat` against its base, pre-review text, guard notes and
drift. Findings the previous round attributed to the task are quoted, and the implementer's
reply is included when this attempt is not the one those findings judged. The tail is the task contract plus
the `{tasks=…}` instructions below, then the whole combined diff in a fence. The diff is not
truncated. An oversized group never gets here. The threshold is the plan's QA threshold.

`{tasks=…}` is its own brace. It is not the facts brace
`{reach=… origin=… evidence=…}`. The prompt says to write it immediately before the facts
brace, so the facts brace still ends the line. The last `{tasks=…}` on a finding wins. Ids
are separated by commas or spaces.

Attribution (`scripts/forge_groups/attribute.py`, written to `attribution.json` with the
round):

- A tag names those ids. An empty tag (`{tasks=}` or `{tasks= , }`) names nobody and does
  not fall back to the file.
- With no tag, the tasks are the members whose changed paths contain the finding's file, in
  member order. A leading `./` is stripped and nothing else is normalised: `src/./a.py` does
  not match `src/a.py`.
- Ambiguous when the tag names an id that is not a member, when the tag names nobody, when
  there is no tag and no file, or when no member changed that file.
- `blocking` is the gate's per-finding decision at the threshold (`none` is P3; unset is
  P2). `PLAUSIBLE` never blocks.
- `implicated` is the union, in member order, of the tasks on blocking findings that are not
  ambiguous. The top-level `ambiguous` list is the `n` of findings that are both blocking
  and ambiguous.

The facts line is `implicated<TAB>ambiguous<TAB>findings<TAB>blocking`. The two lists are
comma separated, or `-`. `findings` is the count. `blocking` is `1` or `0`.

### Each outcome, and what happens next

The round ends in `groups/<gid>/status`, and one `rounds.tsv` row: round, epoch, status,
implicated tasks or `-`, QA spec. Status writes are atomic.

- **`PASS`.** The scheduler runs `_merge_group`: one `--no-ff` merge of `candidate.commit`
  onto integration (`forge: merge group <gid> (ids)`). Every member is judged `PASS` (one
  `attempts.tsv` row), its status becomes `PASS`, `merged` is touched, and the group's
  `merged` is touched. A conflict sets the group and every member to `CONFLICT`, keeps
  `conflict.files`, and merges nothing. Integration moved (`accepted.integration` does not
  match) or dirty: the group is `INVALIDATED` with `integration-moved` or
  `integration-dirty`, and nothing merges. A member whose branch tip is not its reviewed
  commit, or whose worktree fingerprint is not `source.fingerprint`: those members are
  `INVALIDATED` with `group-candidate`, and the group is `INVALIDATED` with
  `candidate-moved`. A member that is no longer `IMPLEMENTED`, or a recomputed fingerprint
  that does not match, invalidates the group with `candidate-moved` and merges nothing;
  members are not marked `INVALIDATED` unless they moved. A member dependency that is not
  `MERGED` blocks the merge and leaves the status unchanged. `forge group retry` on `PASS`
  refuses; the next `run` merges it.
- **`FAIL`.** `_group_settle` writes each implicated member's `qa.last` (a header, the
  blocks attributed to it, `Also involves:` when a finding names other members too, then
  `FORGE_VERDICT: FAIL`), runs the QA gate, sets the status to `FAIL`, writes one judged
  attempt row, runs `post_attempt`, and writes `settled.group` (`gid round`). A member that
  is not implicated stays `IMPLEMENTED`: no `qa.last`, no attempt row, no gate. The group
  writes `settled.round`. The scheduler requeues the `FAIL` members when `--retry-failed`
  budget remains. Without it they stay `FAIL` and the others stay `IMPLEMENTED`. The next
  round starts when the implicated members are `IMPLEMENTED` again. Settling the same round
  twice does nothing. A `FAIL` that implicates no member settles nothing.
- **`NEEDS_DECISION`, ambiguous.** A blocking finding names no member. Members stay
  `IMPLEMENTED`. `decision.md` starts `reasons: ambiguous`. The user names the members with
  `forge group retry PLAN GID --tasks a,b`. That runs `_group_settle --tasks`, which fails
  those members, each with the ambiguous findings (marked as assigned by a decision), and
  also any member the review implicated by its own blocking finding, with only that. It moves
  `decision.md` to `decision.<round>.md`, and sets the group to `FAIL`. The next `run
  --retry-failed` retries those tasks. A live scheduler picks them up on its sweep when
  they have budget left. Do this only when the user says so.
- **`NEEDS_DECISION`, oversized.** Refused by `forge group retry`. Split `groups.tsv` and
  run `plan` again, or set that group's `review` to `task` and run `plan` again. The reason
  word is the first token after `reasons:`. Only `oversized` and `ambiguous` are handled.
  Any other reason is refused; read `decision.md`.
- **`UNKNOWN`.** No verdict. Members stay `IMPLEMENTED`. The scheduler reviews the group
  again within `--retry-failed`, counted in `groups/<gid>/retries.used`. No member attempt
  is spent. With the budget spent, the group stays `UNKNOWN`.
- **`INFRA`, `TIMEOUT`, `ERROR`, `INTERRUPTED`.** `INFRA` is dispatch exit 8: `infra.txt`
  has `stage=qa` and the class, and no attempt is spent. `TIMEOUT` is exit 7. Any other
  dispatch failure is `ERROR`. `forge group retry` on `UNKNOWN`, `INFRA`, `TIMEOUT`,
  `INTERRUPTED` or `ERROR` sets the group status to `INTERRUPTED` and touches no member
  file, so the next `run` reviews it again. `--tasks` is refused in those states. A review
  that dies while `REVIEWING` is marked `ERROR`. A scheduler killed mid-review leaves
  `REVIEWING`; the next `run` turns that into `INTERRUPTED` and reviews again, without
  re-implementing the members.
- **`INVALIDATED`.** A tracked change to the candidate or to the review snapshot during the
  review. An untracked file in the snapshot is not a change. Also the merge-time reasons
  above. The next `run` reviews it again, unless `invalidated.reason` is
  `integration-moved` or `integration-dirty`. `forge group retry` says the same and does
  not rearm it.
- **`CONFLICT`.** `forge group retry` refuses. Two paths differ. A conflict while building
  the candidate leaves the members `IMPLEMENTED`, and `retry` of an `IMPLEMENTED` task
  exits 3, so the printed `forge-parallel.sh retry` line does not apply until a member is
  no longer `IMPLEMENTED`. A conflict merging onto integration sets every member to
  `CONFLICT`, and a `CONFLICT` task can be retried.
- **`WAITING`.** A member is not `IMPLEMENTED`. **`REVIEWING`.** A review is running.
  Neither is retried with `forge group retry`.

### Attempt accounting

Only a judged attempt writes an `attempts.tsv` row, and the cap counts those rows. An
`IMPLEMENTED` member spends nothing. A member a `FAIL` does not implicate spends nothing.
A re-review after `UNKNOWN`, `INFRA`, `TIMEOUT`, `INTERRUPTED` or `ERROR` spends no member
attempt. On `PASS`, every member gets one judged `PASS` row, including members the findings
did not name. A later `FAIL` of only some members records one `FAIL` row for each of those,
at the attempt number they are on. Separately, a task attempt left `STALE` (`prompt.md` or
`approach.md` changed while it ran) is not judged. `attempts.tsv` records `STALE`, and the cap
skips that row, as it skips `INFRA`.

### Resume

`run` again does not re-implement a member left `IMPLEMENTED`. A `PASS` group is merged,
with no new QA dispatch; a stale member branch is not merged. An unsettled `FAIL` is
settled, then those members are requeued. `INTERRUPTED`, `INFRA` and `TIMEOUT` are reviewed
again. `INVALIDATED` is reviewed again unless the reason is `integration-moved` or
`integration-dirty`. `UNKNOWN` is reviewed again while the group's `--retry-failed` budget
remains; otherwise the status line is printed. `NEEDS_DECISION`, `CONFLICT` and `ERROR` wait
for a person. The summary says `forge: <id> is implemented, waiting for group <gid>
(<status>)` for a member still `IMPLEMENTED`. An empty group status is printed as
`not reviewed`. A dependent of an unmerged group stays `BLOCKED`.

### Files in `groups/<gid>/`

`status` (then `PASS`, `FAIL`, `NEEDS_DECISION`, `UNKNOWN`, `CONFLICT`, `INFRA`, `TIMEOUT`,
`ERROR`, `INVALIDATED`, `WAITING`, or `REVIEWING`; `INTERRUPTED` is written by the scheduler
or by `forge group retry`), `round`, `rounds.tsv`, `candidate.base`, `candidate.commit`,
`members.tsv` (round, task, reviewed commit, attempt; one row per member of a round that
built a candidate), `fingerprint`, `combined.diff`, `members.paths` (`task<TAB>path`),
`attribution.json`, `qa.input`, `qa.last`, `qa.out`, `qa.gate`, `qa.round-<n>.last`,
`known_issues.md`, `infra.txt`, `decision.md`, `conflict.files`, `review.base`, `merge.out`,
`group.out` (the scheduler's log). Settle writes `settled.round`, `settle.tasks` (until the
round is settled) and `decision.<round>.md`. Merge writes `merged` and, when it refuses,
`invalidated.reason` (`integration-moved`, `integration-dirty`, `candidate-moved`).
`retries.used` counts `UNKNOWN` re-reviews. The candidate worktree stays at
`<wt_root>/_group-<gid>`.

On the member: `reviewed.commit`, `source.fingerprint`, `settled.group`, and
`invalidated.reason` of `group-candidate` when the member moved. Plan files: `groups.tsv`,
`groups.frozen`, `group_max_tasks`, `group_max_diff_kb`.

The dataset records `group_review` and `group_merge`. The QA dispatch has `scope` `group`
and an empty task, and it is left out of the per-task reviewer statistics. A judged member
attempt is linked with `review_scope=group`, `group` and `round`, and does not keep a copy
of that review's `qa.last` or `qa.gate`. See
[the dataset](dataset.md#raw-events-not-training-examples).

`forge group show PLAN [--group GID] [--json]` reads this. `forge group retry` is the
decision and the rearm above. Exit codes and flags are in
[commands](../docs/commands.md#the-forge-command).

### Out of scope

- Shadow reviews of groups. A member skips shadow, and the group dispatch is one fresh QA.
- Fractal. `group review does not support Fractal yet`.
- A reviewer session across rounds. The group dispatch does not continue a session.
- Fail-first on a member. Pre-review tests still run, as above.

## Retro notes (opt-in)

`plan --retro` makes each agent record what got in its way: a sandbox that blocked a command, a
tool that is not installed, an instruction it could not follow, a forge fault. A note says what
happened and who can fix it. Without a flag a plan keeps its earlier choice, and a first plan
takes the user setting (`forge config set retro on`). `plan --no-retro` turns it off.

- **What an agent writes.** Most tasks have nothing to report, and a hard task is not a note.
  A note is one `FORGE_RETRO: {json}` line in the final message. The planner, QA and the
  whole-run review write it the same way. A dwarf blocked before it finishes also appends the
  same JSON to `.forge/retro-inbox.jsonl` in its worktree, which forge reads when the dispatch
  ends. The fields are `type` (`environment`, `sandbox`, `tooling`, `prompt`, `forge-bug` or
  `other`), `fixable_by` (`user`, `forge` or `unsure`), `summary`, `evidence` (the command, its
  exit code and the error, up to 2 KB), `blocking` and `tried`.
- **Checked against the log.** forge looks for the note's command and the first line of its
  error in that dispatch's log. When both are there the note is `verified`; otherwise `claimed`.
- **Orchestrator notes.** `forge retro note PLAN --type T --fixable-by F --summary S` records
  one note of the run's own, with `--command`, `--exit`, `--error`, `--blocking` and `--tried`
  as needed. It is always `claimed`, and it does not check the retro setting.
- **Where it goes.** `<plan>/retro/notes.jsonl`, `tasks/<id>/retro.json` for a task, and one
  `retro_note` dataset event per note. `forge retro report PLAN` writes `<plan>/retro.md`.
  Notes with the same type and the same normalised command form one pattern; a note with no
  command is matched on its normalised summary instead. Each pattern
  goes to the group its majority `fixable_by` names: **You can fix this on this machine**
  (`user`), **forge should fix this** (`forge`), or **Not sure who should fix this** (`unsure`,
  also on a tie). A run prints one line with the pattern count and the path when there are any
  patterns. The first plan with retro on says what is recorded, once per user.
- **Filing.** `forge retro issues PLAN` drafts one redacted issue per `forge` pattern, for
  forge's own repository, and posts nothing. The dry run prints the texts and their hash.
  `forge retro issues PLAN --apply --approve HASH` posts only when the hash matches those texts
  and the `--choice rN=new|comment:N|skip` choices. It needs an authenticated `gh`, and it
  refuses a never-send repository. Both refusals exit `3`.
- **Never memory.** Notes never go into `.forge/memory.md` or the ledger, and no prompt reads
  them back. A retro plan does add a short instruction block to each prompt, at its end.
- **Off.** With retro off (`--no-retro`, no setting, or `FORGE_RETRO=off`) no instruction is
  added to any prompt, the inbox is not read, and forge stores no note from a dispatch. Its
  prompts stay as they were without the feature. `forge retro note` still records a note when
  it is called, because it does not check the setting.

Files it adds: `retro_mode` (plan file), `retro/notes.jsonl`, `retro/.lock`, `retro.md`,
`tasks/<id>/retro.json`, and `.forge/retro-inbox.jsonl` in a worktree (removed once its notes
are stored). It costs no model call of its own.

## Brief mode (opt-in)

With `plan --brief advisory|gated|strict --brief-model SPEC`, a read-only side dispatch writes
the questions each task needs answered before it starts. Each question has a default. A task
with no answer follows the default. The user answers with `forge brief answer`.
A task-scoped answer goes into that task's prompt. A `wave` or `plan` answer stays parked
until an approved proposal applies its diff. forge never answers for the user.

- **Cost.** One dispatch per task that has no `brief.json`, at effort `low`, four at a time.
  A reply that is not valid JSON, or has the wrong shape, is retried once; a second failure
  stores no questions and the reason in `brief.json`'s `error`. Answers, stale flags and
  `reject` call no model. A proposal is one more side dispatch, kind `brief-change`. Each
  unstarted task whose questions are regenerated is one more, kind `brief`. Both are read-only,
  at effort `low`, on the brief model.
- **When.** `run` writes the missing briefs when it starts. `strict` waits for them; `advisory`
  and `gated` write them alongside the scheduler. `forge brief write PLAN [--task ID] [--jobs N]
  [--force]` does the same by hand, and `--force` redoes a task that already has a brief.
- **Levels.**
  - `advisory`: the run does not wait. Open questions reach the prompt under "Open questions:
    follow the stated default", with each default.
  - `gated`: a task with an unanswered must-answer question is held. The run prints
    `forge: <task> waits for your answer to <qid>` with the `forge brief answer` line. A task
    whose brief is still being written waits too.
  - `strict`: `run` exits `3` before any task starts while a question of an unstarted task is
    open. It prints each question, its default and the answer command. Answer them and run again.
- **Answers.** `forge brief answer PLAN TASK QID TEXT` (or `--file F`; `-` reads stdin). For a
  task-scoped question, an answer given before the task starts goes to `brief.answers.json` and
  into the prompt under "Decisions the user made", which overrides what the requirements say.
  After the task starts, that answer goes to `brief.late.json` and changes nothing in the run.
  The command then prints the `tasks.tsv` row, the `prompt.md` a follow-up needs, and the
  `forge control add` line that queues it.
- **Scope.** `--brief-scope` is `task` (the default), `wave` or `plan`. The question's own
  scope decides what an answer may change. `task` writes the answer into that task. `wave` and
  `plan` questions are parked in `brief.parked.json` whatever `--brief-scope` says, and they
  change the plan only through a proposal you approve. Both scopes use the plan's
  [groups](#groups-and-group-review-opt-in). A wave proposal needs the question's group in
  `groups.tsv`. When the task belongs to a group, the question
  writer is told that group's title, goal and the other tasks in it. It asks `wave` when the
  answer would change how several tasks of the group are done, and `plan` when the answer
  would add, remove or split tasks, or change their order. A wave question from a grouped
  task records `"group": "<gid>"`. A plan question from a grouped task records `"group": null`.
  A task in no group keeps the prompt it had before groups, and its parked questions have no
  `group` key. With `--brief-scope wave` or `plan` and no `groups.tsv`, forge prints one note
  and goes on. `brief.groups.noted` keeps that note to once per plan. The note says to run
  `plan --groups waves`, or to let the planner write groups, because a wave question with no
  group stays parked. `gated` and `strict` still read only the task questions in `brief.json`.
  A parked question does not hold a task.
- **Parked answers.** A parked question can be answered when the brief scope is at least as
  wide as the question. `wave` answers wave questions. `plan` answers wave and plan questions.
  `task` answers neither: the question id is unknown, and `answer` exits `2`. The answer goes
  to `tasks/<id>/brief.parked.answers.json` as `{qid: {text, actor, t}}`, including after the
  task has started. It is never a late answer, and it does not change a prompt. The command
  prints `forge brief propose PLAN TASK QID`. Nothing in the plan changes until that proposal
  is approved. `forge brief show` lists parked questions by group: `Group <gid> (<title>):`
  and the question, or `Group <gid>:` when the title is `-` or the group is gone, then
  `Plan:` for a plan question. A question with no `group` key stays under
  `Parked (wider scope):`.
- **Proposals.** `forge brief propose PLAN TASK QID` asks the brief model what that parked
  answer changes, in one read-only side dispatch of kind `brief-change`, and stores the result
  under `proposals/<pid>/` (`p1`, `p2`, and so on). Each directory holds `proposal.json`,
  `diff.patch` (a unified diff of the plan files it would change) and `hash`. `state` is
  `proposed`, `refused`, `approved` or `rejected`. A reply that breaks the rules below, or
  that changes nothing, is stored as `refused` with its reason and its diff, and `propose`
  exits `3`. `apply` leaves a refused proposal where it is. A failed dispatch stores nothing.
  The question's scope is the proposal's scope: under `--brief-scope plan`, a wave question
  is still a wave proposal. A plan question needs `--brief-scope plan`, or `propose` refuses
  it. `forge brief proposals PLAN` lists every proposal, oldest first, with its state, scope,
  group, summary and diff. `--json` prints the same records. A `proposed` one also prints the
  exact `forge brief apply` line. `forge brief reject PLAN PID [--reason TEXT]` sets
  `rejected`. It touches no plan file. Only a `proposed` or `refused` proposal can be rejected.
- **What each scope may change.**
  - `task`: the answer, as under Answers. There is no proposal.
  - `wave`: `prompt.md`, `approach.md` and the `files` column, and only for unstarted members
    of the question's group. The question must name a group that is in `groups.tsv`. A change
    that names another task, a started member, or any other field refuses the whole proposal.
    A wave question with no group cannot be proposed.
  - `plan`: the same three fields on any unstarted task, new tasks, removal of unstarted
    tasks, and new dependency lists for unstarted tasks. A split is a removal plus the added
    tasks, and every task that still depended on a removed one needs its deps rewritten in
    the same reply. An added task's group is null or an existing group id. Anything that
    would not be a plan `plan` could have written refuses the whole proposal. Between runs,
    an added row gets dwarf and qa `-`.
- **Approve by hash.** `hash` is the sha256 of `diff.patch` and the current contents of every
  target. For a wave `files` change the target is that task's `tasks.tsv` row. For a plan
  change the hash also covers `tasks.tsv`, `groups.tsv` and every edited prompt or approach.
  `forge brief apply PLAN PID --approve HASH` must quote the hash `proposals` shows. A
  different hash, a state other than `proposed`, a target that has started, or a target that
  changed since the proposal, refuses the apply and writes nothing. After a target changes,
  propose again.
- **Live and between runs.** While a scheduler is live, a wave proposal that changes only
  prompt and approach is written. The next attempt of an unstarted task reads those files.
  A wave proposal that changes `files` is refused while the run is live, and nothing is
  written: the scheduler keeps the files column it read at start. With no live run, a wave
  `files` change is written, `groups.frozen` is re-frozen (a stale marker left by a plan
  proposal stays stale), and `waves.tsv` is recomputed when that file already exists. A
  plan apply with no live run writes `tasks.tsv` and the task directories, and `groups.tsv`
  when membership changed. A removed task's directory moves to `tasks/.removed/<id>-<pid>/`.
  `groups.frozen` becomes the marker `stale: plan changed by proposal <pid>`, so `run`
  refuses until `forge-parallel.sh plan` validates, routes and freezes again. With a live
  run, a plan proposal that includes an edit, a removal or a dep change is refused, and
  nothing is written. A proposal that only adds tasks queues each one with
  `forge control add`, in dependency order. A task added that way joins no group. It needs
  dwarf and qa specs copied from a task that already has them. If a later add is refused,
  the tasks already queued stay, and the proposal is `approved` so it is not applied again.
- **Affected tasks.** After a successful apply, every task the proposal changed or added, and
  every task that depends on a changed, added or removed task, directly or further down, gets
  `tasks/<id>/brief.affected`. The marker is `{pid, reason, t}`. `reason` is `changed`,
  `added` or `dependent`. An unstarted task's questions are regenerated, one brief dispatch
  each (`write --force`). An answer whose question text is still in the new brief is kept.
  Any other answer moves to `brief.answers.prev.json`. A task that has already started gets
  the same marker with `flag_only`, and its questions stay. `forge brief show` lists a marked
  task as `affected by <pid>`, and adds `flagged (already started)` when `flag_only` is set.
  `apply --no-regenerate` writes the same markers and does not dispatch. Regenerating one
  task later is `forge brief write PLAN --task ID --force`. The next approved proposal that
  touches a task replaces that task's marker. Earlier proposals stay in `proposals/`.
- **Stale.** After a task merges, a merge that changed files outside that task's declared files
  flags the open questions of its dependents that have not started (`possibly_stale`). `forge
  brief stale PLAN TASK` does this by hand.
- **Model.** `--brief-model SPEC` is required with brief on, and it must resolve; otherwise
  `plan` exits `2`. forge never picks the model.
- **Privacy.** A never-send repository gets no brief dispatch (`never-send`), so its tasks have
  no questions and nothing holds them.
- **Off.** With `--brief off`, no setting, or `FORGE_BRIEF=off`, there is no writer, no hold and
  no prompt addition. `forge-prompt.py` adds nothing without a `brief` file. `forge brief write`,
  `forge brief show` and `forge brief propose` exit `3` for such a plan.

Files it adds: the plan files `brief`, `brief_scope` and `brief_model`; `brief.out` (the
writer's output), `brief.pid` (while the writer runs) and `brief.groups.noted` (once, when
wave or plan scope has no groups); `proposals/<pid>/proposal.json`, `diff.patch` and `hash`;
and per task `tasks/<id>/brief.json`, `brief.parked.json`, `brief.answers.json`,
`brief.late.json`, `brief.parked.answers.json`, `brief.affected` and `brief.answers.prev.json`.
A removed task's directory is moved to `tasks/.removed/<id>-<pid>/`. Its events are
`brief_question`, `brief_answer`, `brief_diff` and `side_dispatch` (kind `brief` or
`brief-change`). A `brief_diff` event is `{pid, scope, group, state, tasks}` and is written
when a proposal is proposed, refused, approved or rejected. The approved event from a
successful apply also carries `affected` and `flagged`, the task ids it marked.

## Task summaries (opt-in)

`plan --summaries on --summaries-model SPEC` writes a short summary of each task to
`tasks/<id>/summary.json`. The model sees only the task's facts: its status, its attempt trail
(attempt, dwarf spec, verdict, findings count, worst severity, retry reason), the latest review's
finding titles and severities, its known issues, and its dependencies and dependents. The facts
are capped at 8 KB. The model never sees the repository, the diff or the task prompt.

- **Cost.** One read-only dispatch per task, at effort `low`, when its facts change. Unchanged
  facts are not dispatched again, because `inputs_hash` matches the stored summary. Each kind
  of side dispatch is capped per run at `max(10, 3 × tasks)`.
- **When.** During `run`, the scheduler starts a detached `forge summarize PLAN --task ID` for
  each task whose status, dwarf reply, attempt trail, verdict or retry count changed since its
  last look. The first look at a task only remembers it. The scheduler never waits for a
  summary, and a failed one changes nothing in the run. On a finished run, `forge control set
  PLAN summaries on` starts `forge summarize` itself, with its output in `summaries.out`.
- **Output.** `summary.json` holds `text`, `sentence`, `model`, `generated_at`, `inputs_hash`
  and `sources` (the attempts and findings the facts came from). The prompt asks for 3 to 5
  short lines. A reply that is not a JSON object with `text` and `sentence`, or whose text runs
  past 8 lines, is rejected: the previous `summary.json` stays and `summary.error` says why.
- **Commands.** `forge summarize PLAN [--task ID] [--force] [--json]`. `--force` dispatches even
  when the facts did not change. `--json` prints one object per task: `task`, `status`, `reason`.
  `forge control set PLAN summaries on|off` sets the plan. `--task ID on|off|inherit` sets one
  task; its own `on` or `off` overrides the plan, and `inherit` drops that override.
- **Model.** `--summaries-model SPEC` is required with summaries on, and it must resolve;
  otherwise `plan` exits `2`. Set a user default with `forge config set summaries.model SPEC`.
- **Off.** With `--summaries off`, no setting, or `FORGE_SUMMARIES=off`, there is no dispatch and
  no `summary.json`.

Files it adds: the plan files `summaries` and `summaries_model`; `summaries.out`; and per task
`tasks/<id>/summary.json`, `summary.error` (after a failed reply) and `tasks/<id>/summaries`
(`on` or `off`, the task's override). Its events are `side_dispatch` (kind `summary`) and the
`side_summary` role in the dataset.
