# Models, efforts and harnesses

[← README](../README.md) · [All docs](README.md)

How forge names a model (`alias:effort:harness`), what each harness offers, how effort is validated and clamped, when to drop the sandbox, and how to teach forge a new model.

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
- effort missing → dwarf builds at `medium`, qa reviews at `high`, a planner plans at `xhigh`. A
  reviewer thinking *less* than the implementer tends to wave things through, which wastes the
  stage entirely; `xhigh` reviews cost about twice as much as `high` ones for the same verdicts in
  the SWE-bench Pro benchmark (#89), so `high` is the default and `xhigh` is yours to ask for.

## Recommended routing

What follows is measured, not a rule. It comes from benchmark #86 (SWE-bench Pro,
decomposed runs, every role on the claude harness), dated 2026-10-28. It will be
re-measured, and this table replaced, on the next benchmark round.

Measured over dwarf attempts across four decomposed runs. Times are model session times.

| Spec | Attempts | Mean time | Mean cost | Review outcome |
|---|---|---|---|---|
| `haiku:max` | 7 | 7.8 min | $0.25 | 7/7 passed first time; up to 5.1M tokens in one attempt |
| `haiku:xhigh` | 4 | 1.9 min | $0.02 | 4/4 passed first time |
| `sonnet:medium` | 15 (14 tasks) | 1.1 min | $0.14 | 13 passed first time, 1 on retry |
| `sonnet:high` | 5 | 3.3 min | $0.33 | 5/5 passed first time, including the high tasks of three HARD-51 problems |
| `sonnet:xhigh` | 2 | 3.6 min | $0.54 | 2/2 passed first time |
| `opus:high` | 2 (hard set, high tier) | 5.9 min | $1.13 | 2/2 passed first time |

What it says:

- `haiku:max` was the slowest dwarf in every run that used it, about 4x `haiku:xhigh`,
  for no gain in pass rate.
- `sonnet:medium` is the workhorse: about a minute, $0.14, and enough for every medium
  task.
- Above medium, `sonnet:high` held up even on HARD-51 high tasks, and `opus:high` is the
  safer choice where a high task's failure costs a whole chain.
- Escalation never triggered: no task failed twice on one model.

The recommendation:

- Dwarfs: low `haiku:xhigh`, medium `sonnet:medium`, high `opus:high` (`sonnet:high` for a
  cheaper run).
- Reviewers by difficulty: `sonnet:medium` / `sonnet:high` / `opus:high` (see #89).
- Planner: `sonnet:high`. In #86, `sonnet:xhigh` cost about what `opus:xhigh` did per plan
  and was slower, and most of a planner's cost is exploring the repository.

```
--dwarf-low haiku:xhigh --dwarf-medium sonnet:medium --dwarf-high opus:high --qa-low sonnet:medium --qa-medium sonnet:high --qa-high opus:high
```

forge ships two built-in profiles carrying this routing, `balanced` and `lite` — `lite` is
the same with the high tier on `sonnet:high` for both dwarf and reviewer — both using
`--retry-failed 2 --escalate-after 2`; select one with `--profile balanced` (see
[commands.md](commands.md)), and `forge config profile show balanced` prints it.

| Profile | High tier (dwarf and reviewer) |
|---|---|
| `balanced` | `opus:high` |
| `lite` | `sonnet:high` |

forge notes a spec measurably slower than a lower effort of the same model with no gain
(today `haiku:max` against `haiku:xhigh`), in `doctor` and under the plan table, and never
changes the spec.

## Effort, ceilings and clamping

Every harness has its own effort ladder:

| Harness | Ladder |
|---|---|
| codex | `low medium high xhigh max ultra` |
| claude | `low medium high xhigh max` |
| openclaude | `low medium high xhigh max ultracode` |
| antigravity | `low medium high` |
| grok | `low medium high xhigh max` (grok-4.7 offers up to `xhigh`) |
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

## Yolo mode

`--yolo-dwarf` and `--yolo-qa` drop that role's sandbox and approval gates:

| Harness | Flag used |
|---|---|
| codex | `--dangerously-bypass-approvals-and-sandbox` |
| claude / openclaude / antigravity | `--dangerously-skip-permissions` |
| opencode | `--auto` |
| grok | `--always-approve` |

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

## Model registry

`registry.tsv` is the single source of truth for alias resolution. Four tab-separated
columns, plus an optional fifth:

```
# alias	harness	model	effort	[timeout]
sol	codex	gpt-6.1-sol	<=ultra
sol	openclaude	gpt-6.1-sol	<=max
gemini-pro	antigravity	gemini-3.1-pro	low,high
opus	antigravity	claude-opus-4-6-thinking	none
grok	grok	grok-4.7	<=xhigh
grok	opencode	github-copilot/grok-4.6	-
```

| `effort` form | Means |
|---|---|
| `<=X` | any effort on that harness's ladder up to and including `X` |
| `a,b,c` | exactly these efforts — gaps are real, and this is how they're expressed |
| `none` | takes no effort flag at all; passing one errors |
| `-` | no validated ladder; forwarded verbatim |

The optional fifth column, `timeout`, is a per-model default dispatch limit in seconds (`-`
or absent = the built-in default). It applies only when neither `--timeout` nor
`FORGE_TIMEOUT` was given and is scaled by the resolved effort (see
[timeout precedence](commands.md#scriptsforge-dispatchsh)). Rows without it behave exactly as before.

**Row order is meaningful:** the *first* row for an alias is that alias's default harness.
`--dwarf sol` means sol on codex; `--dwarf sol:high:openclaude` moves the same model
elsewhere.

Adding a model is one line in this file. Nothing else needs to change.
