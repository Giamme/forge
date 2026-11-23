# Reviews, retries and recovery

[← README](../README.md) · [All docs](README.md)

What a reviewer must answer, what forge does when it fails a task, how to retry, tolerate or accept findings, and the checks forge runs around the reviewer.

## Verdicts and failure

Decompose-mode QA must end with a sentinel line:

```
FORGE_VERDICT: PASS   — no CONFIRMED finding at the blocking threshold (P2 or worse)
FORGE_VERDICT: FAIL   — at least one CONFIRMED finding at the blocking threshold (P2 or worse)
```

forge decides the verdict from the findings, at a blocking threshold: `P2` unless
`--qa-threshold` sets another. A CONFIRMED finding at the threshold or worse, or one with no
severity, blocks: a PASS that lists one becomes FAIL. Reporting failing tests caused by the
change blocks the same way: a FAIL whose reply reports failing tests (`FORGE_TESTS: RAN —
FAILED: <names>`, `FORGE_TESTS: FAILED — <names>`, or `FORGE_TESTS_FAILED: <names>`) stays FAIL
even when every CONFIRMED finding is below the threshold, and a PASS that reports failing tests
becomes FAIL. The prompt asks the reviewer to file tests failing because of this change as a
blocking CONFIRMED finding at the threshold or worse, while pre-existing unrelated failures stay
in prose. A CONFIRMED finding below the threshold never blocks on its own:
a FAIL made only of those (with no failing tests reported) is accepted, and either way they are
recorded in `known_issues.md`, which `forge issues` and the sweep pick up. PLAUSIBLE findings never
block. `--qa-threshold P3` (or `none`) makes every CONFIRMED finding block.

Only a standalone final line counts, and the parser is shared by every runner (solo,
decomposed, Fractal's QA checkpoint). The prompt shows each verdict with a dash and its
meaning, and some reviewers copy that whole line, so `FORGE_VERDICT: PASS — meaning` is
accepted, as are trailing whitespace and CRLF line endings. Prose or a quoted marker earlier
in the reply is never a verdict. A missing verdict is `UNKNOWN` and is treated exactly like a
failure — excluded from integration and flagged. Merging a diff whose reviewer never reached
a conclusion would defeat the point of reviewing it, so the ambiguous case fails safe.

Before the verdict, a reviewer says whether its tests ran: `FORGE_TESTS: RAN`,
`FORGE_TESTS: RAN — FAILED: <names>`, `FORGE_TESTS: NOT NEEDED — why` or
`FORGE_TESTS: COULD NOT RUN — why`. Plain `RAN`, `NOT NEEDED` and `COULD NOT RUN` never change the
verdict, while `RAN — FAILED: <names>` (or a `FORGE_TESTS_FAILED: <names>` line) stops the gate
from accepting a FAIL and turns a PASS into FAIL. `COULD NOT RUN` prints a note, and forge-ui and
the dataset record the status, so a review that judged the diff by reading alone can be told apart.
Markup and any separator are accepted, and a line that says something else is recorded as `unclear`.
An older reply has no such line.

A failing task is excluded and **its branch and worktree are preserved**; the rest of the run
proceeds. forge never loops a dwarf against its reviewer on its own: a retry happens when you
type `retry`, or when you opt in for a run with `--retry-failed N` ([below](#retrying-inside-a-run---retry-failed-n)).

An `UNKNOWN` task is different from a rejected one: the reviewer never judged the code, so
`retry` (or `--retry-failed`) asks **QA again on the unchanged work** instead of paying an
implementer to rewrite something nobody rejected. No attempt is spent and no dwarf runs. If
the worktree no longer matches what was reviewed, the full task runs instead. A task
`INVALIDATED` because its review copy changed during the review (the reviewer edited a tracked
file) is retried the same way: its verdict counts for nothing, but the committed work is intact.
Untracked files a reviewer leaves in its copy, such as a test run's `__pycache__/`, do not void
the review.

### A large task diff

A task review inlines its diff up to 400 KB, the whole-run review's limit. Over it, the prompt
holds the diff's stat and the reviewer reads the diff in its working copy, which holds the attempt
as one commit over its base (`git diff HEAD~1 HEAD`), as the whole-run review and `adopt` already
do. If the reviewer's harness still refuses the prompt as too large (codex's `input_too_large`,
claude's "Prompt is too long"), the task ends `ERROR` with a note that names the cause and the way
out: a retry would send the same prompt, so review the commit with `forge-parallel.sh adopt`, or
split the task.

## Retrying a failed task

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
- **Refuses to re-review identical code.** If the retry produces a byte-identical diff
  against an unchanged brief, the task fails again without spending a QA dispatch. A retry that
  only rejects the findings, and leaves both the diff and the brief unchanged, writes
  `tasks/<id>/noretry` and QA is not dispatched. Clarifying `tasks/<id>/prompt.md` or
  `approach.md` (or changing `qa_threshold`) before retrying lets QA re-review even unchanged code
  against the updated brief.

When the diff did change, QA on that retry is handed the previous round as well as the
cumulative diff. The report goes under **Findings from the previous review round**. The
diff between the two attempts goes under **Changes since that review**, and is kept as
`tasks/<id>/changes.since.diff`, `changes.since.range` and `changes.since.stat` (the
range is omitted when the earlier commit is missing, unknown or not an ancestor, or when
nothing changed; a diff over 100 KB is replaced in the prompt by its stat). The
implementer's reply goes under **The implementer's reply (its own account; verify
every claim against the code)**. The prompt tells the reviewer to restate only findings that are still
open, as `- [Px][CONFIRMED|PLAUSIBLE]` lines, and to mention a fixed or rejected finding
only in a plain sentence. The gate and `forge issues` count every labelled line.

`retry` is something you run. Forge never loops a dwarf against its own reviewer on its own
initiative — unless you ask a *run* to with `--retry-failed N`, see
[retrying inside a run](#retrying-inside-a-run---retry-failed-n). A task whose reviewer
never gave a verdict (`UNKNOWN`) or whose run stopped on an infrastructure failure (`INFRA`)
resumes at the review stage instead: QA runs again on the unchanged work, no dwarf runs, and
no attempt is spent.

## Retrying inside a run: `--retry-failed N`

```bash
forge-parallel.sh run <plan-dir> --retry-failed 2
```

A task whose reviewer says `FAIL` (or `UNKNOWN`) is re-queued at once, **inside the same
run**, up to `N` more times — with the reviewer's findings carried into the next prompt exactly
as `retry` does. Other tasks keep running meanwhile; dependents still wait for the task to
*merge*, so nothing downstream starts early. Without this, a failed task waited for the whole
run (verification included) to finish before a human could retry it.

It is opt-in and per invocation: it is not remembered by the plan, and the orchestrator never
adds it unprompted. Reviewer rejections, reviews with no verdict and reviews voided by a
change to the review copy are retried automatically (the last two as a fresh review of the same
work) — `TIMEOUT`, `ERROR`, `CONFLICT` and an `INVALIDATED` merge (the task or integration
branch moved) still need a human. A retry that changes nothing (empty diff, or
a diff byte-identical to the previous attempt) writes a `noretry` marker and is **not**
retried again, so a stuck task cannot burn its whole budget repeating itself.
`tasks/<id>/attempts.tsv` records every entry (epoch, attempt number, resulting status, infra
class).

### A budget per model: `--retry-per-model N`

`plan --retry-per-model N` (1 to 99) writes `retry_per_model`. `--no-retry-per-model` removes
it; a re-plan that names neither keeps the file. A task may fail N times on the model it is
running. After those failures the next attempt moves one rung up the pools given to `plan`,
and the count starts again. With `--escalate-effort`, the same N applies to each effort of
that model before the next effort. This replaces `--escalate-after` for the plan. When
`escalate_after` is also present, `run` prints `retry-per-model N replaces --escalate-after
for this plan` and the policy follows `retry_per_model`.

Automatic retries still need `--retry-failed` above 0. Any positive budget turns the ladder on;
that number is not the limit. A person's decision, `--max-attempts`, the converging stop, a
`noretry` marker and `escalation.stop` still apply. `forge-route.py per-model` says whether one
task may run again. `worst-case` is what the plan table prints: `retry per model: N`,
`worst case with retry-per-model N: X dispatches` (implementer, reviewer, and shadow reviews
when those are on), and, when the cap cuts a ladder short,
`max-attempts … stops these tasks before the top of their ladder`.

Pools `low`, `medium` and `high` with N=3 give a task that starts on `low` up to 9 implementer
attempts, three on each rung. `--max-attempts` bounds that. The default cap is 8, so the table
names the task unless `plan --max-attempts` raises it.

### Failures that are the plan's

Two outcomes are not a further try of the model.

**Reason `plan`.** Task, solo and group reviews ask for this brace. Sweep, adopt and final
reviews do not. A finding whose only fix is a file the task did not declare ends its first line
with `{outside=<path>}`, several paths comma-separated, in its own braces immediately before
the facts brace:

```
- [P1][CONFIRMED] path/file.py:123 — {outside=a.py,b.py} {reach=main origin=new evidence=ran}
```

The implementer does not edit that file. It finishes, then ends its message with one
`FORGE_BLOCKED: <path>: <why>` line per path. `qa.input` shows those lines as a claim ("The
implementer says these fixes need files outside the task") and tells the reviewer to confirm a
real one with `{outside=<path>}` or refute it. Only the reviewer's brace stops the task. When
the status is `FAIL`, the verdict in `qa.last` is `PASS` or `FAIL`, every blocking finding
carries `{outside=…}`, and each path is outside the task's declared files, the decomposed task
stops as `NEEDS_DECISION` with reason `plan` after that attempt, before any escalation. A group
member's settled `FAIL` is checked the same way. A solo review is not.
`tasks/<id>/outside.json` lists each path with the task that declares it, or none, and that
task's status. `decision.md` prints `path — declared by <id> (<status>)` or
`path — declared by no task`, then the plan choices: add the path to this task's `files`, make
this task depend on the owner, or merge the two rows. Edit `tasks.tsv`, run `plan` again when
no run is live, then `retry --decide retry=1` with `--reason` and `--approved`.

That `FAIL` counts toward `--max-attempts`, because a review happened. It does not spend a
further attempt on the model: escalation does not run, and `--retry-failed` does not re-queue a
`NEEDS_DECISION` task. Another dispatch cannot change a file this task does not declare. A
tolerated or `PLAUSIBLE` finding does not block, so it does not count toward the stop. A later
decision restarts the escalation streak past this attempt.

**`STALE`.** `prompt.dispatched` and `approach.dispatched` are the copies taken when the
attempt is dispatched. A missing `approach.md` is an empty copy. If `prompt.md` or `approach.md`
differs at review time, the attempt is not reviewed. The status is `STALE`, the diff is
`stale.diff`, `control.log` records `prompt-changed` with actor `scheduler`, and the console
says the attempt was not reviewed and no attempt was spent. This spends nothing against the
model: the implementer was not judged on the text it was given, so the result says nothing
about it. The attempt counter is refunded, the cap skips `STALE` the way it skips `INFRA`, the
escalation streak treats `STALE` as neutral, and no decision is written. The scheduler
re-dispatches at once, unless the run is stopping, with `retry_findings.source` `prompt`.
`retry_findings.md` holds the new requirements and the diff, and the attempt number is reused.
A `STALE` task still waiting at the next `run` is prepared the same way. That re-dispatch does
not need `--retry-failed`. `forge control retry` accepts `STALE`; `--dwarf` is refused, because
the same implementer gets the new text. A plain `forge-parallel.sh retry` does not take this
path. The stale attempt's `changes.diff` is removed, so a
re-dispatch that keeps the committed work is reviewed rather than refused as identical. The
dataset attempt event has `prompt_changed`. `forge dataset routing`, tiers and the forecast
leave those attempts out. The `prompt` blob is the text the implementer was given.

## Infrastructure failures are not task failures

A quota or usage limit, an auth problem, a rate limit, a network error, or a harness that
returns nothing at all says nothing about the work. forge classifies those from the dispatch
log (`forge-runtime.py classify`) and `forge-dispatch.sh` exits **8** instead of 4. A
decomposed task then ends as **`INFRA`**, not `ERROR`/`FAIL`, and:

- **no attempt is spent** — a dwarf-stage stop refunds the counter; a QA-stage stop keeps the
  dwarf's committed work and re-runs **QA only** next time (`tasks/<id>/resume`, revalidated
  against the branch tip and fingerprint before it is trusted);
- the scheduler pauses only what the stop is about: a **rate limit** pauses that model on that
  harness, a **quota** or **auth** stop pauses that harness, and anything else (network, an
  empty answer) pauses everything, letting running tasks drain. A task whose next dispatch
  needs a paused model or harness waits; the others keep running. Unstarted tasks stay
  `PENDING` (never `BLOCKED`), and once nothing else can run `run` exits **8**: fix the cause,
  then run the same command again;
- `run --infra-retries M` waits instead — up to `M` waits per paused scope, using the provider's
  retry-after hint when it gave one (capped at 6 hours) and otherwise the backoff shape — and
  resumes that scope's tasks in the same run. `--infra-backoff` picks the shape: `exp:60` by
  default (60 s doubling to 30 min; `FORGE_INFRA_BACKOFF` sets that base), `exp:<base>,<cap>`,
  `linear:<start>,<step>[,<cap>]` or `list:<a>,<b>,...`. `forge control set infra-backoff`
  changes it for the next wait, and `forge control show` (and forge-ui) list each pause with
  the time left;
- `run --infra-fallback` moves an implementer stopped by a rate limit to the next model of its
  own tier pool that nobody paused, at once and with no attempt spent (recorded in
  `escalations.tsv` with source `fallback`); a pinned task, or a pool with no healthy member,
  waits as above;
- verification is skipped for a paused run (it is unfinished); the next `run` verifies at its end.

For a planned outage, `forge control drain <plan>` lets the running tasks finish and starts
nothing new until `forge control resume <plan>`, inside the same run.

`.infra` (`dwarf.infra`/`qa.infra`) holds `class=`, `retry_after=`, `detail=`, `rc=`,
`role=`, `harness=`, `model=`. Detection is deliberately strict: strong phrases only, and ordinary text that merely
mentions "rate limit" in a reply that otherwise succeeded is never classified. A false
positive costs a pause, not a spent attempt. Under Fractal a dwarf-side infrastructure stop
still collapses to Fractal's own "failed" handling; QA-side stops propagate as above.

## Severity, thresholds and known issues

QA is asked to label every finding `- [P0..P3][CONFIRMED|PLAUSIBLE] file:line — what breaks`
(P0 data loss/security/crash on the main path, P1 wrong behaviour on realistic input, P2
edge-case bug, P3 minor). [The verdict rule](#verdicts-and-failure) says which of them block.
Task and solo reviews ask for every CONFIRMED finding, and task reviews also ask the reviewer
to say so plainly when the diff is correct.

`--qa-threshold Pn` (on `plan`, `run`, `retry`, `review` and solo; a plan remembers it) changes
the default `P2`, the **least severe level that still blocks**: `P2` means P0–P2 block and only P3
is tolerated. `none` means P3, so every CONFIRMED finding blocks; `default` removes the setting.
Solo is the exception: it takes only `P0`–`P3` or `none`, so `default` exits 2 there.
A task-specific `tasks/<id>/qa_threshold` file beats the plan-level value, which is how a
dev-tooling or investigation task gets a looser bar than production code. A reviewer `FAIL` made
only of CONFIRMED findings that are all labelled and strictly milder than the threshold is
accepted as **PASS with known issues**: the task merges, `tasks/<id>/known_issues.md`
keeps the findings, `qa.gate` records the decision (`accept`), and every run/retry/integrate summary
lists them. The literal status stays `PASS`, so nothing downstream needed a new vocabulary.
The same gate runs the other way: a PASS that lists a blocking finding becomes FAIL (`qa.gate`
decision `fail`), and the next attempt starts with a note saying why.
It fails safe: an unlabelled CONFIRMED finding, or a FAIL with no CONFIRMED finding at all,
is never tolerated.

Every finding also ends its first line with three facts, `{reach=main|realistic|unusual
origin=new|existing evidence=ran|traced|suspected}`: how likely the trigger is, whether this change
introduced the problem, and how the reviewer knows. They are **recorded in shadow** and decide
nothing: the gate, `forge issues`, the ledger and forge-ui read a finding with the suffix exactly as
one without it, and a missing or malformed suffix changes nothing. They go to the run dataset, and
`forge dataset facts` says whether a gate on them would do better than the severity line alone: how
often two reviewers of one attempt agree on reach versus severity (with `--shadow-qa`), and how many
reviews and rounds rules such as "unusual reach does not block unless P0" or "existing problems do
not block" would have changed, listing the findings they would have let through so a person can
judge them. See [the dataset reference](../references/dataset.md#finding-facts).

Tolerated findings are recorded, not fixed. After a run, the skill offers once to file them as
GitHub issues, together with PLAUSIBLE findings of passed tasks and whatever a whole-run review
left open (`--file-issues ask|auto|off`, `ask` by default). Where the
[gh-proman](https://github.com/Giamme/gh-proman) skill is set up for the repository, it files
them on the team's project board; anywhere else forge files them itself with plain `gh`:

```sh
./forge issues <plan-dir>                       # drafts only: titles, severity, already filed
./forge issues <plan-dir> --include-plausible --apply --label bug
```

Each issue gets the finding, its severity, the task, reviewer and a code link at the merged
commit. A hidden fingerprint, shared with gh-proman, keeps a finding from being filed twice.
Findings a person merged with `accept` are origin `accepted`: the CONFIRMED lines under
`## Accepted by a human despite the reviewer's <status> verdict` in `known_issues.md`, or under
`## Accepted by a human despite forge's FAIL (…)` when `accept` followed a PASS that forge made FAIL.
Their issue says a person accepted the work over that verdict, and gives the recorded
reason. Findings above that heading, which a threshold tolerated, stay origin
`tolerated`. A whole-run review whose integration branch has moved since it is outdated.
Its findings stay listed, and the issue says the review is outdated and names the tasks
that merged after it. In `--json` each whole-run finding carries `review`: `name`,
`outdated` and `merged_since`. `outdated` is null when the review has no verdict or the
plan has no accepted integration commit, and `merged_since` is empty unless the review
is outdated. Every other finding has `review` null.

## Fixing them in the run: the finding sweep (opt-in)

`run --sweep P3` fixes the tolerated findings while the run's context is fresh, instead of leaving
them for issues. Once every task has merged, and before the combined verification, forge plans one
sweep:

- **Which findings.** The CONFIRMED findings the threshold tolerated, at `P3` or worse, and
  PLAUSIBLE ones too with `--sweep-plausible`. Left out:
  - findings a person accepted;
  - dismissed ones (`forge issues --dismiss`), and ones with no file or whose file is gone;
  - ones whose code a later task changed after the review.
- **How many tasks.** One fix task per file, worst first, at most 8 findings each and
  `--sweep-max N` tasks (default 5). They run in parallel up to `--max-parallel`.
- **Which models.** Each fix task's implementer is your low tier (`--dwarf-low`, else `--dwarf`),
  or the implementer of the task the finding came from. Its reviewer is that task's reviewer.
- **The plan.** It lists the groups, the findings, the dispatches and everything left out with
  its reason. It is printed and kept in `<plan>/sweep/plan.md`.

A fix task's review is narrow. The reviewer says, for each finding, whether the change fixed it
(`S1: FIXED`, `S2: NOT FIXED — why`), and reports only regressions the change introduced. It passes
when at least one finding is fixed and nothing blocking broke. A finding the sweep fixed is no
longer offered as an issue. One it did not fix still is, and so is anything a sweep review found:
there is only one round. A fix task that fails does not fail the run.

The sweep uses the same `--retry-failed` budget and controls as the rest of the run, and a plan is
swept once. A run without `--sweep` behaves as before. The cost is at least two dispatches per file,
plus retries, spent on findings the threshold had said could wait, and changes land right before
the final review. That is why it is off by default.

`run --sweep-ahead` is the cheaper first step and needs no extra dispatch. A task dispatched after
others merged is told the open findings in the files it owns, at most three, to fix only when its
change already goes through that code. Its reviewer is told the same.

## Accepting a task despite its reviewer

```bash
bash scripts/forge-parallel.sh accept "$PLAN" tooling \
  --reason "only an unreachable option left; tracked in the backlog" --approved
```

When a reviewer keeps finding new corners of something that should ship (one real
investigation harness got 12 findings over five rounds, and only a human decision ended it),
`accept` is that decision made explicit instead of done with git plumbing. It needs **both**
`--reason` and `--approved`, because it overrides a reviewer; an orchestrating model never runs
it on its own. It accepts only a `FAIL` or `UNKNOWN` task whose reviewed work is **intact** —
the branch is still at the reviewed commit and the worktree still has the saved fingerprint —
otherwise it exits `3` and says to `retry` for a fresh review (accepting would sign off on code
nobody reviewed). It writes `tasks/<id>/known_issues.md` (your reason, who and when, and the
reviewer's reply copied verbatim, under
`## Accepted by a human despite the reviewer's <status> verdict`, or after forge's flip under
`## Accepted by a human despite forge's FAIL (…)`) and `accepted.by` (with `gate=fail` after a flip),
merges through the same checks as any other
task, and puts everything back if the merge conflicts (exit `6`). Dependents are not touched;
`run` continues them.

## Whole-run review (opt-in)

Per-task QA only sees one slice. In one real run, a holistic review of the combined result still
found two confirmed P2 defects in code every per-task review had passed. It is opt-in because it
spends QA quota and `integrate` made no model calls before:

```bash
bash scripts/forge-parallel.sh review "$PLAN" --qa opus              # read-only look at the combined result
bash scripts/forge-parallel.sh integrate "$PLAN" --approved --final-review opus   # and gate the merge on it
```

The reviewer gets the overall goal, every task's complete requirements (bounded to about 80 KB,
truncated visibly), what forge observed that no per-task reviewer saw (accepted known issues,
scope drift, guard notes, the verification result including a `FLAKY` pass and coverage
warnings), the combined diff (inline up to 400 KB), and the same labelled-findings contract —
with an emphasis on cross-task interactions, interface mismatches and ordering races. `review`
reads `integration.base..integration tip` from a disposable checkout, writes
`<plan>/final-review-<epoch>-<pid>/` (`qa.input`, `qa.last`, `verdict`, `qa.gate`, …), keeps
`<plan>/final-review` and `final-review.verdict` pointing at the newest, and never touches your
branch or the integration branch. With `integrate --final-review`, it runs after verification
and before your branch can move: `PASS` (or a `FAIL` the threshold accepts) continues,
anything else stops with exit `5`; an infrastructure stop exits `8`. The QA spec is `--qa`, else
the one remembered in `final_qa`, else the single spec every task shares (otherwise it asks).
Tasks that are not merged are not in the result, and it says so. `integrate` also names unmerged
tasks even without a review.

A later whole-run review carries the previous one's report, under **Findings from the
previous final review**, and the diff since it, under **Changes since that review**
(inlined up to 200 KB, otherwise its stat). `review.meta` records `integration=` (the
accepted integration commit, or `none`), `previous=` (that review's directory, or
`none`) and `since=` (its tip, when that commit is an ancestor of this review;
otherwise `none`, and the prompt says the range cannot be shown). When the two reviews
share a commit, `since=` is that commit and the prompt says nothing changed. The same
labelled-line rule applies: restate only what is still open, because every labelled line
is filed.

## Group review (opt-in)

`plan --groups waves --group-review` reviews several related tasks in one QA dispatch. It is
off unless you pass it. A planner's groups, and `--groups` without `--group-review`, are names
only (`review=task`): they show in the approval table, and each task is still reviewed on its
own. The mechanics are in
[Groups and group review](../references/decompose.md#groups-and-group-review-opt-in).

**Fewer dispatches, a larger prompt.** A group is one review instead of one per member, and
the reviewer sees those tasks together: each task's requirements, and the combined diff. The
prompt is larger. That is the tradeoff.

**A failure stops the group.** A blocking finding sends the implicated members back. The
others stay implemented and are not retried, and nothing in the group merges until those
members are fixed and the group passes. A member also waits for its siblings before anything
merges, so a task that depends on one of them starts later than it would under per-task review.

**Fewer dispatches are not a measured saving.** They do not by themselves prove a lower token
cost, or that the same defects are caught. The group's QA dispatch is recorded with `scope`
`group` and is left out of the per-task reviewer statistics. Read the `group_review` and
`group_merge` events. Measure with `forge dataset` before treating group review as a cost
saving. See [the dataset](../references/dataset.md#raw-events-not-training-examples).

**When to use it.** Many small, related, low-risk tasks. Keep a high-risk task on per-task
review. `--group-review` already leaves high-difficulty tasks out of the groups it derives.

A combined diff over `group_max_diff_kb` (256 KB unless the plan says otherwise) is not
reviewed and not truncated: the group stops for a decision. That is separate from a
[whole-run review](#whole-run-review-opt-in), which reads the combined result after the run
and does truncate. Group review runs no shadow review, does not continue a reviewer session
across rounds, and does not run under Fractal.

## Reviewer session (opt-in experiment)

`plan --reviewer-session` continues the same reviewer's session across rounds. It is off
unless you pass it; `--no-reviewer-session` turns it off again and removes the plan's
`reviewer_session` marker. While the marker is present, per-task QA and a repeated
whole-run review continue the previous round's session on claude, openclaude and codex,
when the resolved harness, model and effort are unchanged. The approval table names it.
Dispatch details are in
[continuing a reviewer session](../references/harnesses.md#continuing-a-reviewer-session).

`plan --reviewer-session-rate R` turns it on for a sampled share of the tasks only, to compare
the two halves in one run. Within each difficulty, `plan` picks round(R × n) tasks by a hash of
the run id and the task id, and writes them to `reviewer_session_tasks`. A re-plan keeps the
same tasks. The other tasks review fresh every round, and their `review_sessions.tsv` rows say
`not-sampled`. Plain `--reviewer-session` on a re-plan goes back to every task. A whole-run
review is not sampled.

Any round that cannot continue starts a fresh session. That is the first round, a missing
or malformed session id, a resolution that is missing or different, any harness other
than those three, a Fractal plan, and a continue that the harness refuses or that fails
(dispatch exit **9**). Exit 9 means the session was not continued. Forge sends the same
prompt again as a fresh session, and that dispatch decides the round. One row per round
is appended to `tasks/<id>/review_sessions.tsv`: round, attempt, `continued` or `fresh`,
the reason, and the session id the round considered (empty when there was none). The
row describes the dispatch whose result counts, so a failed continue is a single
`fresh` row. Reasons are `same-spec`, `first-round`, `no-session`, `invalid-session`,
`spec-changed`, `harness-unsupported`, `fractal`, `continue-unsupported` and
`continue-failed`. A final review records the same outcome as `session=` and
`session_reason=` in `review.meta`.

A shadow review never continues a session. It stays a separate fresh review of a copy of
the prompt.

A review that was asked to continue is tagged `session_continue` in the dataset
(`continued`, or `failed` when the continue did not succeed) and left out of the
reviewer slices and the agreement rate. The fresh dispatch after exit 9 is not tagged,
so it still counts. See [the dataset](../references/dataset.md).

This is separate from a QA-only resume, the re-run after an infrastructure stop. That
re-run is the `tasks/<id>/resume` marker: forge revalidates it against the worktree,
then asks QA again about unchanged work, and no attempt is spent. The reviewer session
continues the reviewer's conversation into a later round, and only when you opt in.

## Shadow reviews: how much of a verdict is the reviewer?

`plan --shadow-qa <spec>[,<spec>...] [--shadow-rate R]` (default R = 0.1) sends a sampled
share of reviewed attempts to a second reviewer, the first listed one of another model
family than the task's. Its verdict goes to the run dataset and nothing in the run reads it,
so it never changes a status, merge, memory or retry. `forge dataset status` then reports how
often the two reviewers agree, overall and per model pair. The table shows the extra reviews
before you approve. The shadow runs beside the main review and is stopped soon after that
finishes, so the most it costs is reviews and a little latency. To measure what reviewer effort
buys, list the reviewers' own model at another effort (`--shadow-qa sol:medium` beside `sol:high`):
`forge dataset pairs` reports those pairs apart from model and family comparisons, and a shadow
identical to its primary is recorded as a control, never as an effort comparison. See
[decomposition](../references/decompose.md#shadow-reviews-opt-in).

## Implementer guards

The dwarf is told not to commit, switch branches, start background processes, or claim checks
it did not run. forge then **observes** whether it did, rather than trusting its report:

- **self-commit / branch switch** — HEAD or the checked-out ref moved during the dispatch;
- **orphan processes** — the harness runs in its own session; anything still alive in it after
  the harness exits is listed in `<role>.orphans` and stopped (`FORGE_ORPHAN_GUARD=off`
  disables this; it is also skipped under Fractal);
- **promised later work** — "running in the background", "will report back once it
  finishes" and similar in the final message.

These are warnings recorded in `tasks/<id>/guard.txt`, shown in the QA prompt ("Implementer
guard notes") and in the run summary. They never change a status: the diff and the reviewer
still decide. In solo mode a self-commit prints the `git reset --soft <old HEAD>` that would
undo it; forge never resets anything itself.

## Verification coverage and flaky reruns

`--verify` is whatever you hand it, and a gate that skips a suite the project defines lets a
regression there reach your branch silently. `forge-verify-coverage.py` reads the project's own
test configuration (`package.json` scripts, Makefile targets, pytest/tox, Cargo, Go, Ruby,
Elixir, `scripts/test*.sh`), follows aggregate scripts, and **warns** — before any model is
paid for, again in the run summary and at integrate — about suites the verify command never
runs, e.g. `suite 'test:contract' (npm run test:contract) is not run by the verify command`.
It is advisory: it never fails a run, and `verification.coverage.txt` keeps the lines. It is a
text heuristic (root of the repo only; a mentioned command counts as run).

A failing verify command is rerun **once** (`--verify-retries N`; `0` restores the single strict
run) when the source fingerprint is unchanged. If the rerun passes the status is `PASS` but
`verification.flaky` is written, `verification.log` keeps the first (failing) run,
`verification.retry.log` the second, and `run`/`integrate` print a loud `FLAKY` line — an
intermittent failure can be a real bug, so it is never passed off as a clean pass. A command
that changes the tree is never retried.

With [verification lanes](../references/decompose.md#execution-checks-and-integration-verification)
the rerun is per lane: only a lane that failed runs again, alone, and a lane that passes on its
rerun is `flaky`. The run is then a pass, but `run`/`retry`/`integrate` print
`FLAKY: lane <name> failed once and passed on rerun`, and `verification.flaky` records each
such lane (`lanes=`, `<lane>.first_exit=`, `<lane>.retries_used=`); a lane whose every failing
test is in its baseline prints `known failures only: lane <name>`. A whole-run review's
verification summary lists every lane with its state (`unit: pass`, `db: flaky`, ...) and says
again which lane failed, which passed only on a rerun ("so its pass is not clean") and which
failed only on known tests. In lanes mode there is no whole-command rerun.

## Native review

`--native-review` switches a codex reviewer to its purpose-built reviewer, for a diff too large
to inline. Know the trade: codex refuses a custom prompt alongside its scope flags, so the
reviewer never learns what the dwarf was *asked* to do. It can still judge whether the code is
correct, but not whether it's the right change, so its result is informational and the verdict
is `UNKNOWN`. See [solo runs](../references/solo.md) for how the baseline is passed.

## Fail-first evidence (opt-in)

`--fail-first` (on a solo run or `plan`) checks that the tests a change adds or edits can
actually fail. Before each review, forge runs each new or edited test file against the change
and against the base without it, using the per-file command in `.forge/verify-subset`, and hands
the reviewer the result: `proven`, `passes_without_change`, `inconclusive` or `error`. The
reviewer is asked for a P2 finding for every test that passes without the change. It is advice
only: forge never changes a status or verdict from it. forge-ui shows the evidence under the
review findings. See [fail-first evidence](../references/decompose.md#fail-first-evidence-opt-in).

## Pre-review tests (opt-in)

`plan --pre-review-tests` runs only the tests an attempt affects, with `.forge/verify-subset`,
before forge pays for its review: the tests the change adds or edits, and the tests of the files it
changes (from `.forge/test-map`, the naming convention or a reverse lookup). Never the whole suite:
a change to shared test infrastructure, more than 20 files or more than 120 s is skipped as too
broad. A red attempt goes back to its implementer without a review, with the failing tests and
their output as its findings; a test that also fails on the task base is not blamed, and a failure
that passes when rerun alone is recorded as flaky. Green, the reviewer gets the result as
evidence. See [pre-review tests](../references/decompose.md#pre-review-tests-opt-in).
