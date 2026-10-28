# Decision models

A decision model is a small hosted model that answers narrow, typed questions (a yes/no
probability, a single choice) over state forge supplies, in about a second. forge uses two:
TypeSafe's **Jev** and Cloudflare's **Clef**, which takes Jev's request shape. Both are
optional. Read this when turning one on, when changing a site, or before proposing a new one.

- **Off by default.** Nothing is sent until a key is stored and Jev is enabled.
- **Always fail-open.** A timeout, an error, a refusal or a missing key is logged as a skip
  reason, and the run proceeds exactly as it would with the model off. A decision-model call
  can never fail a run.
- **Advisory.** A decision model never changes a `FORGE_VERDICT`, a task's tier or the model a
  task runs on, and it never marks anything verified itself. The memory sites do act when on
  (what `memory.md` keeps, files and injects), and verify discover picks the command that runs
  when none is configured; that command's own result decides verification. `--jev-shadow` is
  narrower than it sounds, see [Setup today](#setup-today).
- **`FORGE_JEV=off` sends nothing.** It is a hard kill switch and wins over every flag, every
  per-capability variable and the stored config.

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

## Setup today

> **About to change.** `forge decide` (#36) replaces this setup with per-gate configuration
> (off, shadow or on, a provider and a model per gate, user and per-repo config, budgets).
> This section describes what works until that lands.

```sh
forge jev setup                          # store a TypeSafe key, validate it, enable Jev
forge jev setup --provider cloudflare --account-id <id> [--model clef|clef-flash] [--sites a,b]
forge jev sites [SITE=typesafe|cloudflare]   # which provider answers each site, and whether it is ready
forge jev enable | disable [--capability tests|gates|memory]
forge jev status                         # resolved config, enabled capabilities
forge jev doctor [--live]                # config and key checks; --live probes every provider in use
```

**`--jev-shadow` only silences the plan-time warnings** (prompt, drift, forward): `plan`
records the judgments and prints "checked N task(s) in shadow mode (no change)". It does not
stop the memory and verify-discover consumers. With `memory` on, `forge-memory.sh` still
applies curate, slice and stale answers in shadow mode, and with `tests` on, a discovered
command still runs when no verify command is configured, so a `PASS` can follow from a
command forge chose (a loud `jev chose a verification command` note says so). To judge without
acting on those, leave `memory` and `tests` off (`--no-jev-memory --no-jev-tests`) rather than
relying on shadow.

Run flags (`run`, `plan`, solo): `--jev` / `--no-jev` (mutually exclusive), `--jev-shadow`,
`--jev-<cap>` / `--no-jev-<cap>` for `tests`, `gates` and `memory`. `--jev-routing` still parses
so old commands keep working, but the `routing` capability has no sites. `--jev-act` is a
no-op that prints one deprecation line. The resolved selection is frozen in
`jev-selection.json`, so resume and retry decide as the original run did. Old selections stay
readable.

Precedence, implemented identically by `scripts/forge-jev-options.sh` and `scripts/forge_jev/`:

1. `FORGE_JEV=off` wins over everything.
2. Any `FORGE_JEV_<CAP>=on` is an allowlist: only those capabilities run.
3. Otherwise `FORGE_JEV=on` (from `--jev`) or the stored config's `enabled` turns it on, and a
   capability explicitly off is subtracted.
4. Default: off.

Environment: `FORGE_JEV`, `FORGE_JEV_<CAP>`, `FORGE_CLEF=off` (every site back to TypeSafe),
`FORGE_JEV_PROVIDER=typesafe|cloudflare` (force one provider for every site),
`TYPESAFE_API_KEY` (overrides the stored key for that invocation).

**Privacy.** Each enabled capability sends a narrow slice of state to an external API:
`gates` sends the task prompt, goal, a title/files/deps view of sibling tasks, file excerpts
(the first 4000 characters of the task's declared files) and diff hunks; `memory` sends the
memory entries under consideration (curate: the new fact and the existing entries; slice: the
task and the fact lines; stale: the fact and the anchor file's content); `tests` sends
candidate commands with an excerpt of what each runs. Treat all of it as leaving the machine.

The `.forge/` and `.git` exclusion is narrower than "everything": it filters the **candidate
file list** drift builds from the diff, and nothing else. **`.forge/memory.md` is sent**: prompt
adequacy puts its first 4000 characters in the request state for every task, and memory curate,
slice and stale send entries from it. Private text in `.forge/memory.md` therefore reaches the
provider whenever `gates` or `memory` is on. Keep such text out of that file, or leave those
capabilities off for the repo. Files a task declares are excerpted whatever their path.

The TypeSafe key lives in `${XDG_CONFIG_HOME:-$HOME/.config}/forge/jev.json`, mode `0600`. A
Cloudflare token (Account > Workers AI > Read) lives in `cloudflare.env` beside it, mode
`0600`, or in `CLOUDFLARE_API_TOKEN`; the account id is in config or `CLOUDFLARE_ACCOUNT_ID`.
Neither is ever written to a run dir, a prompt, a log or argv. A site mapped to Cloudflare
sends it what it would have sent TypeSafe.

### Cloudflare Clef

Without a Cloudflare provider configured, every site asks TypeSafe. `setup --provider
cloudflare` maps only `jev-drift` by default, because a paired replay (2026-10-16) found Clef
non-inferior there: AUC 0.985 against Jev's 0.972, paired difference +0.013 [0.000, +0.028], on
4560 decisions from 17 runs. A site moves only if Clef is non-inferior on at least 30 labeled
decisions from at least 8 held-out runs; `jev-memory-slice` tied (0.951 against 0.953) on 5
runs and stays on TypeSafe. Thresholds can be set per model with
`model_thresholds.<model>.<name>`; none is set, so Clef reads the shared ones.

The client handles Clef's limits: at most 64 questions per request (drift asks up to 120, so it
goes as two concurrent requests); a context of 64K tokens (`clef`) or 24K (`clef-flash`), beyond
which the service cuts state silently, so the client refuses (`too-large`) rather than send it;
allocation and plan errors are final, rate limits are retried inside the deadline. Every
requested answer must be present and in range or the call is `malformed`.

## Adding a site

A new site takes this path, in this order. Skipping a step is how the sites in
[Tried and removed](#tried-and-removed) shipped.

1. **A rubric in `questions.py`.** A typed question with its own wording and its own threshold
   name. Never reuse another rubric's threshold: `coupling_warn` borrowed `gate_warn` and never
   fired once on any plan.
2. **Asked through `client.ask` with a site name** (`jev-<name>`). `client.ask` is the only
   place that talks to a provider; it handles the key, the deadline, the provider mapping and
   the fail-open skip, and it records the request.
3. **Gated through `site_ready`.** Add the site to `SITE_CAPABILITY`. The shell asks
   `forge_jev_active <capability>` and the Python side asks `site_ready(site)`; both must be
   false when the capability is off, `FORGE_JEV=off` is set, or the provider has no credentials.
4. **Recorded in the dataset.** The request must land in `jev.jsonl` and the run dataset whole,
   and the export (`forge dataset export`) needs a slice that labels it from what happened
   afterwards.
5. **Replayed against labelled outcomes before it may act.** Measure on the state production
   sends, with a label that comes from outside the model, and report the class separation, the
   noise floor from repeated identical requests, and the baseline from the criteria above. A
   number measured on state the model is not sent describes nothing: it has bitten this file
   seven times.
6. **Ships advisory first.** It prints or records and cannot block a dispatch. A new site must
   also honour `--jev-shadow` itself (today only the plan-time warnings do). It acts on
   something only after step 5 holds on live runs, and never on a verdict, a tier or a model.

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
