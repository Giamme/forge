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

**The reply** holds exactly one fenced block opened by ```` ```forge-plan ```` whose body is JSON:

```
{"tasks": [{"id": "parse-config", "deps": [], "difficulty": "medium", "risk": "low",
            "files": ["src/config.py", "tests/test_config.py"],
            "title": "Parse the new config section", "approach": "…",
            "prompt": "… ## Acceptance criteria …"}]}
```

**`parse`** is strict and never guesses or repairs. It refuses a reply unless: there is exactly
one `forge-plan` block whose body is an object with exactly the key `tasks`; `tasks` is 1 to 20
objects with exactly the keys `id, deps, difficulty, files, title, approach, prompt`; each `id`
matches `^[a-z0-9][a-z0-9-]{0,39}$` and is unique; `deps` are known ids with no cycle;
`difficulty` is `low`, `medium` or `high`; `files` is a non-empty list of repo-relative paths (no
absolute path, no `..`, no tab, comma or newline); `title` is one non-empty line without a tab;
`approach` is a string (it may be empty); `prompt` is non-empty and has a `## Acceptance
criteria` heading. `risk` (#122) is the one optional key and never refuses a reply: `low`,
`medium` or `high` is written to `tasks/<id>/risk`, anything else (or no key) writes nothing.

On success it writes `tasks.tsv` (dwarf and qa `-`), `tasks/<id>/prompt.md` (the task's prompt,
then `## The whole request` and `$PLAN/request.md` verbatim, so no requirement is lost) and, for
a non-empty approach, `tasks/<id>/approach.md`. `$PLAN/request.md` must hold the user's request
(exit 2 when missing). On a refusal it writes only `$PLAN/planner.refused` (the first reason and
the reply's path), prints both and exits 4; fix the reply (or re-dispatch the planner with the
reason appended) and run `parse` again.

**`gate`** decides before any planner is paid for: `one-task` when the request has at most 1500
characters and at most 6 requirements (counted by `split_requirements` of
`forge_jev/coverage.py`), else `plan`. It prints the decision and records the signals, the
thresholds and the rule in `$PLAN/plan-gate.json`. It is this conservative because size did not
predict the plan's shape on the 16 plans of benchmark #86 (element-web, 3262 characters and 15
requirements, planned one task; flipt, 3276 and 22, planned two parallel tasks): it only skips
the planner for small goals.

**`one-task`** writes a one-row plan with no planner: id `task`, files `.`, the request as the
prompt followed by one acceptance criterion, and the request's first line as the title.
