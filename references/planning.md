## Planning

Fractal selection applies to the later implementation stage. It does not turn a planner
dispatch or `forge-parallel.sh plan` into nested execution, install a runtime during
planning, or bypass the task/model approval table. Pass `--fractal` or `--no-fractal` to
the eventual solo runner or decomposed `run`. See [Fractal](fractal.md).

For automatic recursive decisions during execution, use `--fractal-auto-decompose`.
Its `--fractal-planner` selects the per-node decision model, independently of the
top-level `--planner` that designs the whole run. Without an override it reuses that
planner when configured, then each task's initial root model. Child planning must
preserve the approved interfaces and ownership established here.

By default **you** plan: before dispatching, decide the approach and put it in the prompt,
so the dwarf builds the agreed thing rather than inventing a shape nobody has seen. In a
decomposed run that matters more, because the capsule prevents two dwarves from touching
one *file* but nothing stops them inventing incompatible *interfaces* at a seam they share.

`--planner <spec>` hands that stage to a named model instead — one dispatch for the whole
run, never one per task. A single planner is the point: one mind designs both sides of every
seam, where N independent planners would recreate the problem. Effort defaults to `xhigh`,
since a bad plan is executed at full price by every dwarf downstream of it.

Offer optional installation once before a direct planner dispatch with
`bash <skill_dir>/scripts/forge-install-ripwire.sh`. Propagate `FORGE_RIPWIRE=off`
when the user opted out. Supply `--ripwire-query-file` when requirements are separate
from the assembled planner prompt; see [Ripwire context](ripwire.md).

```bash
bash <skill_dir>/scripts/forge-dispatch.sh planner <spec> \
  --repo "$REPO" --run-dir "$FORGE_RUN" --prompt-file "$FORGE_RUN/planner.prompt"
```

The planner is instructed to read only and uses QA permission flags. These flags alone do not enforce filesystem immutability.
Ask it for the approach only: in a decomposed run, `tasks.tsv` rows plus a few lines of
approach per task; in a single-task run, just the approach. Ask too for tasks small enough to
pass on a first attempt, each ending its prompt with an `## Acceptance criteria` section of
checks the reviewer can verify; see [task size](decompose.md#task-size-and-acceptance-criteria). Write each task's approach to
`tasks/<id>/approach.md`, where the runner picks it up for both the dwarf and qa.

If `plan` exits 4 (the prompt or coverage gate is `on` and sent work back), read
`<plan>/plan.sendback.md` and re-dispatch the planner with that file appended to its prompt: it
rewrites the listed `prompt.md` files and extends or adds tasks for each uncovered requirement.
Then run `plan` again, once; forge never sends the same task or requirement back twice. Under
the table, the `decision-model gates:` block lists what drift and forward added to
`tasks.tsv` (files and deps, `on` only) and what is still flagged; see
[decomposition](decompose.md#plan-gates-sent-back-and-the-block-under-the-table).

Show the approach before dispatching and let the user change it. That gate is the last
moment the plan is free; after it, changing the plan costs a whole run.

## The planner contract (`forge-planner.py`)

`scripts/forge-planner.py` is the contract between a planner and forge, so no session has to
turn a free-form reply into `tasks.tsv` by hand. A later `forge-parallel.sh auto` calls
`prompt` and `parse`; you can call them yourself. Standard library only, and every
subcommand refuses (exit 2) a plan dir that already has `tasks.tsv`.

```bash
python3 <skill_dir>/scripts/forge-planner.py prompt --plan "$PLAN" --goal-file goal.md [--level low|medium|high]
python3 <skill_dir>/scripts/forge-planner.py parse  --plan "$PLAN" --reply reply.txt
python3 <skill_dir>/scripts/forge-planner.py gate     --plan "$PLAN" --goal-file goal.md
python3 <skill_dir>/scripts/forge-planner.py one-task --plan "$PLAN" --goal-file goal.md [--difficulty medium]
```

**`prompt`** writes `$PLAN/planner.prompt` and prints its path: the planner's role (read-only),
the request verbatim, the decomposition rules and the reply format. The rules are those of
[decompose.md](decompose.md) plus: one task is a valid answer; a pure chain is worse than one
task (prefer parallel branches); never two tasks on one file unless a dep orders them; a
single-file deliverable is split by module, each task owning its own source file, with one final
low-difficulty task assembling the required file; each task prompt ends with `## Acceptance
criteria` and never asks for the whole suite. A planner you dispatch by hand (above) can be
given this same prompt file as `--prompt-file`.

**The reply** holds exactly one fenced block opened by ```` ```forge-plan ```` whose body is JSON
with the key `tasks` and, optionally, `groups`:

```
{"tasks": [{"id": "parse-config", "deps": [], "difficulty": "medium", "risk": "low",
            "files": ["src/config.py", "tests/test_config.py"],
            "title": "Parse the new config section", "approach": "…",
            "prompt": "… ## Acceptance criteria …"}],
 "groups": [{"id": "config-limits", "title": "Config limits",
             "goal": "The limits section is read into Config with the defaults the request names.",
             "members": ["parse-config"]}]}
```

`groups` names steps the user will think of as one: tasks that together deliver one goal, usually
one group per wave of the plan's dependency order. Each group has a short title and a goal
sentence that says what is true once its tasks are done. A group may hold one task, and a task
may be left out of every group. Omit the key to return no groups.

**`parse`** is strict and never guesses or repairs. It refuses a reply unless: there is exactly
one `forge-plan` block whose body is an object with the key `tasks` and no keys other than the
optional `groups`; `tasks` is 1 to 20 objects with exactly the keys `id, deps, difficulty, files,
title, approach, prompt`; each `id` matches `^[a-z0-9][a-z0-9-]{0,39}$` and is unique; `deps` are
known ids with no cycle; `difficulty` is `low`, `medium` or `high`; `files` is a non-empty list of
repo-relative paths (no absolute path, no `..`, no tab, comma or newline); `title` is one
non-empty line without a tab; `approach` is a string (it may be empty); `prompt` is non-empty and
has a `## Acceptance criteria` heading. `risk` (#122) is the one optional task key and never
refuses a reply: `low`, `medium` or `high` is written to `tasks/<id>/risk`, anything else (or no
key) writes nothing.

`groups`, when present, is a list of objects with exactly the keys `id`, `title`, `goal` and
`members`. `id` matches the same slug as a task id, is unique among the groups, and is not a task
id. `title` and `goal` are one non-empty line each, at most 200 characters, with no tab or other
control character. `members` is a non-empty list of task ids from this reply, kept in reply order;
a member that is not a task of the reply, a task in two groups (or listed twice in one), a
duplicate id, an id equal to a task id, a missing field, or a multi-line title or goal refuses
the reply. The planner never sets
`review`: every row is written `review` `task` and `qa` `-`. Group review is the user's opt-in.

On success it writes `tasks.tsv` (dwarf and qa `-`), `tasks/<id>/prompt.md` (the task's prompt,
then `## The whole request` and `$PLAN/request.md` verbatim, so no requirement is lost) and, for
a non-empty approach, `tasks/<id>/approach.md`. When the reply includes `groups`, it also writes
`groups.tsv` (`id`, `review`, `qa`, `members`, `title`, `goal`; a header alone when the list is
empty) in that same pass. A reply with no `groups` key writes no `groups.tsv`, and the other
files are unchanged. `$PLAN/request.md` must hold the user's request (exit 2 when missing). On a
refusal it writes only `$PLAN/planner.refused` (the first reason and the reply's path), prints
both and exits 4; nothing else is written, including `groups.tsv`. Fix the reply (or re-dispatch
the planner with the reason appended) and run `parse` again.

Those rows are how a wave gets a purpose. Brief mode's wave questions are about a group: the
title is the step and the goal is what the question is deciding. The approval table prints a
`groups:` block under the tasks (id, title, goal, members). `plan --group-review` opts the groups
into one review each (`review` becomes `group`, one QA pass for the members). Without that flag
every group stays `review` `task` and `qa` `-`, and each member is reviewed on its own. Editing
`groups.tsv` is the other way to opt in.

**`gate`** decides before any planner is paid for: `one-task` when the request has at most 1500
characters and at most 6 requirements (counted by `split_requirements` of
`forge_jev/coverage.py`), else `plan`. It prints the decision and records the signals, the
thresholds and the rule in `$PLAN/plan-gate.json`. It is this conservative because size did not
predict the plan's shape on the 16 plans of benchmark #86 (element-web, 3262 characters and 15
requirements, planned one task; flipt, 3276 and 22, planned two parallel tasks): it only skips
the planner for small goals.

**`one-task`** writes a one-row plan with no planner: id `task`, files `.`, the request as the
prompt followed by one acceptance criterion, and the request's first line as the title.
