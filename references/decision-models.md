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
- **Advisory.** A decision model never changes a `FORGE_VERDICT`, a task's tier or the model a
  task runs on, and it never marks anything verified itself. With the gate `on`, the memory
  sites act (what `memory.md` keeps, files and injects) and verify discover picks the command
  that runs when none is configured; that command's own result decides verification. Drift,
  forward and prompt only print a note. `shadow` asks and records, and changes nothing. See
  [Setup](#setup).
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
| drift | warns at plan time that a task may edit undeclared files | precision at `gate_warn` 0.60 over 50 commits × 2 repos: **1.000** and **0.934**; non-edited files scored ≤0.23 where the true file scored 0.65 | the post-wave `drift.txt`, which reports the broken promise only after the wave ran | one request per task |
| forward | warns that an earlier-wave task may be written against what a later one builds | 7 cross-wave pairs, 5 runs each: `readme`→`cli` **0.88–0.90**, every other pair 0.05–0.15 (gap 0.73, noise 0.07) | wave order, which cannot see the case; the review cannot either | one request per plan |
| prompt adequacy | warns that a prompt is too vague for a reviewer to judge correctness | 3 runs over 8 probe prompts and 8 real ones: unjudgeable **0.06–0.27**, checkable behaviour **0.85–0.96** | silence; the old 0.30 sat 0.03 above the worst true positive | one request per task |
| memory curate, slice, stale | refiles, merges, hides or drops a fact in `memory.md` | merge: wrong 0.56–0.72, right 0.91–1.00. Refile: wrong 0.56–0.72, right 0.92–1.00. Stale: gone false 0.03–0.10, still true 0.47–0.96 | exact-match keys (two wordings of one fact never reach two distinct runs) | curate at `record`, slice at `inject`, stale only on `prune` |
| verify discover | picks a verify command when none is configured | 18 repos: 10 fire at 0.70, every pick correct, lowest correct 0.73, highest wrong 0.55 | `UNVERIFIED` | one request, only when nothing is configured |

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
columns; thresholds are `DEFAULT_THRESHOLDS` in the same file.

| site | capability | runs | can change | threshold | recorded |
|---|---|---|---|---|---|
| `jev-prompt` | `gates` | `plan`, per task | prints a "prompt may be too vague" note | `prompt_warn` 0.55 | `tasks/<id>/jev.json` |
| `jev-drift` | `gates` | `plan`, per task | prints "may also edit undeclared files" | `drift_warn`, falls back to `gate_warn` 0.60 | `tasks/<id>/jev.json` |
| `jev-forward` | `gates` | `plan`, once | prints "runs before X and may depend on it" | `forward_warn` 0.50 | `jev-coupling.json` |
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

forge decide gate memory-curate on --provider typesafe
forge decide status
```

Name a provider to choose where a gate's requests go. The local server above is
`forge decide gate memory-curate on --provider local`. A gate with no `--provider` does not
read the key file `provider add` wrote: it uses the legacy transport, `jev.json`'s endpoint
with `TYPESAFE_API_KEY` or `jev.json`'s key (or Cloudflare, for a site `jev.json` maps
there). Migration leaves `jev.json` in place, so with either key present such a gate still
sends requests, wherever `jev.json` points.

`status` prints each gate's effective mode and which level set it, the providers (key
`present` or `missing`), today's spend, and whether a privacy floor is on. It never writes.
forge-ui's Settings page has the same fields under Decision models; a change there writes the
user file.

### Gates and modes

Seven gates, each `off` (the default), `shadow` or `on`:

`drift`, `forward`, `prompt`, `memory-curate`, `memory-slice`, `memory-stale`, `verify-discover`.

`off` asks nothing. `shadow`, on every gate, asks and records the answer, then leaves the run
as it would have been with the gate off: no note, no memory edit, no verify command.

`on` in this release:

| gate | what `on` does |
|---|---|
| `drift` | prints the "may also edit undeclared files" note |
| `forward` | prints the note that a task runs before another and may depend on what it builds |
| `prompt` | prints the "prompt may be too vague" note |
| `memory-curate` | refiles, merges or withholds the fact |
| `memory-slice` | removes lines from the injected slice |
| `memory-stale` | drops a fact that went false |
| `verify-discover` | runs the chosen command when none is configured |

Drift, forward and prompt stay advisory when `on`. The three memory gates and verify discover
act. Verify discover still does not mark anything verified: the command it picked does, or
`UNVERIFIED` stands.

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
   prints a count in shadow mode instead of its warnings.
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
`--decide-budget tokens=N[,usd=X]` on a run sets that run's cap at the flags level and does
not change the daily cap.

### Privacy floor

```sh
forge decide privacy never-send --repo DIR
forge decide privacy allow --repo DIR
```

`never-send` writes `"privacy": "never-send"` into `<DIR>/.forge/decide.json`. Every gate is
then off (skip `privacy`): no decision-model request leaves that repo, and `doctor --live`
does not probe. The floor beats every level, including run flags and `FORGE_JEV=off`.

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
  so its gates stay `off`.
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

Point a gate at that provider with `forge decide gate drift on --provider <name>`. A paired
replay on 2026-10-16 found hosted Clef non-inferior on drift: AUC 0.985 against
Jev's 0.972, paired difference [0.000, +0.028], on 4560 decisions from 17 runs. A site moves
only after Clef is non-inferior on at least 30 labeled decisions from at least 8 held-out
runs. `jev-memory-slice` tied (AUC 0.951 against 0.953) on 5 runs and stays on a `jev`
provider. No per-model threshold is set, so Clef reads the shared ones.

The client sends at most 64 questions per request (drift asks up to 120, so that request is
two concurrent chunks) and refuses `too-large` past the context, 64K tokens for `clef` and
24K for `clef-flash`, or whatever `--context` set. The service would otherwise cut the state.
Rate limits are retried inside the deadline; quota and plan errors are final. A response
missing an answer, or with one out of range, is `malformed`.

## Adding a site

A new site takes this path, in this order. Skipping a step is how the sites in
[Tried and removed](#tried-and-removed) shipped.

1. **A rubric in `questions.py`.** A typed question with its own wording and its own threshold
   name. Never reuse another rubric's threshold: `coupling_warn` borrowed `gate_warn` and never
   fired once on any plan.
2. **Asked through `client.ask` with a site name** (`jev-<name>`). `client.ask` is the only
   place that talks to a provider; it handles the key, the deadline, the provider mapping and
   the fail-open skip, and it records the request.
3. **Gated through `site_ready`.** Add the site to `SITE_CAPABILITY` and as a gate in
   `forge_decide.GATES`. The shell asks `forge_decide_active <gate>` and the Python side asks
   `site_ready(site)`; both must be false when the gate is off, `FORGE_JEV=off` is set, the
   privacy floor is on, or the provider cannot be asked.
4. **Recorded in the dataset.** The request must land in `jev.jsonl` and the run dataset whole,
   and the export (`forge dataset export`) needs a slice that labels it from what happened
   afterwards.
5. **Replayed against labelled outcomes before it may act.** Measure on the state production
   sends, with a label that comes from outside the model, and report the class separation, the
   noise floor from repeated identical requests, and the baseline from the criteria above. A
   number measured on state the model is not sent describes nothing: it has bitten this file
   seven times.
6. **Ships advisory first.** It prints or records and cannot block a dispatch. A new site must
   treat `shadow` as ask-and-record (the kept sites do). It acts on something only after step 5
   holds on live runs, and never on a verdict, a tier or a model.

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
