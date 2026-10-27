# Decomposed runs

Mechanics behind `--decompose-level`: how a goal becomes tasks, how tasks are routed to
dwarves by difficulty, how waves are computed, and what happens when something fails.

Read this when writing a decomposition, when routing does something unexpected, or when a
run leaves branches behind.

## Contents

- [The two axes: decompose-level and difficulty](#the-two-axes-decompose-level-and-difficulty)
- [tasks.tsv](#taskstsv)
- [Routing](#routing)
- [Fractal within decomposed tasks](#fractal-within-decomposed-tasks)
- [Waves and disjointness](#waves-and-disjointness)
- [Task size and acceptance criteria](#task-size-and-acceptance-criteria)
- [The capsule](#the-capsule)
- [Planning](#planning)
- [QA verdicts](#qa-verdicts)
- [Severity, thresholds and known issues](#severity-thresholds-and-known-issues)
- [Retrying, and resuming an interrupted run](#retrying-and-resuming-an-interrupted-run)
- [Infrastructure failures](#infrastructure-failures)
- [Implementer guards](#implementer-guards)
- [Accepting, splitting, combining and the whole-run review](#accepting-splitting-combining-and-the-whole-run-review)
- [Branches, worktrees, cleanup](#branches-worktrees-cleanup)
- [Failure modes](#failure-modes)

## The two axes: decompose-level and difficulty

These are independent and easy to conflate.

**`--decompose-level`** controls how finely the goal is split:

| Level | Split |
|-------|-------|
| `low` | only where the goal contains obviously independent pieces; 2–3 coarse tasks |
| `medium` | one task per coherent unit (module, feature slice); typically 4–6 |
| `high` | finest split where each task is still independently reviewable *and* independently correct; typically 7–12 |

At every level, two rules override the target count: never split so fine that a task cannot
be verified on its own, and never emit two tasks that must edit the same file — make it one
task, or add a dep. The counts are guidance, not quotas; a goal with three natural seams
produces three tasks at `high` too.

**`difficulty`** is rated per task, by what the task demands of the model — **not** by how
much code it produces:

| Difficulty | Means |
|------------|-------|
| `low` | mechanical and local: rename, move, docs, a test for behaviour that already exists. Little design judgment; a mistake is obvious. |
| `medium` | self-contained implementation against a clear spec; some design choices, bounded blast radius. |
| `high` | needs design judgment, touches cross-cutting behaviour, has subtle edge cases or state/concurrency, or the right approach is not obvious from the goal. |

A five-hundred-line mechanical rename is `low`. A ten-line concurrency fix is `high`. Sizing
by diff length instead of by judgment required is the main way difficulty routing stops
paying off — it sends the cheap model at the subtle problem and the expensive one at the
boilerplate, which is worse than not routing at all.

## tasks.tsv

Tab-separated, one row per task, written by whoever decomposes the goal:

```
# id  deps  difficulty  files  dwarf  qa  title
mul	-	high	src/calc.py	-	-	Add multiply() to the calculator
docs	-	low	README.md	-	-	Document the calculator API
tests	mul	medium	tests/test_calc.py	-	-	Tests for multiply()
```

| Column | Meaning |
|--------|---------|
| `id` | short slug, unique; also the worktree and branch name |
| `deps` | comma-list of task ids that must land first, or `-` |
| `difficulty` | `low` \| `medium` \| `high` |
| `files` | expected touch set, comma-separated. Drives disjointness — see below |
| `dwarf` / `qa` | `-` to resolve from the routing rules; an explicit spec always wins |
| `title` | one line, shown in the approval table and the capsule |

Each task also needs `tasks/<id>/prompt.md` — the implementation instruction, written the
same way a single-task forge goal is. `tasks/<id>/approach.md` is optional: a few lines of
intended approach, written by the orchestrator or by `--planner`. When present it is shown
in the approval table, prepended to the dwarf's prompt, and given to qa as the intent to
review against — so a review can say "this works but abandons the planned shape", which
matters when a sibling task was planned against that shape.

`files` is a promise, not a prediction: it is what the wave planner trusts when deciding what
may run concurrently, and it is what the capsule tells other dwarves not to touch. An
understated `files` is how two dwarves end up in the same file and produce a conflict.

The promise is checked. After each dwarf finishes, the runner compares what the task
*declared* against what it actually touched (a declared directory covers the paths beneath
it, so `src/routes` covers `src/routes/api.py`). Undeclared paths go into
`tasks/<id>/drift.txt`, into the run summary, and into the QA prompt — which already asks
whether the diff went outside the task's scope and previously had no way to know. It is a
warning and never a failure: a dwarf that genuinely had to touch one more file did the right
thing. What it buys is that a merge `CONFLICT` two waves later arrives with its cause
already named, instead of leaving someone to diff two branches to find out.

`plan` resolves difficulty into concrete specs and writes them back into the `dwarf`/`qa`
columns, so `run` never consults the routing rules. This is also what makes a gate override
durable: an explicit value is preserved across a re-plan, while `UNASSIGNED` is treated as a
leftover marker and re-resolved.

## Routing

```
--dwarf <spec>          fallback for any tier without its own rule
--dwarf-high <spec>     used for high-difficulty tasks
--dwarf-medium <spec>
--dwarf-low <spec>
--qa, --qa-high, --qa-medium, --qa-low     identical grammar
```

Each flag takes an ordinary forge spec (`alias[:effort[:harness]]`), so a tier can name a
model *and* the harness it runs through. A comma-list pools several models within one tier
and round-robins across them — useful when a tier has more tasks than one provider should
carry, since distinct harnesses have independent quotas.

Resolution order per task: explicit column value → the tier's rule → plain `--dwarf` → and if
none of those exist, `UNASSIGNED`. An `UNASSIGNED` task makes `plan` exit 2 and `run` refuse
outright. Forge does not invent a model when the user has not said which one should spend
their quota.

QA has no tier rules by default and falls back to `opus`, matching single-task forge.

### Exploratory routing (on by default)

Every task in the run history ran on the model its tier was routed to, so the history
says how that model did and nothing about how another would have done on the same task
(only 9 tasks ever ran on more than one spec). Choosing models from measured pass rates
needs some randomized picks, so `plan` makes a few: `--explore E`, with E between 0 and
0.5, defaults to **0.2**.

- **Which rows:** only rows whose dwarf column is `-` (left to forge). The pool's own turn
  is still taken, so the round robin of every other row is unchanged.
- **Two candidates:** the pool's pick and its *neighbour*, the model one tier away in the
  pools given to this plan: the next cheaper tier for medium and high rows ("is a cheaper
  model enough?"), the next stronger for low rows ("did it need more?"). Within a tier the
  round robin already alternates between pool members; across tiers nothing else ever
  produces data. The neighbour keeps the pick's position in its pool, so
  `--dwarf-low luna,haiku --dwarf-medium sol,sonnet` pairs luna with sol and haiku with
  sonnet. A different effort is a different candidate, so pools that differ only in
  effort (`--dwarf-low sol --dwarf-high sol:xhigh`) compare the two efforts. A row whose
  tier has no neighbour is not explored.
- **How often:** with probability E the row is drawn uniformly between the two, so it
  actually changes model with probability E/2 (10% at the default).
- **Never a model you did not name.**
- **The table:** an explored row is marked `*` in the approval table, and a footer states
  the active setting, so the spend is visible before anything runs. To pin another model,
  put its spec in the dwarf column and re-plan.
- **The record:** each decision goes to `tasks/<id>/route.jsonl` (append-only) with its
  two candidates, the pool's own pick, the choice, and its probability:
  `(1 - E)·[choice = pool's pick] + E / 2`.
- **Turning it off:** `--explore 0`. The choice is kept in the plan dir (`explore`), so a
  later re-plan neither turns it back on nor changes rows already resolved. Precedence:
  the flag, then the plan's earlier choice, then `FORGE_ROUTE_EXPLORE`, then 0.2.
- **Later changes:** a re-plan draws nothing for rows that are already resolved. An
  approval-gate edit or a `retry --dwarf` simply runs a different model; the analysis
  keeps such a task in the arm it was assigned and reports it as not followed.

`forge dataset routing` reads the draws back: per (tier, pool pick, neighbour) pair, each
arm's first-pass rate (inverse-propensity weighted, every task in the arm it was assigned),
attrition, runtime and a run-clustered interval, judged at the first of 30, 60 or 120
neighbour assignments it reaches. See [Dataset](dataset.md#routing). At the default rate
that is weeks of normal use per pair; a pair that matters can be given more with
`--explore 0.5` on its plans.

## Fractal within decomposed tasks

Select optional Fractal execution on `run`; planning the task table does not activate it.
`--decompose-level` defines the top-level Forge tasks and approval table. Each such task
can then use a Fractal implementation node with bounded children inside its declared `files`.
Each top-level task still returns its combined candidate to Forge's independent QA and
existing integration checks. Child completion never marks the Forge task accepted.

Use `--fractal-auto-decompose` on `run` to require recursive split-or-atomic decisions
automatically. Add `--fractal-planner <spec>` to use a stronger decision model for all
nodes; otherwise the saved top-level planner or each task's initial root model is reused.
Show that choice and the bounds in the approval table. See the
[decision contract](fractal.md#automatic-decomposition-decisions) for recovery and limits.

```bash
# With tasks.tsv, goal.txt, request.md and per-task prompts already prepared:
bash <skill_dir>/scripts/forge-parallel.sh plan "$PLAN" --repo "$REPO" --decompose-level medium \
  --dwarf sol:high --dwarf-low luna:medium --dwarf-high sol:xhigh,terra:xhigh --qa opus

# Preview the selected backend, pools, bounds and workspace without provider calls.
bash <skill_dir>/scripts/forge-parallel.sh run "$PLAN" --fractal --dry-run

# After approval of the task/model table:
bash <skill_dir>/scripts/forge-parallel.sh run "$PLAN" --fractal \
  --max-parallel 3 --fractal-concurrency 3 --fractal-depth 2
```

Add `--ui` to `plan` to open the plan in [forge-ui](ui.md): the `forge-ui:` URL is printed
under the task table, so the user can watch the later `run`. Ask once when the plan is made,
not on `run`, `retry` or resume; it is fail-open and not remembered.

`plan` saves tier pools in `fractal-routing.json`. The first enabled execution freezes
eligible specs, canonical resolutions and limits with the run and preflights every
eligible model/harness. Children route by difficulty, then the configured general dwarf,
then their parent's dwarf. A retry cannot introduce a model outside that frozen pool.

`--max-parallel` bounds top-level task pipelines. `--fractal-concurrency` separately bounds
all active model invocations across their trees, including QA. Parents release their model
slot while waiting; nesting cannot multiply that limit. Child dependencies must be satisfied,
ownership must remain inside the parent, and overlapping children are serialized.

The choice is persisted in `fractal-selection.json`. Explicit `--fractal` or `--no-fractal`
skips the once-per-interactive-run question; unattended execution without either is off.
Installation never activates the backend. The [Fractal reference](fractal.md) documents
all limits, prerequisites and the child-request protocol.

## Waves and disjointness

A wave is the largest set of tasks whose deps are all satisfied by earlier waves **and** whose
`files` sets are pairwise disjoint. Everything else waits.

Disjointness — not the task count — is what actually bounds parallelism. Two dwarves editing
one file produce a conflict no reviewer can untangle and no merge can resolve honestly, so
they are placed in different waves however the goal was decomposed. When this happens the
deferral is reported in the approval table:

```
deferrals (serialized to protect the merge):
  wave 1: lint deferred (file overlap with a task already in this wave)
```

That line matters: a decomposition that looks parallel but serializes at run time should say
so, rather than leaving the user to wonder why eight tasks took eight rounds.

Waves remain a planning preview. At execution time a Python 3 standard-library coordinator
starts a task as soon as every prerequisite is MERGED, capacity is available, and its paths
do not overlap an active task. Each passing task merges serially through the existing
fingerprint checks. A slow unrelated task no longer holds up eligible dependents.

Each task's baseline is pinned immediately before dispatch and stays fixed across retries.

## Task size and acceptance criteria

Size predicts a failed first attempt better than anything forge asks a model. In the run
history (269 first attempts, 2026-10-17), the share a reviewer passed:

| prompt.md | passed | | declared files | passed |
|---|---|---|---|---|
| under 3K chars | 62% (n=50) | | 1 | 83% (n=12) |
| 3–5K | 56% (n=48) | | 2 | 68% (n=25) |
| 5–7K | 51% (n=49) | | 3–5 | 52% (n=75) |
| 7–10K | 33% (n=49) | | 6–8 | 40% (n=50) |
| 10–14K | 28% (n=29) | | 9 or more | 29% (n=107) |
| 14K and over | 23% (n=44) | | | |

Size and difficulty are confounded (high tasks are longer), but splitting reduces both.
So:

- Split along independently verifiable behaviour and explicit interfaces, not to make more
  parallel tasks. A task should still be reviewable and correct on its own.
- End every `prompt.md` with an `## Acceptance criteria` section: observable checks the
  reviewer can verify (a behaviour, a command that passes, a file that exists), not
  restated goals.
- `plan` prints an advisory block under the table naming tasks at 7,000 characters or 9
  declared files and over, with the history for their size, and tasks with no acceptance
  criteria. A heading inside a code fence, or with nothing under it, does not count. The
  block never changes the table or the exit code.

## The capsule

Every dispatch is a clean slate — forge never passes `--continue`, `--resume`, or
`codex exec resume`, except the opt-in `plan --reviewer-session` experiment (it continues
a reviewer's session across rounds on claude, openclaude and codex). That keeps one task's
confused turn from poisoning another and keeps each task's context small, but it also
leaves each dwarf blind to the wider run.

`tasks/<id>/capsule.md` records ownership and baseline context. `baseline.capsule` preserves
the implementation-time capsule for QA. MERGED means the reviewed commit is an ancestor of
this task's pinned baseline, not merely that another task finished while QA was preparing.
Complete requirements, approach, retry findings and distinct memory facts are assembled once
per role; implementation and review instructions are separate.

It exists to prevent two specific failures that clean slates create, which the opt-in
`--reviewer-session` experiment does not change:

- a dwarf reimplementing a helper an earlier task already merged, because nothing told it
  that task was done;
- a dwarf editing a file another dwarf currently owns — producing exactly the conflict the
  wave planner exists to avoid.

For QA the capsule additionally scopes the review: code from `MERGED` tasks is already in the
base and is not this task's bug. Without that, a wave-2 reviewer reports wave-1 code as
defects in the diff it was handed.

Keep it short. It is paid for on all 2N dispatches, so it is a status table and a few rules,
not a design document.

## Planning

The capsule stops two dwarves editing one *file*. It does nothing about two dwarves
inventing incompatible *interfaces* at a seam they share: both stay inside their own
`files`, both pass their own QA, and the mismatch only appears at integration.

Writing the approach down before dispatch is what closes that. By default the orchestrator
does it while decomposing, which costs nothing. `--planner <spec>` dispatches it instead —
**one dispatch for the whole run, never one per task**, because the value is that a single
mind designed both sides of every seam. N independent planners would recreate the problem
they were meant to solve.

The planner runs with qa's permission profile: it reads the repo and writes nothing to it.
Its output becomes `tasks.tsv` plus each `tasks/<id>/approach.md`, and the approval gate
renders both — the last moment the plan can be changed for free.

## QA verdicts

Decompose-mode QA prompts require a final line:

```
FORGE_VERDICT: PASS    no confirmed correctness bug (style nits are not failures)
FORGE_VERDICT: FAIL    at least one CONFIRMED correctness bug
```

The runner accepts only a standalone verdict on the final nonempty line (the shared parser
also tolerates the dash gloss the prompt prints after each verdict, trailing whitespace and
CRLF). A missing verdict is `UNKNOWN` and is treated exactly
like a failure — excluded from integration and flagged. Merging a diff whose reviewer never
reached a conclusion would defeat the point of reviewing it, so the ambiguous case fails
safe rather than optimistically.

A failing task is excluded and its branch preserved; the rest of the run proceeds. There is
no automatic repair pass unless the run was started with `--retry-failed N`, consistent with
forge's rule that a dwarf → QA cycle does not loop on its own initiative.

The prompt contract (preamble, findings format, severity definitions and the verdict lines)
comes from one place, `scripts/forge-contract.py`, shared by solo, decomposed and the final
review; the implementer rules appended to every dwarf prompt come from the same module.

### Fail-first evidence (opt-in)

`plan --fail-first` (and `forge-solo.sh --fail-first`) checks that the tests a change adds
or edits can fail. Before each review, forge exports the reviewed commit and, separately,
the task's base plus only the change's test-path files (tests, their fixtures and helpers),
then runs `.forge/verify-subset` (the per-file test command with `{files}`, see
[Jev](jev.md)) once per new or edited test file in each. Every file gets one result in
`tasks/<id>/failfirst.json` (solo: `<run>/failfirst.json`):

- `proven`: passes with the change, fails without it;
- `passes_without_change`: passes both ways, so it does not prove the change;
- `inconclusive`: fails even with the change (a broken test or a missing dependency);
- `error`: could not run (export failure, or `FORGE_FAIL_FIRST_TIMEOUT`, default 600 s, per run).

The reviewer's prompt lists the results and asks for a `[P2][CONFIRMED]` finding for each
test that passes without the change, unless the reviewer explains why it need not fail
(a refactor kept green). It is advice: forge never changes a status or verdict from it, and
a P2 only blocks through the reviewer and the usual `--qa-threshold` gate. The exports are
throwaway directories outside the review snapshot, so no fingerprint changes. forge-ui
shows the evidence under the task's review findings, and each check is recorded in the
run dataset as a `fail_first` event with the JSON attached. Without
`.forge/verify-subset` the check records `skipped` and the prompt says nothing.
`plan --no-fail-first` turns it off again.

### Shadow reviews (opt-in)

`plan --shadow-qa <spec>[,<spec>...] [--shadow-rate R]` sends a sampled share of
reviewed attempts (R, default 0.1) to a second, independent reviewer. Its verdict is
recorded in the run dataset and never read by the run. It exists to measure how often two
reviewers agree on one diff, which is the noise in every review-based label. The approval
table shows the reviewers, the rate and the expected extra reviews, and `--shadow-qa none`
turns shadow reviews off again.

- **Which reviewer:** the first listed spec whose model family (Claude, GPT, Gemini, ...)
  differs from the task's reviewer, so the two do not share one blind spot. If none
  differs, the first listed spec. Forge only ever uses the specs you list. A spec that
  cannot run here (`doctor` fails) is dropped with a note and never fails the run.
- **Which attempts:** a deterministic sample per (run, task, attempt), so a QA-only
  resume decides the same way.
- **Same evidence, separate everything else.** The shadow reviews a byte-identical copy of
  the primary's prompt, in its own snapshot, from its own dir
  (`<plan>/shadow/<task>/<attempt>-<k>/`). Nothing reads that dir. It writes no status,
  memory, ledger, gate or retry findings, and a reviewer that edits its snapshot is
  recorded as `invalid`.
- **Bounded:** it runs beside the primary, and the task waits for it before writing any
  status. Once the primary is done, the shadow gets as long again as the primary took
  (at least 5 minutes), then it is stopped. It dies with its task, including on Ctrl-C of
  a manual `retry`.
- **Scope:** parallel mode only, and never under Fractal.

`./forge dataset status` reports the agreement rate, overall and per model pair. The export
adds `shadow` and `consensus` to the matching review cases' metadata.

## Severity, thresholds and known issues

QA labels each finding `- [P0..P3][CONFIRMED|PLAUSIBLE] file:line — what breaks (input)`:
P0 data loss, security hole, crash or wrong result on the main path; P1 wrong behaviour on
realistic input or a broken existing contract; P2 edge-case bug, unusual-timing race, missing
hardening; P3 minor. Severity is impact; CONFIRMED/PLAUSIBLE is confidence.

`run`/`retry --qa-threshold Pn` (remembered in `<plan>/qa_threshold`; `none` clears it) sets
the **least severe level that still blocks**. `tasks/<id>/qa_threshold` overrides it per task (the value `none` exempts the task from a plan threshold; `forge control set ... qa-threshold` writes both and takes effect at the next attempt) —
the way to give a dev-tooling or investigation task a looser bar than production code,
which otherwise gets a new finding in a new corner of a non-default option every round and
only a human decision ends it. The orchestrator decides *deterministically* (`forge-contract.py gate`),
it does not take the reviewer's word: with a threshold set, a `FAIL` becomes `PASS` with known
issues only when there is at least one CONFIRMED finding, every CONFIRMED finding carries a
`P0..P3` label, and all of them are strictly milder than the threshold. Anything unlabelled,
any blocking finding, or a `FAIL` with no CONFIRMED finding stays a failure. The task's status is
then the literal `PASS`; `known_issues.md`, `qa.gate` and the summary say what was tolerated.
Without a threshold none of this applies.

## Retrying, and resuming an interrupted run

A failed task keeps its branch and its worktree. `retry` is what acts on them:

```bash
forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>]
```

It re-dispatches that one task **in the worktree it already has**, with the previous
reviewer's findings prepended to the prompt — that carry-forward is the whole difference
between a retry and simply running the task again. It then re-reviews, and merges if the
verdict is now `PASS`. `--dwarf` escalates to a stronger model and is written back into
`tasks.tsv`, so the table shows the model that will actually be spent and the escalation
survives a re-plan.

Two details that are load-bearing rather than incidental:

- **The base is pinned per task, not per run.** A retry diffs against the commit the task
  was originally branched from, so its reviewer sees the task's *cumulative* work. Reviewing
  only the fix would let the first attempt's code through unread, and re-basing onto the
  integration branch as it now stands would put other tasks' merged code into this task's
  diff.
- **An identical retry is not re-reviewed.** If the diff comes back byte-identical to the
  previous attempt, the task fails again without a QA dispatch. Paying a reviewer to read
  the same code twice buys nothing. A retry that only rejects the findings, and leaves
  that diff unchanged, is still this identical retry.

`retry` is a command a human types. Forge does not loop a dwarf against its own reviewer on
its own initiative; the one exception is the opt-in `run --retry-failed N` below.

**`UNKNOWN` and `INFRA` resume at the review stage.** If the reviewer never gave a verdict (or
its dispatch stopped on an infrastructure failure after the dwarf finished), the work was not
rejected, so `retry` / `--retry-failed` re-runs **QA only** on the unchanged, already-committed
work: no dwarf, no new commit, no attempt spent. A `tasks/<id>/resume` marker records this and is
revalidated against the branch tip and the saved source fingerprint before it is trusted; if the
worktree changed, the full task runs. Findings are carried only from a real review.

On a retry whose diff changed, QA receives the previous round under **Findings from the
previous review round**, the diff since that round (`tasks/<id>/changes.since.diff`,
`changes.since.range` and `changes.since.stat`; omitted when the earlier commit is
missing or not an ancestor, or when nothing changed) and the implementer's reply. It is
told to restate only findings that are still open, as `- [Px][CONFIRMED|PLAUSIBLE]`
lines, because every labelled line is counted. That is the retry's review, not the
QA-only re-run above.

**In-run retries.** `run <plan> --retry-failed N` re-queues a task whose reviewer said
`FAIL`/`UNKNOWN` as soon as it ends, up to `N` more times in that run, preparing the retry exactly
as `retry` does (same findings carry-forward, same pinned base, same cumulative review). Other
tasks keep running; dependents wait for the task to merge. It applies per invocation (not
remembered), never to `TIMEOUT`/`ERROR`/`INVALIDATED`/`CONFLICT`, and never after a retry that
changed nothing (`tasks/<id>/noretry`). Every entry is logged in `tasks/<id>/attempts.tsv`.

**Escalation after repeated failures.** In forge's history the first three attempts at a task
pass review at about the same rate (43%, 42%, 47%), and the fourth and later at 29%, whether
or not the model changed. So a retry does not repeat a dispatch that has already failed three
times on one model (`--escalate-after N` on `run` or `retry`, kept in the plan, default 3, `0`
turns it off):

- **Counting:** the count is the task's failed attempts (FAIL, or an implementer TIMEOUT) on its current model,
  from the fifth column of `attempts.tsv`. Infrastructure stops, missing verdicts and
  forge's own errors judge nothing and are skipped; a pass or a change of model resets it.
- **Escalation:** at the threshold, the next attempt goes to a different model in the next tier
  up of the pools given to `plan` (same position in the pool, as with exploration),
  recorded in `escalations.tsv` and `tasks.tsv` like a `retry --dwarf`.
- **Stop:** when nothing is above (the model is in the top tier, or in no tier pool at
  all), the task stops with `tasks/<id>/split.suggested`: split it into smaller tasks, or
  name a model. `run --retry-failed` does not retry it, and a plain `retry` refuses (exit 2).
- **Effort step** (`--escalate-effort` on `run` or `retry`, kept in the plan as `escalate_effort`;
  `--no-escalate-effort` removes it; off by default): when no higher tier has another model, the
  same alias and harness is tried once more at the next effort its pairing accepts
  (`forge-dispatch.sh efforts --spec <spec>` lists them, weakest first), counted from the effort
  the last dispatch really ran after any clamp; a spec that names no effort counts as `medium`.
  The streak then counts per effort, so the new effort starts from zero. At the pairing's highest
  effort the task still stops, and `split.suggested` says that effort was tried too. Turning the
  step on (flag or `forge control set escalate-effort on`) clears the stop markers, so tasks the
  policy already stopped are decided again at their next retry.
- **Overrides:** an explicit `retry --dwarf <spec>` is the human's choice and skips the policy, even
  with the same model. The dwarf column itself cannot tell a pin from a pool pick, so it does
  not; `--escalate-after 0` does.
- **Fractal plans** keep their frozen eligible set and are not escalated; neither is a
  QA-only resume, which spends no implementer attempt.
- **Reporting:** the policy's state is decided after every attempt, so a run whose retry budget ran out
  still reports `its next retry goes to <spec>` or the split suggestion under the results.
  A pending pin is reported ahead of both, as `its next retry goes to <spec> (pinned)`, or
  `(pinned; after the QA-only re-run)` only when the next entry really re-runs QA (the
  `resume` marker, or an unreviewed verdict, still matches the worktree). A marker the
  next retry will discard is not that wait.

History cannot yet say whether escalating rescues a task (31% of the 29 later attempts on a
changed spec passed, mostly effort bumps); the attempts this policy makes are what will.

**Live controls.** `forge control show|set|pin|unpin <plan>` changes a run while it works:
`qa-threshold` (read once per attempt, so an attempt keeps the bar it started with; per task, `none`
exempts it and `inherit` drops the override), `timeout`, `escalate-after`, `escalate-effort`
(`on|off`), `verify-retries`, `infra-backoff` (a shape as for `--infra-backoff`; `none` drops it,
read at the next infrastructure wait), and, only while a scheduler is live, `retry-budget` (plan
or `--task`), `infra-retries` and `max-parallel` (1 to 64, at the next dispatch decision; a lower
limit lets running tasks finish and never stops one). The budgets
keep the **per invocation** rule: the flags of `run` are not remembered, and a live scheduler only
mirrors them into `retry_budget`, `infra_budget` and `tasks/<id>/retry_budget` (re-read at every
decision, removed when it stops; `tasks/<id>/retries.used` stays), so a change dies with the
run. `pin <task> <spec>` sets the model the task's next full retry uses, once (checked with
`forge-dispatch.sh doctor`, no quota): `prepare_retry` claims `escalation.pin` as
`escalation.pin.applying`, rewrites `tasks.tsv`, appends the `escalations.tsv` row with
`source=pin`, clears `escalation.stop`/`split.suggested`, then consumes the pin. A symlinked
`escalations.tsv`, or a symlink in any directory from the plan down to it, is not written:
the pin stays and nothing is retried. A QA-only resume leaves it for the next full retry;
Fractal plans never apply pins; a MERGED task refuses one.
The last column of `escalations.tsv` says who decided: `policy`, `retry` (an explicit
`--dwarf`), `pin` or `fallback` (`--infra-fallback`, below); four-column rows read as `policy`.
Every change goes to `control.log` (JSON lines: `at`, `actor` `cli|ui`, `action`, `task`, `key`,
`value`, `previous`) and to the dataset as a `control` event. Exit 2 invalid input, 3 refused (not
live, MERGED, Fractal, failing doctor, lock busy over 5 s). Report them; never set one yourself
unless asked.

Five more commands steer a live scheduler (exit 3 when none is live):

- `drain <plan>` starts nothing new: running tasks finish, the scheduler waits (it prints
  `drained — N task(s) waiting` once) and infrastructure waits keep counting down. `resume <plan>`
  ends the drain. This is the supported way to pause a run; nothing has to be signalled.
- `stop <plan>` starts nothing new and ends the run once the running tasks are done: a waiting
  infrastructure pause ends too, unstarted tasks stay `PENDING`, verification is skipped and `run`
  exits 130 (0 when every task had already merged). `stop --now` interrupts the running tasks
  as SIGTERM would.
- `retry <plan> <task> [--dwarf <spec>]` and `add <plan> <task>` only queue a request, one JSON
  file in `control.requests/`. At its next pass the scheduler applies or refuses it and appends
  an outcome line to `control.log` with the same `request` id (`actor` `scheduler`, `outcome`
  `applied|refused`, `reason`). `retry` takes a `FAIL`, `UNKNOWN`, `TIMEOUT`, `ERROR`,
  `BLOCKED` or `INTERRUPTED` task that is neither running nor waiting, prepares it exactly as
  `forge-parallel.sh retry` does (a policy stop refuses it unless `--dwarf` names a model), and
  spends no automatic retry budget; an `INFRA` task is the
  scheduler's own to resume. `add` takes a row the human appended to `tasks.tsv` (with its
  `tasks/<id>/prompt.md`) whose dependencies this run already knows, and refreshes `waves.tsv`.
  A request never outlives the scheduler that was live when it was queued; one left behind is
  refused, and every request is refused once the run is stopping.

**Ordinary execution resume:** run `run` again. Tasks already `MERGED` are skipped. A saved PASS is fingerprint-checked and merged without
redispatch. PENDING, `INFRA` (paused by an infrastructure failure) and newly unblocked tasks run
when eligible. Failed or interrupted tasks
require an explicit `retry` (or `--retry-failed N`); `run` never silently pays for them again. Worktrees and pinned
baselines are retained. A per-plan advisory lock excludes concurrent run/retry/integrate
operations.

**Interrupts.** A SIGINT or SIGTERM to `run` (or to its scheduler), and `forge control stop
--now`, first reap the tasks that already ended (a PASS still merges), then send TERM to each
running task's process group, wait up to `FORGE_INTERRUPT_GRACE` seconds (default 10) for the
group to empty and KILL what is left. Only those live tasks become `INTERRUPTED`; their worktree
keeps the partial work. Each guarded dispatch records its harness in `tasks/<id>/<role>.harness`
(pid and start time), and `forge-guard.py stop` ends a harness that outlived its dispatch, at
the interrupt and again at the next scheduler start (a scheduler killed outright leaves tasks
`RUNNING`; the next run stops their harnesses and marks them `INTERRUPTED`). An interrupted
`run` skips verification and its rerun, prints `forge-parallel.sh retry <plan> <id>` for each
interrupted task, releases the plan lock and exits **130**. A signal that arrives during
verification stops the verify command's whole process tree (TERM, then KILL after a few
seconds) and records `verification.status` `INTERRUPTED`; that verification is not rerun.

**Fractal resume:** discover the managed ID with `<skill_dir>/forge fractal runs --repo
"$REPO" --json`, then use `<skill_dir>/forge fractal resume RUN_ID`. Run-level resume
recovers execution and the recorded Forge pipeline, retaining accepted stages and pinned
baselines. A control scoped with `--task` or `--task ... --node ...` only resumes that
execution scope; it does not relaunch the full pipeline. Use `retry "$PLAN" TASK_ID` for
an explicitly requested new attempt, with the existing backend, bounds and eligible pool.
Pause, stop and inspection preserve both the work and diagnostics.

## Infrastructure failures

Quota or usage limits, auth problems, rate limits, network errors and empty answers say nothing
about the work. `forge-dispatch.sh` classifies them from the dispatch log (strong phrases and
structured `is_error`/`turn.failed` results only; a reply that otherwise succeeded is never
reclassified) and exits **8**, writing `<role>.infra` (`class=`, `retry_after=`, `detail=`,
`harness=`, `model=`). `do_task` then ends the task as **`INFRA`**, copying that file to
`tasks/<id>/infra.txt` with `stage=dwarf|qa`:

- a **dwarf-stage** stop refunds the attempt counter;
- a **QA-stage** stop keeps the attempt (the dwarf's work is real and committed) and writes the
  `resume=qa` marker, so the next entry re-reviews without running the dwarf.

The scheduler pauses only what the stop says something about. Its **scope** comes from the class:

| Class | Scope paused | Other tasks |
|---|---|---|
| `rate_limit` | that model on that harness (`model:<harness>/<model>`) | keep running, on any other model |
| `quota`, `auth` | that harness's account (`harness:<harness>`) | keep running on other harnesses |
| anything else (network, empty output, ...) | everything (`all`): running tasks drain, nothing new starts | wait with it |

A pending task whose next dispatch (its implementer and reviewer, or the reviewer alone before a
QA-only resume) needs a paused pair waits; the rest of the run continues. Unstarted tasks stay
`PENDING`, never `BLOCKED`. Without `--infra-retries`, `run` ends once nothing else can run, prints
what to fix and exits `8`; verification is skipped and running the same command again resumes.
`run --infra-retries M` waits out each scope instead, up to `M` waits **per scope** (a stop in a
scope already paused joins its wait and is not counted again), then resumes its tasks in the same
run with no attempt spent. A scope whose waits are used up stays paused while other tasks finish.

Each wait is the provider's retry-after when the log carried one (capped at 6 h), otherwise the
backoff shape for that wait number. `--infra-backoff <shape>` on `run` (kept in the plan as
`infra_backoff`, read at every wait, so `forge control set infra-backoff` applies to the next one)
takes `exp:<base>[,<cap>]` (base doubling, capped at `<cap>`, default the larger of the base and
30 min), `linear:<start>,<step>[,<cap>]`, or `list:<a>,<b>,...` (a bare `<a>,<b>,...` is a list;
the last value repeats), all in positive seconds. The default is `exp:60`; `FORGE_INFRA_BACKOFF`
changes that base. The wait message names the source, for example `waiting 120s before resuming
(backoff exp:60, wait 2 of 3)`. While anything is paused, `infra.paused` (JSON `{"scopes": [...]}`
with `scope`, `class`, `harness`, `model`, `tasks`, `cycle`, `until`, `exhausted`) says what waits
and until when; `forge control show` reports it as `infra_paused`, and it is removed when nothing
is paused.

`--infra-fallback` on `run` (kept as `infra_fallback`; `--no-infra-fallback` turns it off, the
default) moves a **dwarf-stage `rate_limit`** stop to the next member of the task's own tier pool,
in pool order, that runs another model on a pair nobody paused. No attempt is spent; the move is
recorded in `escalations.tsv` with source `fallback` and in `tasks.tsv`, and the task is
dispatched again at once. A pinned task, a Fractal plan, a QA-stage stop and a pool with no
healthy member keep the ordinary pause.

A planned pause (a provider maintenance window, a quota reset) is `forge control drain`, not a
signal: running tasks finish, nothing new starts, and `forge control resume` continues the same
run.

A dwarf that exits 0 with an empty final message *and* an empty diff is the same class
(`class=empty`): it never really ran. Under Fractal a dwarf-side stop still ends as Fractal's own
`failed`; QA-side stops behave as above, and `pipeline.py` resets `INFRA` to `PENDING` on resume.

## Implementer guards

forge records what it observed about how each dwarf behaved, never what the dwarf claimed:
HEAD or the checked-out ref moved (**self-commit / branch switch**), processes left alive in the
harness's session after it exited (**orphans**, listed in `dwarf.orphans` and stopped; disabled by
`FORGE_ORPHAN_GUARD=off` or under Fractal), and a final message that **promises later work**.
They are written to `tasks/<id>/guard.txt`, added to the QA prompt as "Implementer guard notes" and
shown in the run summary. They never change a status. The dwarf prompt itself tells the
implementer not to commit, not to start background processes, to finish every measurement before
its last message, and to say plainly what it could not run.

## Accepting, splitting, combining and the whole-run review

Four subcommands act on plans after tasks have run. Full usage and refusal conditions are in the
[README](../docs/reviews.md#accepting-a-task-despite-its-reviewer); the mechanics:

- **`accept <plan> <task> --reason … --approved`** overrides a `FAIL`/`UNKNOWN` reviewer only
  when the reviewed commit is still the branch tip and the worktree still matches
  `source.fingerprint`. It records `known_issues.md` and `accepted.by`, merges through `merge_task`,
  and restores the prior state if the merge fails. The orchestrator never runs it unprompted.
- **`review <plan> [--qa <spec>]`** and **`integrate --final-review <spec>`** run one QA dispatch
  over the combined result (`integration.base..tip`, or `parent..candidate`) in a disposable
  checkout with the goal, every task's requirements, known issues, drift, guard notes and the
  verification result in front of the reviewer. Same verdict/severity contract and
  `--qa-threshold` gate as per-task QA; a mutation during the review is `INVALIDATED`; a `FAIL`
  or missing verdict blocks `integrate` with exit `5`, an infrastructure stop with `8`, and the
  user's branch is never touched. Nothing runs unless one of them is invoked.
- **`split <plan> <new-plan> --tasks a,b`** copies the selected tasks' saved state to a new plan,
  renames their branches and worktrees to the new run id, starts a new integration branch at the
  source's accepted tip and removes them from the source (refuses Fractal-backed plans, `MERGED`
  or `RUNNING` tasks, and selections that strand a dependent).
- **`combine <new-plan> <plan>…`** merges the integration branches of fully merged plans into one
  new plan whose `run` only verifies. Task branches keep their original names.

`split`/`combine` hold the plans' advisory locks for their whole duration, and every failure path
removes what it created.

## Branches, worktrees, cleanup

```
<plan-dir>/tasks/<id>/approach.md               intended approach, if planned
<repo>/.forge/                                  project memory (survives the run)
<repo>/../.forge-worktrees/<run-id>/<task-id>   task worktree
<repo>/../.forge-worktrees/<run-id>/_integration integration worktree
forge/<run-id>/<task-id>                        task branch
forge/<run-id>                                  integration branch
```

Fractal also keeps control repositories, isolated product candidates and per-step logs
under `${XDG_STATE_HOME:-$HOME/.local/state}/forge/fractal/runs/<managed-run-id>/tasks/`.
These are separate from the Forge task worktrees listed above. Candidates begin at each
task's pinned baseline and return to its ordinary review/integration pipeline; Fractal
initialization metadata stays outside the product diff. Managed run/task IDs are shown by
`fractal runs` and `fractal tree`, and differ from the human-readable `tasks.tsv` IDs.

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
| task status `INFRA`, `run` exits `8` | quota/auth/rate limit/network/empty output stopped a dispatch; no attempt spent. See `tasks/<id>/infra.txt`, fix the cause, run again (or `--infra-retries M`, `--infra-fallback`) |
| task status `INTERRUPTED`, `run` exits `130` | the run was signalled or stopped with `forge control stop --now`; verification was skipped. The summary prints `forge-parallel.sh retry <plan> <id>` for each one; a plain `run` does not dispatch them |
| a run that sits idle with tasks `PENDING` | `forge control drain` is in effect (`forge control show` says `drained`; `forge control resume` continues), every scope a task needs is paused on infrastructure (`infra.paused`), or `max-parallel` is lower than you think |
| a `FAIL` task merged anyway | an explicit `--qa-threshold` tolerated its findings; see `known_issues.md` and `qa.gate` |
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
and the task branch must still point at the reviewed commit when merged. INVALIDATED results
never merge. Disposable snapshots detect mutation; they do not confine unrestricted shell access.

Configure combined executable checks with `run <plan> --verify '<command>'` (also accepted by
`retry`), or a shell command file at `<repo>/.forge/verify`. Commands execute under Bash from
the integration checkout. Forge records `verification.command`, `.log`, `.exit`, `.fingerprint`
and `.status` in the plan directory. Failed checks or source changes fail the run. With no
command, the result is explicitly UNVERIFIED.

A failed command whose source fingerprint is unchanged is rerun (`--verify-retries N`, default
`1`, `0` = strict single run). A rerun that passes leaves status `PASS` but writes
`verification.flaky` (both exit codes), keeps the first run in `verification.log` and the second
in `verification.retry.log`, and prints a loud `FLAKY` line in `run`/`retry`/`integrate` output;
a command that changes the tree is never retried. This runs before Jev's optional model-assisted
triage, which then does not.

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
