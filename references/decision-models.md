# Decision models

A decision model is a small hosted model that answers narrow, typed questions (a yes/no
probability, a single choice) over state forge supplies, in about a second. forge uses two:
TypeSafe's **Jev** and Cloudflare's **Clef**, which takes Jev's request shape. Both are
optional. Read this when turning one on, when changing a site, or before proposing a new one.

- **Off by default.** Nothing is sent until a gate is `shadow` or `on` and its provider can be
  asked. See [Setup](#setup).
- **Always fail-open.** A timeout, an error, a refusal or a missing key is logged as a skip
  reason, and the run proceeds exactly as it would with the model off. A decision-model call
  can never fail a run.
- **The rule: a gate never changes a verdict, never blocks a dispatch, and never makes work
  pass.** It never changes a review verdict, a severity, `accept` or a PASS. A decision model
  never changes a task's tier or the model a task runs on, and never marks anything verified
  itself. With the gate `on`, a gate acts in one of these ways and no other: it sends a task's
  prompt or an uncovered requirement back to the planner, once; it adds a predicted file to a
  task's `files`, or a dependency that forms no cycle; it bounces a retry, skips the review of an
  attempt that says it did not finish, or stops a task for the user's answer; it puts the likeliest
  culprit's boundary first in the attribution search; it turns duplicate findings into comments and
  pre-selects concrete ones for filing; or it acts on memory and on the verify command. Verify
  discover picks the command that runs when none is configured; that command's own result decides
  verification. `shadow` asks and records, and changes nothing. See [Gates and modes](#gates-and-modes)
  and [Setup](#setup).
- **`FORGE_JEV=off` sends nothing.** It turns every gate off. A repo privacy floor does the
  same and is applied after it. See [Setup](#setup).

Which models do which work is measured instead of asked: exploratory routing and
`forge dataset routing` compare models per tier from randomized assignments (see
[Decomposition](decompose.md#exploratory-routing-on-by-default)).

## Criteria a site must meet to exist

#35 audited every site against four rules. A site that fails one is removed, not tuned.

1. **It changes something.** A warning a person sees, a command forge runs, a fact it keeps
   or drops. A score nobody reads is not a site.
2. **It was measured.** Against a label from outside the model (git history, the task's own
   outcome, the repo's documented command), on state production really sends.
3. **It beats a trivial baseline.** Doing nothing, an exact-match key, or a cheap feature.
4. **It is cheap enough per run.** One bounded request, never one per file, per dispatch or
   per entry on a hot path.

How each kept site meets them:

| site | changes | measured | baseline it beats | cost |
|---|---|---|---|---|
| drift | in `on`, adds the predicted undeclared file to the task's `files` before the waves are computed (in `shadow`, only recorded) | precision at `gate_warn` 0.60 over 50 commits × 2 repos: **1.000** and **0.934**; non-edited files scored ≤0.23 where the true file scored 0.65 | the post-wave `drift.txt`, which reports the broken promise only after the wave ran | one request per task |
| forward | in `on`, makes the earlier-wave task wait for the later one it may be written against, unless that forms a cycle (in `shadow`, only recorded) | 7 cross-wave pairs, 5 runs each: `readme`→`cli` **0.88–0.90**, every other pair 0.05–0.15 (gap 0.73, noise 0.07) | wave order, which cannot see the case; the review cannot either | one request per plan |
| prompt adequacy | in `on`, sends a task back to the planner, once, when a reviewer could not judge its prompt (in `shadow`, only recorded) | 3 runs over 8 probe prompts and 8 real ones: unjudgeable **0.06–0.27**, checkable behaviour **0.85–0.96** | silence; the old 0.30 sat 0.03 above the worst true positive | one request per task |
| memory curate, slice, stale | refiles, merges, hides or drops a fact in `memory.md` | merge: wrong 0.56–0.72, right 0.91–1.00. Refile: wrong 0.56–0.72, right 0.92–1.00. Stale: gone false 0.03–0.10, still true 0.47–0.96 | exact-match keys (two wordings of one fact never reach two distinct runs) | curate at `record`, slice at `inject`, stale only on `prune` |
| verify discover | picks a verify command when none is configured | 18 repos: 10 fire at 0.70, every pick correct, lowest correct 0.73, highest wrong 0.55 | `UNVERIFIED` | one request, only when nothing is configured |

The five gates added in C3–C5 (coverage, retry-addressed, dwarf-message, attribution, findings)
are not in this table. None has a replay or a measured threshold yet, so their thresholds are
starting points, and `forge decide gate G on` needs `--force` for each until a replay passes.

Two caveats travel with these numbers:

- Forward rests on a single positive example. A second plan could move the positive class, but
  unlikely by 0.38.
- `tests_act` 0.70 is **stale**. The 18-repo table was measured before a make target's recipe
  reached the model, and the one confirmed wrong pick (`spankai` → `cargo test`) has since risen
  from 0.55 to 0.77–0.81, past the gate. The gate reads 1 − P(none), the probability that the
  repo is verifiable at all, because Choice confidence drops when two commands both run the suite.
  Re-measure across the 18 repos before moving the number; fitting a new one to one data point
  is the mistake this file records.

Every number here is from this project's repos and does not transfer. Re-measure before trusting
a threshold elsewhere. Each rubric has its own threshold because the rubrics are not on one
scale: the single shared `gate_warn` warned on 5 of 9 and 5 of 11 perfectly good tasks.

## The kept sites

`SITE_CAPABILITY` in `scripts/forge_jev/__init__.py` is the source of truth for the first two
columns; thresholds are `DEFAULT_THRESHOLDS` in the same file. A capability of `—` means the
gate has no legacy capability, so its own mode alone decides.

| site | capability | runs | can change | threshold | recorded |
|---|---|---|---|---|---|
| `jev-prompt` | `gates` | `plan`, per task | sends a task whose prompt is below the threshold back to the planner, once (`plan` exits 4) | `prompt_warn` 0.55 (flags a probability below it) | `<plan>/jev.jsonl`; judgment in `tasks/<id>/jev.json`; `plan-gates.json` (`prompt`) |
| `jev-drift` | `gates` | `plan`, per task | adds each predicted file at or above the threshold to the task's `files`, before the waves are computed | `drift_warn`, falls back to `gate_warn` 0.60 | `<plan>/jev.jsonl`; judgment in `tasks/<id>/jev.json`; `plan-gates.json` (`drift`) |
| `jev-forward` | `gates` | `plan`, once | makes the earlier task of a warned pair depend on the later one, unless that would form a cycle (then recorded as refused); at most three pairs a plan (`MAX_WARNINGS`, the highest-scoring) | `forward_warn` 0.50 | `<plan>/jev.jsonl`; `jev-coupling.json`; `plan-gates.json` (`forward`) |
| `jev-coverage` | `—` | `plan`, once, one question per requirement of `request.md`: the first 30 that have at least four words and are not repeated (`MAX_REQUIREMENTS`), so a later requirement is never asked. Skipped above 40 tasks, with fewer than two requirements, or without `request.md` | sends each uncovered requirement back to the planner, once (`plan` exits 4) | `coverage_warn` 0.50 (the probability that no task covers it) | `<plan>/jev.jsonl`; `plan-gates.json` (`coverage`, `sent_back`, `pending`) |
| `jev-retry-addressed` | `—` | a retry (attempt above 1) whose retry findings came from a review, after its diff is taken and before QA | bounces the retry back to its implementer without a review when its diff leaves a blocking finding untouched | `untouched_warn` 0.70 | `tasks/<id>/jev.jsonl`; `bounced` and `bounce.md`; attempts.tsv `BOUNCED`; dataset attempt `sent_back: bounce` |
| `jev-dwarf-message` | `—` | each implementer reply, before its diff is taken and before any review | stops the task as `NEEDS_INPUT` when the message asks a question only the user can answer; sends the attempt back as FAIL without a review when it says it did not finish or was blocked | `message_ask` 0.70 (question); `message_warn` 0.70 (unfinished, blocked) | `tasks/<id>/jev.jsonl`; `needs_input.md` or `message.md` with `message.failed`; attempts.tsv `INCOMPLETE`; dataset attempt `sent_back: message` (not for a question) |
| `jev-attribution` | `—` | `attribute` and `run --attribute`, once per search, after a failed combined verification | ranks the merged tasks, and in `on` verifies the top suspect's boundary first; nothing is merged, reverted or accepted | none (it orders a search) | `<plan>/jev.jsonl`; `attribution.json` (`gate`); the `attribution` event |
| `jev-findings` | `—` | `forge-issues.py`, once per plan, through `forge-jev.py findings --run-dir <plan>` | turns a finding that duplicates an open issue into a comment on that issue (posted by `--apply`), and pre-selects the PLAUSIBLE findings that read as concrete defects | `findings_dup` 0.85 (duplicate); `findings_concrete` 0.50 (concrete) | `<plan>/jev.jsonl`; the `gate` field of each finding and draft; comment receipts in `${XDG_STATE_HOME:-~/.local/state}/forge/findings/<repo key>.comments.json` |
| `jev-memory-curate` | `memory` | `forge-memory.sh record` | not injected, refiled, or merged onto an existing key | `memory_merge` 0.85, `memory_recategorize` 0.80, durability `gate_warn` 0.60 | ledger flag; nothing is ever lost |
| `jev-memory-slice` | `memory` | `forge-memory.sh inject` | removes lines from the role slice, never adds or reorders | none | `jev.jsonl` |
| `jev-memory-stale` | `memory` | `forge-memory.sh prune`, deep prune only | drops an entry whose fact went false | `stale_drop` 0.25 | `memory_prune` event |
| `jev-verify-discover` | `tests` | `verify_result`, when neither `--verify` nor `.forge/verify` exists | chooses the verify command | `tests_act` 0.70 | `verification.jev.json` |

Every request, from every site, is also recorded:

- `<run-dir>/jev.jsonl`: one row per request with site, question ids, answers, confidences,
  latency, tokens and whether the judgment was applied. `jev.skip` holds the reason for a
  skipped one.
- The run dataset keeps the **full request** whole (state and questions), so a judgment can be
  replayed later. See [Dataset](dataset.md).

Thresholds are deliberately conservative where an action is irreversible: `stale_drop` sits far
below `gate_warn` because dropping a fact is silent and permanent, while a stale fact costs a
line. `memory.md` is injected into every future dispatch, so a wrong fact is paid for forever;
`ledger.tsv` is append-only, so a rejected line is still kept in full.

Verify discover enumerates commands that demonstrably exist (`package.json` scripts, Makefile
targets with their recipes, tox/nox, runner scripts, workflow `run:` steps, `## Verify` facts in
`.forge/memory.md`) and Jev selects one or `none`. It selects, never generates. `UNVERIFIED`
remains the outcome whenever nothing is chosen: a model may find a command, never declare
something verified.

## Setup

`forge decide` is the setup command. `forge jev` is an alias for this release and then goes
away. For `setup`, `sites`, `enable`, `disable`, `status` and `doctor` the launcher names the
`forge decide` command and then runs the old one. Command lines are also in
[the command reference](../docs/commands.md#forge-decide).

### Quick start

```sh
# hosted TypeSafe. The key is one line on stdin, stored mode 0600.
printf '%s\n' "$TYPESAFE_API_KEY" | forge decide provider add jev --name typesafe --key-stdin

# or a local server: --local needs --url, sends no key and costs nothing
forge decide provider add jev --name local --url http://127.0.0.1:8080 --local

forge decide gate memory-curate shadow --provider typesafe
forge decide status
```

Name a provider to choose where a gate's requests go. The local server above is
`forge decide gate memory-curate shadow --provider local`. `on` waits for a passing replay
(see [Measuring a gate](#measuring-a-gate)). A gate with no `--provider` does not
read the key file `provider add` wrote: it uses the legacy transport, `jev.json`'s endpoint
with `TYPESAFE_API_KEY` or `jev.json`'s key (or Cloudflare, for a site `jev.json` maps
there). Migration leaves `jev.json` in place, so with either key present such a gate still
sends requests, wherever `jev.json` points.

`status` prints each gate's effective mode and which level set it, the providers (key
`present` or `missing`), today's spend, and whether a privacy floor is on. It never writes.
forge-ui's Settings page has the same fields under Decision models; a change there writes the
user file.

### Gates and modes

Twelve gates, each `off` (the default), `shadow` or `on`:

`drift`, `forward`, `prompt`, `coverage`, `retry-addressed`, `dwarf-message`, `attribution`,
`findings`, `memory-curate`, `memory-slice`, `memory-stale`, `verify-discover`.

`off` asks nothing. `shadow`, on every gate, asks and records the answer, then leaves the run
as it would have been with the gate off: no note, no work sent back, no retry bounced, no review
skipped, no task stopped, no search reordered, no memory edit, no comment, no verify command.

What `on` does:

| gate | what `on` does |
|---|---|
| `drift` | adds each file it predicts at or above `drift_warn` to the task's `files`, before the waves are computed, so the wave plan treats it as declared |
| `forward` | makes the earlier task of a warned pair depend on the later one, never one that would form a cycle; the waves are computed again |
| `prompt` | sends a task whose prompt is below `prompt_warn` back to the planner, once |
| `coverage` | sends each uncovered requirement back to the planner, once (among the first 30 requirements of `request.md`) |
| `retry-addressed` | bounces a retry back to its implementer without a review when its diff leaves a blocking finding untouched |
| `dwarf-message` | skips the review of an attempt whose final message says it did not finish or was blocked (the attempt ends FAIL, unreviewed), or stops the task as `NEEDS_INPUT` when the message asks a question only the user can answer |
| `attribution` | after a failed combined verification, verifies the likeliest culprit's boundary first |
| `findings` | turns a finding that duplicates an open issue into a comment on that issue (posted by `forge issues --apply`), and pre-selects the concrete findings for filing |
| `memory-curate` | refiles, merges or withholds the fact |
| `memory-slice` | removes lines from the injected slice |
| `memory-stale` | drops a fact that went false |
| `verify-discover` | runs the chosen command when none is configured |

`prompt` and `coverage` send work back **once** per item: a task for `prompt`, a requirement for
`coverage`. `plan` writes `plan.sendback.md` listing what was flagged and exits 4 before it prints
the table. Rewrite what the file lists, then run `plan` again. What is still flagged from that list
is printed under the table and is not sent back again. An item the first list did not name is a new
flag, such as a task the rewrite added with an unjudgeable prompt, or a requirement the rewrite left
uncovered. That item is sent back in its own round, so the second `plan` can exit 4 again. No item
is sent back twice; `plan-gates.json` keeps that history across re-plans.

`dwarf-message` and `retry-addressed` send an attempt back without spending a review on it. The
attempt ends FAIL, and the next attempt is handed the implementer's own words (`dwarf-message`) or
the untouched blocking findings (`retry-addressed`). `retry-addressed` does not check the attempt
after its bounce again.

The memory gates and verify discover act as they did before C3–C5. Verify discover still does
not mark anything verified: the command it picked does, or `UNVERIFIED` stands.

`on` for any gate needs a passing replay for the provider and model it uses, or `--force`.
See [Measuring a gate](#measuring-a-gate). None of the five gates added in C3–C5 has a recorded
replay yet, so each needs `--force` for now.

Set one with `forge decide gate <gate> off|shadow|on [--provider P] [--model M]`. `--repo DIR`
writes `<repo>/.forge/decide.json`; `--profile NAME` writes that profile in the user file.
The two flags are exclusive. `--provider` must already be configured, and the gate uses only
that provider.

### Where settings live, and which wins

Two files. The user file is `${XDG_CONFIG_HOME:-~/.config}/forge/decide.json`, mode `0600`.
An empty or relative `XDG_CONFIG_HOME` counts as unset. The repo file is
`<repo>/.forge/decide.json`, mode `0644`, and is the one a checkout shares.
`--repo` defaults to `FORGE_DECIDE_REPO`, else the git top level of the working directory.

Six levels, lowest first. A level sets only the fields it names. The user file wins over the
repo on any field both set; `status` lists those as shadowed.

1. **default.** Every gate `off`, no budget.
2. **repo.** `<repo>/.forge/decide.json`: `gates`, and the per-run budget (`run_tokens`,
   `run_usd`).
3. **user.** `decide.json`: `gates`, providers, and the whole budget, including the daily caps.
   While that file does not exist, the legacy level stands here (source `legacy`): `jev.json`
   plus `FORGE_JEV*` turn a gate `on` when its old capability is enabled. A fresh install, with
   neither file and no `FORGE_JEV*`, stays at `default`.
4. **profile.** `FORGE_DECIDE_PROFILE`, looked up in the user file's `profiles` first, then the
   repo file's. A profile in the user file may set the daily budget; one in the repo file may
   set only the per-run budget.
5. **env.** `FORGE_DECIDE_GATE_<NAME>=off|shadow|on`, with `NAME` upper-cased and `-` written
   `_` (`FORGE_DECIDE_GATE_MEMORY_CURATE`, `FORGE_DECIDE_GATE_VERIFY_DISCOVER`). Once the user
   `decide.json` exists, `FORGE_JEV_<CAP>` can only turn gates off, and any `=on` is an
   allowlist of the capabilities left on. `FORGE_JEV_SHADOW=on` (`--jev-shadow`) turns every
   `on` into `shadow`, except, before the user `decide.json` exists, a gate the legacy level
   turned on: that site acts as it did before #36, its records are marked shadow, and `plan`
   prints a count in shadow mode instead of its warnings. Drift, forward and prompt are the
   exception to the exception: they only advised before #36, so under `--jev-shadow` they
   record what they would do (`would_add`, `shadow: true`) and change nothing in the plan.
6. **flags.** `--gate`, `--no-gates` and `--decide-budget` on `plan` or a solo run, frozen in
   `<plan-or-run>/jev-selection.json`. `--no-gates` turns every gate off after `--gate` is
   applied. A re-plan replaces the file. Solo again on the same run dir keeps it. `run` and
   `retry` keep it too, and say so when flags are passed again. A plan with no selection file
   takes the flags on `run` or `retry` and freezes them then.

After those six, `FORGE_JEV=off` turns every gate off (skip `kill`). A repo privacy floor then
turns every gate off (skip `privacy`) and wins over the kill switch. Both send nothing.

`providers` and `budget.daily_*` count only in the user file. `privacy` counts only in the repo
file. Anywhere else they are ignored, and `doctor` reports them as misplaced.

### Providers

Two types, each hosted or local. `jev` is TypeSafe's request shape; hosted, the URL defaults
to `https://api.typesafe.ai/v1/systemone` and the model to `jev-latest`. `clef` is Cloudflare's
envelope; hosted, pass `--url`, or `--account-id` (else `CLOUDFLARE_ACCOUNT_ID`) to build the
Workers AI URL. The default model is `clef`.

```sh
forge decide provider add jev --name typesafe --key-stdin
forge decide provider add clef --name cloudflare --account-id <id> --key-env CLOUDFLARE_API_TOKEN
forge decide provider add jev --name local --url http://127.0.0.1:8080 --local
forge decide provider list
forge decide provider rm local
```

`--context N` is the context limit in tokens. A request over 90% of it is skipped `too-large`
and the state is not cut to fit. `--price X` is USD per million tokens, which the budget uses.
`--deadline S` is seconds per request; the default is 5. `--local` requires `--url`, sends no
key and adds no cost.

A key is never an argument. `provider add` reads one line from `--key-stdin` and writes
`${XDG_CONFIG_HOME:-~/.config}/forge/keys/<name>.key` (mode `0600`, the `keys/` directory
`0700`), stores a variable name with `--key-env VAR` and reads it when a request is made, or
prompts on a terminal. Adding the same name again without a key keeps the key it has. `status`
and `provider list` show `present` or `missing`. `provider rm` refuses while a gate names that
provider, and deletes the `keys/<name>.key` it wrote.

### Budgets

Two caps, each in tokens and estimated USD. Unset means no cap.

```sh
forge decide budget                              # caps, and what today has spent
forge decide budget --run tokens=20000,usd=0.50  # this run
forge decide budget --daily tokens=200000,usd=5  # user level; a repo file cannot set it
forge decide budget --run none                   # clear the per-run cap
forge decide budget --repo DIR --run tokens=5000 # per-run cap for one repo
```

`--daily` is refused together with `--repo`. `none`, or `tokens=none` / `usd=none`, clears
that cap. A number is zero or more.

Today's spend is `${XDG_STATE_HOME:-~/.local/state}/forge/decide/spend/<YYYY-MM-DD>.json`
(local date; a new day is a new file). The run's own counter is `<run-dir>/decide.budget.json`
and shows up in `status` when `FORGE_DECIDE_RUN_DIR` is set. `forge decide budget` and
`forge decide status` both print today's tokens and USD.

Once any set cap is reached, every later decision-model call is skipped with reason `budget`
and the run continues. A local provider, or one with no `--price`, adds tokens and zero USD.
A replay (`forge decide replay --send`) is charged to neither cap, and a request-cache hit
costs nothing. See [Measuring a gate](#measuring-a-gate).
`--decide-budget tokens=N[,usd=X]` on a run sets that run's cap at the flags level and does
not change the daily cap.

### Privacy floor

```sh
forge decide privacy never-send --repo DIR
forge decide privacy allow --repo DIR
```

`never-send` writes `"privacy": "never-send"` into `<DIR>/.forge/decide.json`. Every gate is
then off (skip `privacy`): no decision-model request leaves that repo, and `doctor --live`
does not probe. The floor beats every level, including run flags and `FORGE_JEV=off`. A
replay skips that repo's stored requests too.

Removing it is a per-repo edit: `privacy allow --repo DIR` deletes the key from that repo's
file. The user `decide.json` cannot set or clear it.

With no floor, a gate that is `shadow` or `on` sends that site's state to its provider.
`.forge/memory.md` is part of it: prompt adequacy includes its first 4000 characters, the
memory gates send the entries they are judging, and verify discover sends the bullets under
`## Verify`. Drift's candidate file list leaves out `.forge/` and `.git`; that filter applies
to the candidate list only. Keep private text out of `.forge/memory.md`, or set the floor.

### Migration

```sh
forge decide migrate            # once
forge decide migrate --force    # replace the user decide.json from jev.json again
```

`migrate` reads `${XDG_CONFIG_HOME:-~/.config}/forge/jev.json` and a Cloudflare token
(`cloudflare.env` beside it, or `CLOUDFLARE_API_TOKEN`) and writes the user `decide.json`:

- A TypeSafe key becomes the provider `typesafe` (`type` `jev`, URL and model from
  `jev.json`). The key moves to `keys/typesafe.key`.
- Cloudflare becomes the provider `cloudflare` (`type` `clef`). A token already in
  `cloudflare.env` stays in that file; one stored in `jev.json` moves to
  `keys/cloudflare.key`; one that lives only in the environment is referenced as
  `key_env` `CLOUDFLARE_API_TOKEN`.
- When `jev.json` is `enabled`, the `memory` gates and `verify-discover` are set `on`, and
  `drift`, `forward` and `prompt` are set `shadow`. A capability switched off is left unset,
  so its gates stay `off`. The five gates added in C3–C5 have no legacy capability, so migration
  leaves them unset. The `on` gates are written with `"force": true`, so they show as
  forced until a replay passes for them.
- A site mapped to `cloudflare` points at that provider; every other site points at
  `typesafe`, when that provider was created. `forge jev setup --provider cloudflare` mapped
  `jev-drift` by default, and migrate copies that map.
- Each stored threshold is copied onto the gates that read it (`drift_warn` onto `drift`,
  `forward_warn` onto `forward`, `prompt_warn` onto `prompt`, `memory_merge`,
  `memory_recategorize` and `gate_warn` onto `memory-curate`, `gate_warn` also onto `drift`,
  `stale_drop` onto `memory-stale`, `tests_act` onto `verify-discover`). Per-model values for
  the provider's model override those and set `thresholds_model`. Retired thresholds are not
  copied.

While the user `decide.json` does not exist, runs still follow `jev.json` and `FORGE_JEV*`.
On that path `FORGE_CLEF=off` sends every site to TypeSafe, `FORGE_JEV_PROVIDER` forces
`typesafe` or `cloudflare`, and `TYPESAFE_API_KEY` overrides the stored key.
`status`, `doctor`, `provider list` and a bare `budget` never write, so they do not migrate.
The migration runs before a write to the user file: `gate` without `--repo`, `provider add`,
`provider rm`, `budget` with `--run` or `--daily` and without `--repo`, and `migrate`.
A repo-only write (`privacy`, `gate --repo`, `budget --repo`) leaves `jev.json` in place.
After `decide.json` exists, which gates are on comes from it.

`forge jev` remains an alias for this release. Next release it is gone.

### Doctor

```sh
forge decide doctor [--repo DIR] [--json]
forge decide doctor --live
```

Offline, it reports settings that went stale. Nothing it prints contains a key. Exit 0 when
the only findings are advice, exit 1 when a problem is found or a live probe fails.

Advice, which does not fail the command: `threshold-override` (a stored threshold differs from
the measured default), `shadowed` (the user file wins over the repo), `privacy-floor`.

Problems: `retired-threshold`, `unknown-provider` (a gate names a provider that is not
configured), `unused-provider`, `misplaced` (providers or a daily budget in a repo file, or
`privacy` in the user file), `key-file` (missing, or not mode `0600`), `not-migrated`
(`jev.json` exists and `decide.json` does not), `config-error`.

`--live` sends one fixed synthetic request, with no repo data, to each configured provider.
It is the only `forge decide` subcommand that uses the network, and it is skipped when the
repo has a privacy floor.

### Cloudflare Clef

Point a gate at that provider with `forge decide gate drift on --provider <name>`, once a
replay has passed for it (see [Measuring a gate](#measuring-a-gate)). A paired replay on
2026-10-16 found hosted Clef non-inferior on drift: AUC 0.985 against
Jev's 0.972, paired difference [0.000, +0.028], on 4560 decisions from 17 runs. A site moves
on the rule in [Promotion](#promotion). `jev-memory-slice` tied (AUC 0.951 against 0.953) on
5 runs and stays on a `jev` provider. No per-model threshold is set, so Clef reads the shared ones.

The client sends at most 64 questions per request (drift asks up to 120, so that request is
two concurrent chunks) and refuses `too-large` past the context, 64K tokens for `clef` and
24K for `clef-flash`, or whatever `--context` set. The service would otherwise cut the state.
Rate limits are retried inside the deadline; quota and plan errors are final. A response
missing an answer, or with one out of range, is `malformed`.

## Measuring a gate

A gate becomes `on` by the same path for every site:

1. **Shadow records.** With the gate `shadow`, the site asks. The answers go to `jev.jsonl`,
   and the full request and its answers go to the run dataset. The run goes on as it would
   with the gate off.
2. **Labels arrive for free.** After the run, the dataset shows what happened next: which
   undeclared file the task edited, which command the repo documents. Nobody labels anything.
   See [Labels](#labels).
3. **Replay compares.** `forge decide replay` re-asks the stored requests on one provider and
   model, and scores the answers against those labels and against the site's baseline, a cheap
   heuristic that uses no model.
4. **Promotion.** The gate may be `on` for a provider and model only when the replay for them
   passes. Otherwise `forge decide gate G on` is refused, unless it is forced. See
   [Promotion](#promotion).

A replay measures one provider and model. A passing replay on `typesafe` does not cover `local`.

### Labels

Each site's label is one sentence in its record in `forge_decide/sites.py`. Labels mean
**should act**: a label is true when the gate should have acted (sent the work back, added the
file or the dependency, bounced the attempt, named the culprit), and false when it should not
have. A label never comes from the gate's own action: whatever followed a send-back that the gate
made is left unlabelled, or labelled by evidence the gate did not produce.

A site whose threshold reads the other way round from its score sets `invert` in its record.
Two do:
- **Prompt adequacy.** Its model answers "is the prompt adequate", so the probability of acting
  is `1 − p`, and its score is that. `replay` compares the score with `1 − prompt_warn`, the
  same line as `p < prompt_warn`.
- **Findings.** Its score is the Noul of "does it read as a preference", the probability of
  deselecting, and its baseline is 1.0 for a finding with no concrete cue. Its threshold
  `findings_concrete` is on the concreteness scale (`1 − p`), so `replay` compares the score with
  `1 − findings_concrete`.

`report` counts each site's fired decisions with the site's own rule, which already reads the
threshold the right way, so it needs no inversion of its own.

`forge_decide/labels.py` computes the labels from the run dataset with the joins of `forge
dataset export`, so a run the dataset did not capture has no labels. The gate modules' labellers
(`label_prompt`, `label_coverage`, `label_addressed`, `label_message`, `label_attribution`) are
loaded lazily, so a site whose labeller is missing has no rows.

| site | label: true means should act | precision floor |
|---|---|---|
| `jev-drift` | the undeclared file was edited by the task's final passing attempt (true), or was not (false). A file the gate added to `files` is labelled by the same edit | 0.5 |
| `jev-forward` | a later plan revision of the run has the dependency and the gate did not add it (true); both tasks merged and no revision has it (false). A dependency the gate added is false only once a later revision removes it, the user's correction, and is otherwise unlabelled | 0.5 |
| `jev-prompt` | the prompt was inadequate (true): a later plan revision changed it before its first attempt started, or the first attempt's review calls it ambiguous or unclear. False when the first attempt was reviewed and neither held. No label for a task the gate sent back, a task that never ran, or one whose first review is missing or unparseable | none |
| `jev-memory-slice` | the fact's anchor was edited by the task (true), or lies in an area the task never touched (false); a request needs both classes | none |
| `jev-verify-discover` | the candidate is the command the repo documents: its `.forge/verify`, or the command a later run of the same repo verified with, from `--verify` or `repo.verify` (command source not `jev`) | 0.7 |
| `jev-coverage` | the requirement is uncovered (true): a later plan revision has a task whose acceptance criteria hold at least 60% of the requirement's content words, and the tasks the request was asked about did not. Covered (false): every task of the run merged and no revision changed any acceptance criteria. No label for a requirement the gate sent back | none |
| `jev-retry-addressed` | the review of the same attempt raised the finding again (true); that review parsed in full and did not (false). No label without a review (a bounced attempt has none), or when its findings cannot be read in full | none |
| `jev-dwarf-message` | `unfinished` and `blocked`: the QA verdict of the same attempt was FAIL (true) or PASS (false). `question` has no label. No label for an attempt with no review, which is what a send-back leaves | none |
| `jev-attribution` | `culprit:<id>`: the merge breaks verification (true) when the search verified prefix k−1 PASS and prefix k FAIL; false when both were verified and do not form that pair. No label when either prefix was not verified, the base fails, or the search was inconclusive | none |

Verify discover has a label only for a repo that documents a command. Memory slice labels a
request only when it has both classes.

Three sites have no label, so `forge decide replay` refuses them, and their `on` needs
`--force`. Each is listed with the reason its label is not yet defined:

| site | why it has no label |
|---|---|
| `jev-memory-curate`, `jev-memory-stale` | `#41: recurrence is influenced by the gate itself; needs independent contradiction evidence`. An injected fact gets re-recorded, so the gate's own action would decide the label |
| `jev-findings` | `#46: needs issue-closure records`. Auto mode would supply its own ground truth, and an operational non-filing (a cap, a same-titled issue) is not a judgment |

### forge decide report

`forge decide report` reads the `jev.jsonl` files of past runs and the dataset's labels. It
asks no model.

It reads these runs:

- by default, the newest 10 runs in this repo's run registry, by the run directory's
  modification time. `--runs N` changes the count;
- with `--all`, every repo's registered runs, cut the same way: the newest 10 across all repos,
  or the newest N with `--runs N`;
- with `--run DIR` (repeatable), just those directories. It cannot be combined with `--all` or
  `--runs`.

In each run directory it reads every `jev.jsonl`: the run's own, and those under `tasks/*/`
and `integrate-*/`. Anything under `jev-backtests/` is ignored. `--site S` keeps one site.

It prints one row per site, sorted by input tokens, then a `total` row:

| column | meaning |
|---|---|
| `mode` | the gate's mode for this repo, with `(forced)` when it is `on` without a passing replay. `-` for a site with no gate |
| `calls` | every logged request, including cache hits and skips |
| `cached` | requests answered from the request cache, at no cost |
| `errors` | failed requests with no skip reason |
| `skipped` | failed requests with a reason. The line under the table counts each reason |
| `in tokens`, `out tokens` | from each request's usage. A cache hit has none |
| `cost $` | tokens times the provider's price per million tokens, from the user `decide.json` (`provider add --price`). `-` when a provider in use has no price |
| `fired` | requests the site acted on at its threshold for the model used. `(+N?)` counts requests it could not judge, since verify discover needs the stored request body |
| `right` | decisions the site fired on whose label is true |
| `wrong` | decisions the site fired on whose label is false |
| `missed` | decisions the site did not fire on whose label is true |

`right`, `wrong` and `missed` count decisions, one per question, not requests. They read `-`
for a site with no label, and `0` when the runs carry no dataset or no labelled decision yet.
`--json` also gives `labelled.decisions`, which the table does not show.

An example, from a small test run directory with no dataset. Drift's label columns read `0`
because there is no dataset; prompt has no label, so its columns read `-`:

```text
site        mode         calls  cached  errors  skipped  in tokens  out tokens  cost $  fired  right  wrong  missed
jev-drift   on (forced)      3       1       0        1      1,200          80  0.0019      2      0      0       0
jev-prompt  off              1       0       0        0        300          20  0.0005      1      -      -       -
total                        4       1       0        1      1,500         100  0.0024      3      0      0       0
skipped: budget 1
runs read: 1: …/run
```

### forge decide replay

```sh
forge decide replay --site jev-drift --provider typesafe --model jev-latest           # dry run
forge decide replay --site jev-drift --provider typesafe --model jev-latest --send    # ask, judge, record
```

Run the dry run first. It counts the labelled requests the replay would ask, and prints how
many are already answered, how many would be sent, how many the privacy floor skips, the input
tokens and the cost at the provider's price. It sends nothing and writes nothing.

`--send` asks each unanswered request, `--workers` at a time (default 3), then judges the
answers. A request already answered is never sent again, so an interrupted replay resumes. A
failed request is asked again by the next `--send`.

It takes the site's labelled live requests from the dataset, the first per request key.
`--runs N` keeps the newest N dataset runs. A request from a repo with the privacy floor is
never sent. The floor is read from that repo's `.forge/decide.json` at replay time, and the
request is counted under `privacy floor`.

The answers and the result are stored under `${XDG_STATE_HOME:-~/.local/state}/forge/decide/`:

- `replay/<site>/<provider>/<model>.jsonl`: one line per request, with its answers or its
  failure reason. Each component is percent-encoded, so `--model @cf/meta/llama` is stored as
  `replay/<site>/<provider>/%40cf%2Fmeta%2Fllama.jsonl`;
- `replays/<gate>.json`: the result, under `"<provider>/<model>"`. `forge decide gate G on`
  reads this file. A result is recorded only when at least one labelled decision was judged.

On the judged half of the runs (the newer half, see [Promotion](#promotion)), `--send` reports:

- requests answered, and requests skipped by the privacy floor;
- decisions, positives and runs;
- the AUC of the replay, of the baseline, and of the live answers, one per live model;
- replay minus baseline, with its 95% interval and the margin;
- precision, recall and the number flagged at the site's threshold, against its floor;
- `PASSES: passes`, or `does not pass:` with the first rule that failed.

A replay is not charged to the run or daily budget, and its requests are not written to
`jev.jsonl` or the dataset. Each `--send` is a deliberate call to the provider.

Exit codes: 2 for an unknown site, a site with no label, an unknown provider or a bad flag; 1
when the provider has no key and is not local.

### Promotion

`forge decide gate G on`, for the provider and model the gate resolves to, writes `on` only when
the replay of G on that provider and model passes, or when it is forced (see
[Forced gates](#forced-gates)). It passes when, on the judged half:

1. there are at least 30 labelled decisions;
2. they come from at least 8 judged runs. Runs are ordered by their first labelled request,
   and the older half calibrates while the newer half is judged. So a site needs 15 runs before
   it has 8 to judge;
3. the lower bound of the 95% paired interval of AUC(replay) − AUC(baseline) is above −0.02.
   The interval resamples whole runs 2,000 times. `--seed` (default 7) sets the resamples and
   `--margin` (default 0.02) the bound. The verdict uses the interval that is reported;
4. for `drift`, `forward` and `verify-discover`, the replay's precision at the site's
   threshold is at least the floor: 0.5, 0.5 and 0.7. The threshold is `drift_warn`,
   `forward_warn` or `tests_act`, for the model replayed. `memory-slice` has no floor, so it
   passes on rules 1 to 3.

A passing replay shows as `replayed <date>` in `status`.

### Forced gates

`--force` turns a gate on without a passing replay:

```sh
forge decide gate drift on --provider typesafe --force
```

It writes `"force": true` into the gate's entry. That lets the write through, and lets
`forge config set decide.gate.<gate>.provider` or `.model` change an `on` gate without a
replay.

"Forced" describes the state, not the flag. A gate is forced when it is `on` and no passing
replay exists for its provider and model, whichever level turned it on: the user file, the
repo, a profile, an environment variable, a flag, the legacy `jev.json`, or `migrate`. It stays
forced until a replay passes. Setting it to `off` or `shadow`, or to `on` with a passing
replay, drops the flag too.

Forced shows in:

- `forge decide status`: `forced` after the gate's provider, as in
  `drift on (user) provider typesafe, forced`. `status --json` has `"forced": true` for the gate;
- `forge decide report`: the mode column reads `on (forced)`;
- `forge config show --json`: the same `forced` field. The text view does not print it;
- the plan's decide line, as in `forge: decide: on: drift (forced); shadow: none`;
- forge-ui: a `forced` tag on the gate, in Settings under Decision models. A recorded replay
  shows as `replayed <date>`, or as `replay failed` when it did not pass.

`forge decide migrate` writes each gate it turns on with `"force": true`. Those gates keep
acting, and show as forced until a replay passes.

### The request cache

Each decision-model request is keyed by its body: the state, the model and the questions.
Answers from live calls are kept in `<run dir>/decide.cache/`, one file per request and
provider (name and URL). Each file holds the request's hash and the answers, not the request.
The run dir is the plan dir or the solo run dir (`FORGE_DECIDE_RUN_DIR`), so every task of a
plan shares one cache.

The cache answers a repeated request in the same run dir:

- a `run` that resumes a plan, and a `retry`;
- a re-`plan` of the same plan dir;
- a QA-only resume, where the dwarf's work is kept and only QA runs again.

A changed state, model or question is a miss, and so is a different provider or URL.

A hit costs nothing and is not charged to a budget, even when a cap is reached. It is logged in
`jev.jsonl` with `"cached": true`, counts under `cached` in `forge decide report`, and is not
written to the dataset a second time. Only live answers are cached: failures, fixtures and
replays are not. The privacy floor is checked before the cache, so a hit cannot get past it.

To turn the cache off, set `FORGE_DECIDE_CACHE=off` (`0`, `no` and `false` work too) in the
run's environment. Every request is then sent, and nothing is written to the cache.

## Adding a site

A new site takes this path, in this order. Skipping a step is how the sites in
[Tried and removed](#tried-and-removed) shipped.

1. **A rubric in `questions.py`.** A typed question with its own wording and its own threshold
   name. Never reuse another rubric's threshold: `coupling_warn` borrowed `gate_warn` and never
   fired once on any plan.
2. **Asked through `client.ask` with a site name** (`jev-<name>`). `client.ask` is the only
   place that talks to a provider; it handles the key, the deadline, the provider mapping and
   the fail-open skip, and it records the request.
3. **Gated in every copy of the gate list.** The gate's name goes into `GATES`, its site into
   `GATE_SITE`, and its legacy capability into `GATE_CAPABILITY`, all in
   `scripts/forge_decide/__init__.py`. The capability is `None` when the gate has none: the legacy
   level then never turns it on. The same names are also hard-coded
   in these places, and each one needs the new name:
   - `scripts/forge_jev/__init__.py`: `SITE_CAPABILITY`, for every site, and `DEFAULT_THRESHOLDS`
     for each threshold the rubric reads;
   - `scripts/forge_decide/migrate.py`: `THRESHOLD_GATES`, the gates each stored threshold is
     copied onto;
   - `scripts/forge-jev-options.sh`: `FORGE_DECIDE_GATE_NAMES` and the gate names in its help
     text (`tests/test_decide_flags.py` keeps it in step with `forge_decide.GATES`);
   - `ui/forge_ui/control.py`: `DECIDE_GATES`;
   - `ui/forge_ui/static/settings.js`: `GATES`;
   - `ui/tests/test_browser_settings.py`: `_GATES`;
   - `ui/tests/js/settings.test.js`: the list its assertions compare the page with.

   Three tests spell the list out by hand and need the name too: `tests/test_config.py`,
   `tests/test_decide_cli.py` and `tests/test_gate_registry.py`.

   The shell asks `forge_decide_active <gate>` and the Python side asks `site_ready(site)`; both
   must be false when the gate is off, `FORGE_JEV=off` is set, the privacy floor is on, or the
   provider cannot be asked.
4. **Recorded in the run dataset.** The request must land in `jev.jsonl` and the run dataset
   whole, state included. The labeller reads it back from the dataset and labels it from what
   happened afterwards.
5. **A record in `forge_decide/sites.py`, with its functions taken lazily.** One `Site` per site:
   its `fired`, `decisions`, `score` and `baseline`, the label sentence or a `label_issue`, the
   floor if it has one, and `invert=True` when the raw probability points away from acting. A
   site whose functions live in its gate module takes them through `_lazy(module, attr, default)`,
   so `sites.py` never imports a module another task is still writing. While the module is
   missing the site acts on nothing: `fired` and `decisions` give `[]`, `score` gives None and
   `baseline` gives 0.0.
6. **A labeller, read lazily.** `forge_decide/labels.py` finds a site's labeller in `LABELLERS`.
   A labeller has the shape `label(index, record, event, body) -> {qid: bool}`. One in its own
   module, `forge_decide/label_<name>.py`, is registered as `sites._lazy('forge_decide.label_<name>',
   'label', {})`, so the site has no rows until the module exists.
7. **The measurement functions and the CLI verb.** `forge_jev/<module>.py` exports `decisions`,
   `score`, `fired` and `baseline` over the stored body, and `main(args)` for its verb. In
   `forge_jev/cli.py`, `cmd_<verb>` checks its paths (exit 2) and calls
   `_delegate(verb, module, args, repo)`, which imports the module and returns its `main`. A
   missing module is exit 3. Exit 3 always means no opinion: skipped, shadow, or nothing to say.
   Each `main` checks `site_ready(site)` first. The verb goes into `parser()` and into the dispatch
   table in `main()`.
8. **A run-time gate puts its attempt into the request state.** A gate that runs once per attempt
   puts `run` (the plan's `run_id`), `task` and `attempt` into the request's **state**, as
   `dwarf-message` and `retry-addressed` do, and not only into `client.ask(context=...)`. The
   request key, and with it the cache, then differs per attempt and per run. `labels.requests`
   keeps only the first request per key, so without this a message repeated on a later attempt,
   or in another run, would be neither asked nor labelled again. A per-run gate (`attribution`)
   puts `run` alone.
9. **Replayed against labelled outcomes before it may act.** The label must come from outside the
   model. Then measure the site on the state production sends, and report the class separation,
   the noise floor from repeated identical requests, and the baseline from the criteria above.
   `forge decide replay` judges it by the rule in [Measuring a gate](#measuring-a-gate). A number
   measured on state the model is not sent describes nothing: it has bitten this file seven times.
10. **Ships in `shadow` first.** `shadow` asks and records, and changes nothing. A new site moves
    to `on` only after step 9 holds on live runs. What `on` may then do is one of the actions in
    [Gates and modes](#gates-and-modes): it never changes a verdict, never blocks a dispatch, and
    never makes work pass.

## Tried and removed

All of these were removed on 2026-10-27 (#35) unless noted, each on a measurement. None should
be retried without new data that answers the measurement below.

**Routing rubrics and the tier composite.** Five `Score` rubrics per task (design judgment,
concurrency subtlety, blast radius, spec clarity, test coverage) were weighted into a tier, with
a "suggests <tier>" note and a `--jev-act` mode that could write it into the `difficulty`
column. No repo ever reached the calibration floor, so it never acted (removed 2026-10-17).
Over the run dataset its score predicted a first pass about as well as cheap features (AUC 0.69
against 0.64–0.67 on older runs), and on the newest runs every method sat near chance. A model
swap on Clef explained 2% of the variance in its predictions: a decision model judges how hard
a task is, not which model does it better. Models and efforts are measured by randomized
routing instead. `--jev-act` stays as a no-op, and `--jev-routing` still parses.

**`independently_verifiable`.** A gate for tasks whose confirmation needs another task's work.
Its first form asked about a sibling task the model was never shown. With the whole plan in
state it still could not separate a task documenting later work (`readme`, 0.60–0.65) from a
real one (`cli`, 0.65–0.75). At the old 0.38 it caught nothing on a real plan, and at 0.55 it
was a precision gate whose silence meant "no evidence", not "verifiable".

**Same-wave coupling.** Asked whether two tasks in one wave share an interface. It never fired
on any plan until measured, because it borrowed `gate_warn` (0.60) while coupling scores sat at
0.09–0.38. With its own `coupling_warn` 0.20 and a cap of three pairs, it rested on a single
plan with a margin of about 0.02. Cross-wave forward dependency, with a gap of 0.73, is the half that stayed.

**Review triage.** After a FAIL it read the review against the diff and flagged an unsound one
(correctness and citation). It could never change the verdict, and on real runs it flagged 2 of 5
FAILs, both artifacts of the 24K diff cut rather than unsound reviews. The reviewer's
verdict was never its to change, so there was nothing left for it to do.

**`clef-first-pass` and `clef-retry-pass`.** Silent Clef predictions of whether a first attempt
or a retry would pass, recorded and never acted on. They ranked at AUC 0.73 and 0.75 but were
poorly calibrated (retry ECE 0.28), cost 8% of tokens and hit two 10-second timeouts, and never
learned: the newest, largest tasks were too few to test, so the skill never showed up on new
runs.

**Verify flake triage (`flake_act`).** Classified a failing verify command as
`real_regression`, `known_flake`, `environment` or `generated_drift` and reran once on a
corroborated flake. It was unreachable by default: the deterministic rerun (`--verify-retries`,
default 1) comes first and needs no model, so the triage only ran with retries set to 0, plus a
recorded trap sharing a distinctive token with the failure. Its memory readers were also empty
for every repo for a time. The deterministic rerun and the `FLAKY` line stay.

**The test subset.** Asked which tests a diff makes relevant, to run them early. The safe
threshold at recall ≥ 0.95 was 0.20 in one repo and 0.45 in another, a 2.25× spread, so no
global default could serve, and it only ever added a run: the full suite still ran at
integration. `.forge/verify-subset` stays, for `--fail-first`.

**`forge jev backtest` and the corpus.** Replayed git history to score the test rubric, and the
tracked `jev-backtests/` directory held its results. Its tally carried a 140M-token artifact, so
its numbers could not be trusted. Captured requests in the run dataset are what a site is
replayed against now, as in step 5 above.

**Earlier removals (2026-10-17).** Effort sizing (AUC 0.64 overall but 0.50–0.54 within a tier,
81% of suggestions `xhigh` or higher), `forge jev parse-spec` (no shorter than typing the
`--dwarf-<tier>` flags) and Fractal child re-rating (gated on a calibration no repo had).

Old plan dirs and dataset stores that hold routing, coupling or triage rows stay readable; they
are no longer written.
