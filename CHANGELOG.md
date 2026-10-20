# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow the format `{major}.{minor}[.{patch}]` ([...] parts appear only when non-zero).

## [0.7.1] - 2026-10-20

A patch for forge-ui: orbiting its 3D views no longer stalls, and you can look around it on made-up runs before your first one.

### Added

- You can look around forge-ui before your first run: `python3 ui/tools/demo_runs.py DIR` writes made-up runs (one still working, and a few weeks of history across five projects), and `python3 -m forge_ui --no-registry --root DIR`, run from `ui/`, shows them.

### Fixed

- Orbiting forge-ui's 3D views no longer stalls. While the camera moved, every frame asked for two more, so the drawing doubled with each frame: a second of orbiting drew hundreds of times and the page stalled, worst in the run cloud.

## [0.7] - 2026-10-20

forge-ui grows from a run viewer into a full picture of your runs: a full-screen 3D cloud of every run, grouped by project and filtered down to exactly the runs you want, diffs that show each review finding on the line it names, and a model leaderboard that compares implementers and reviewers on your own history. It also gains 3D run views, quick jump, notifications, a files heatmap and one-file HTML exports.

### Highlights

- forge-ui has a run cloud (*Run cloud* in the top bar): every run at once, full screen in 3D, grouped by project. Choose how it is laid out (a helix per project, spheres, galaxies, a timeline with one lane per project, or a constellation that pulls together runs sharing models) and whether runs are blocks or orbs. The filter panel narrows the cloud to exactly the runs you want: projects, state, difficulty, tasks per run, the models that implemented or reviewed them, recent activity, verdicts and more. Orbit, zoom and drag any run wherever you want it; the cloud remembers where you put things in that browser, per layout, and *Positions* resets them or exports and imports them as a file. Click a run for a summary card, double-click to open it.
- forge-ui shows diffs with the review findings in place. From a task, any of its reviews or the final review, open the diff: the task's current, previous or committed changes, or exactly the diff a reviewer read, with every P0–P3 finding shown under the line it points at (and the ones that point outside the diff listed apart). n and p jump between findings. When a run used Fractal, a task also shows the child tasks it was split into, in the inspector and as a small tree floating over the opened task in 3D, each with its decision, cost, output and its own diff.
- forge-ui has a model leaderboard (*Models* in the top bar): for each implementer model, how often its first attempt passed (with a confidence interval), its retries, escalations away, timeouts, time and tokens; for each reviewer, its verdicts, findings per review and how often shadow reviews agreed with it. Filter by project, difficulty and period, or keep only the tasks where forge picked the model at random, the fairest comparison since routing is otherwise not random. Click a model to see its runs in the run cloud.

### Added

- forge-ui now draws your runs as a 3D map in archivision's style. The front page shows every run grouped by repo, with a lamp per task and filters that fade what does not match; a run shows its tasks in their planned waves, ending in integration, verification and the whole-run final review, and *Execution* shows the same run on wall-clock time, attempt by attempt, against `--max-parallel`. Click a task to open it to its attempts, reviews and escalations; the inspector beside it shows each step's exact prompt, model, timings, tokens, verdict and P0–P3 findings. Statuses update live, and you can replay a run, light its critical path, and size or colour tasks by duration, tokens, attempts or model. The 3D view loads three.js from cdn.jsdelivr.net the first time it is shown; offline, or with *2D* chosen, the page works without it.
- forge-ui has a quick jump and notifications. Press Ctrl K (⌘K on a Mac) or `/` anywhere to search every run, task, project and model and jump straight to it; Shift+Enter shows a run in the run cloud. The new bell tells you when a run finishes or ends with failures, when a task fails, and when a final review gives its verdict, with an unread count in the tab title; turn on browser notifications from the bell to hear about them while the tab is in the background.
- You can export a run from forge-ui as one HTML file to share (*Export…* in the run's header). The file opens in any browser without forge-ui, even offline (in 2D; in 3D when it can reach cdn.jsdelivr.net), with the run's plan, execution, inspector and findings. It asks each time what to include: prompts, reports and diffs are left out unless you tick them, and local paths are hidden unless you untick that.
- forge-ui shows which tasks touched which files: *Files* in a run's toolbar draws every file the run planned or changed against its tasks, wave by wave, with how many attempts changed each file, the files a task changed without planning to (drift), merge conflicts, and files two tasks of the same wave both worked on. Click a cell to select the task and open that file's diff.

### Changed

- Every plan, run and solo run now registers its run dir in forge's run registry, not only the ones opened with `--ui`, so forge-ui's map can show all of a repo's runs. While the forge-ui server runs, that means it can show every run on the machine; `forge ui stop` closes it, and `FORGE_RUN_REGISTRY=off` stops runs from registering. `forge-parallel.sh run` also records the `--max-parallel` it was given in the plan dir.

### Fixed

- forge-ui's 2D dependency graph shows its status colours again; the page's own security policy had been blocking them.

## [0.6] - 2026-10-20

Forge gets a companion: forge-ui, a read-only local page that watches any run live, now ships inside forge and is offered at the start of every run. Grok 4.7 joins as a sixth harness through xAI's grok CLI, and a grok session the CLI cancels partway through is now recorded instead of passing as finished.

### Added

- Forge can now dispatch through xAI's `grok` CLI. The `grok` alias runs Grok 4.7 there, at efforts up to `xhigh`, as an implementer or a read-only reviewer: `--dwarf grok:high`. Its answers and token counts are read like Claude's, and an expired login or a rate limit is reported as an infrastructure failure (exit 8). The previous route, Grok 4.6 through opencode, is still available as `grok:high:opencode`.
- Forge can now show a run live in a local web page. At the start of each new run `/forge` asks "Open forge-ui to watch this run? [y/N]"; answer yes (or pass `--ui`) and forge prints a `forge-ui:` link to the tasks, statuses, models, attempts and reviewer verdicts of a decomposed or solo run, and opens it in your browser. `--no-ui` skips the question. All runs share one local server that stops itself after four idle hours, and `forge ui open|status|stop` manages it by hand; `FORGE_UI=off` stops runs from launching it. A UI that cannot start never fails the run. The page has no login, so while the server runs, any program on your machine can read the prompts and diffs it shows, even for runs started with `--no-ui`; `forge ui stop` closes it. Solo runs now also record `status`, `attempts.tsv`, `dwarf.spec` and `qa.spec`.

### Fixed

- A grok session that the grok CLI cancels partway through no longer passes as a finished run. This happens when a tool call needs an approval that a headless run can't give, and the CLI still exits 0. forge now records it as `cancelled` in `<role>.infra`, together with the refused command, and says so in the run output. The work done before the cancel is still reviewed as usual.
- The exploration note under `plan`'s table now appears only when at least one row was actually drawn. It used to say "exploration on" even when every row named its own dwarf, or no tier had a neighbour to try. Its odds are also printed with a decimal point whatever your number locale (it showed "probability 0,1" under an Italian locale).

## [0.5] - 2026-10-19

Forge now picks models from measured results rather than a decision model's opinion: it explores your own pools by default, compares models and efforts per tier with forge dataset routing, moves a task to a stronger model after three failed attempts, and flags tasks too large to pass review first time. Jev's effort sizing, tier rewriting and calibration are gone, and Cloudflare's hosted Clef can answer chosen Jev decisions instead.

### Added

- With a Cloudflare provider configured, forge records two predictions it never acts on: whether each task's first attempt will pass review (once per plan, after models are assigned) and whether a retry will pass with the findings it was handed. They are written beside the task (`clef.first_pass`, `clef.retry_pass.<n>`) and into the run dataset, and the export attaches them to its routing and retry cases, so their accuracy on new runs can be measured before anything relies on them. No prompt, verdict, table or retry ever sees them; `FORGE_JEV_OUTCOMES=off` turns them off.
- Forge can send chosen decision sites to Cloudflare's hosted Clef instead of TypeSafe's Jev: `forge jev setup --provider cloudflare --account-id <id>` validates a Workers AI token (kept in `cloudflare.env` beside `jev.json`, never in argv or logs) and maps the sites a paired replay of forge's own live requests showed Clef no worse on, which today is drift prediction alone; `forge jev sites` shows or changes the map and `FORGE_CLEF=off` sends everything back. Thresholds can now differ per model, and `status` and `doctor` report readiness per site and provider. Without a Cloudflare provider configured nothing changes.
- After three failed attempts on one model, a decomposed task's next retry goes a tier up in the pools you gave `plan`, instead of repeating a dispatch whose fourth try passes least often. When no stronger model exists, the task stops with a split suggestion, and a plain `retry` refuses until you split the task or name a model with `--dwarf`. Use `--escalate-after N` to change the threshold, or `--escalate-after 0` to turn this off.
- `plan` now names tasks that are large enough that their first attempt usually fails review (at least 7,000 characters of prompt or 9 declared files; forge's history passes about a third of those), and tasks whose prompt has no `## Acceptance criteria` section. The note is advisory: the table and the exit code are unchanged.

### Changed

- `plan` now explores by default (`--explore 0.2`): now and then, a row left to forge gets the model one tier away in your own pools (cheaper for medium and high tasks, stronger for low ones), with the odds recorded. The new `forge dataset routing` command compares each pair of models from those draws, and also shows every model's first-pass rate and runtime per tier. Pools that differ only in effort (`--dwarf-low sol --dwarf-high sol:xhigh`) are compared too, so the table also shows whether a higher effort passes more. `--explore 0` turns exploration off, and that choice is kept for later re-plans.

### Removed

- **Breaking:** Jev no longer suggests efforts or rewrites tiers, because forge's run history showed it could not choose either: its effort suggestions carried no information beyond the task's tier. `forge jev calibrate`, `forge jev parse-spec` and `forge jev qa-effort` are gone, and so are the effort suggestions in Jev's routing lines and Fractal's re-rating of child tasks. `--jev-act` is still accepted but changes nothing. `forge jev doctor` lists the `routing_act` and `calibrate_floor` thresholds as safe to delete from an existing config. Drift checks, memory curation, verification and the other advisory gates are unchanged.

### Fixed

- The run dataset no longer drops an event when several writers in one process are the first ever to write to a new store: switching the new file to WAL is no longer raced by every connection, and losing that race is harmless.
- A comma list in `--qa` (or `--qa-high`, `--qa-medium`, `--qa-low`) now rotates through its reviewers task by task, as the docs promise and as the dwarf flags always did. Before, `plan` copied the whole list into every row, so every review failed before reaching a model. A list typed directly into a task's dwarf or qa column is now refused when you run `plan`.

### Security

- A decision-model API key or token that straddled the 200-character cut of an error message could leave its first characters in `jev.jsonl` and `jev.skip`; error text is now redacted before it is shortened.

### For contributors

- The test suite now runs every test process against a throwaway forge config home, so no test, including one still being written, can read your real `jev.json` (and its TypeSafe key) or add its temp dirs to your runs registry.

## [0.4] - 2026-10-15

Every forge run now records its inputs and outcomes in a dataset outside your repos, built for training and calibrating decision models on how forge really performs: it exports labeled decision cases, follows merged work into later history, and can add a second, independent review or an exploratory model pick to measure what a single run cannot.

### Highlights

- Every forge run now records its inputs and outcomes in a dataset outside your repos (`~/.local/share/forge/dataset/`). The record includes the resolved model of each dispatch, prompts, the diff of each attempt, verdicts and findings, tokens, merge and verification outcomes, and the full request behind each Jev judgment. It survives deleting run directories and exists for training decision models on how forge actually performs. `forge dataset status` shows what has been captured, `forge dataset import` brings in runs from before this change, and `FORGE_DATASET=off` turns capture off.

### Added

- `forge dataset export` turns the run dataset into labeled decision cases in the System One format that decision-model benchmarks and fine-tuning tools read. Each case is a decision Jev makes in forge, labeled by what happened next: whether a task's first attempt passed review, whether a retry passed given the findings it was handed, whether the reviewer passed a diff (the reviewer's model and effort are part of the case), which undeclared files a task really edited, whether a review describes the diff it was given, and whether a memory fact concerned a file the task went on to edit. Hosted Jev's own answers are kept alongside as baselines. The export stays private in `~/.local/share/forge/exports/`.
- `forge dataset outcomes` records what integrated work became, from your repo's history alone: for every task an `integrate` brought in, how many of the lines it added are still there 14 days later (`--horizon-days` to change it) and whether a later commit reverted it. It only reads git objects, never your working tree or index, writes nothing for a horizon not yet reached, and adds nothing when run again. The export turns it into the first label no reviewer decides: whether a merged diff survived. Verification results now also name the commit they verified.
- `plan --shadow-qa <spec>[,<spec>...]` sends a sampled share of reviewed attempts (`--shadow-rate`, default 10%) to a second, independent reviewer of another model family, chosen only from the specs you list. Its verdict is recorded in the run dataset and never read by the run: it cannot change a status, a merge, memory or a retry, and a shadow that runs long is stopped soon after the main review finishes. `forge dataset status` reports how often the two reviewers agree, overall and per model pair, which tells you how much of a review verdict is the diff and how much is who reviewed it. The approval table shows the extra reviews before anything runs.
- `plan --explore E` (0 to 0.5, or `FORGE_ROUTE_EXPLORE`) now and then gives a task you left to forge another of the models you passed to that plan, instead of its pool's turn, and records each decision with its probability in the run dataset. Until now every outcome was the outcome of the model the router chose, so nothing could say whether another model would have done better; these recorded odds make that estimable later. Forge still never uses a model you did not name, explored rows are marked `*` in the approval table so the spend is visible before you approve, pinned rows are never touched, and without the flag a plan is exactly as before. A model change made by `retry --dwarf` is now recorded too, at the moment it happens, and Jev's routing calibration leaves explored tasks out.

### Changed

- The run dataset now records which forge produced every event: besides the release, a fingerprint of the exact scripts, references and registry a run used (`code_sha`) and the checkout's commit (`forge_rev`), taken when the execution starts and flagged if the code changed before it ended. Imported runs no longer carry the importer's version. The store upgrades itself on first use. `forge dataset export` uses this to label outcomes more honestly: an attempt that says nothing about the implementer (a reviewer timeout, a harness crash, no verdict) is left out and counted instead of letting a later success count as the first attempt's, reviews gain a severity label (`none` to `P0`) when every finding is graded, routing and retry states gain an explicit task-size block, and each case's metadata says which forge decided and ran it and how the task went (attempts, raw verdict, gate pass, human accept, why it stopped). The orchestrator now records the decompose level it split at even when the user named none.

### For contributors

- The fake agent CLI in the test suite numbers its calls under a lock, so two tasks sharing a scenario key no longer replay the same call twice. That race made `test_accept_that_cannot_merge_exits_6_and_records_nothing` fail intermittently under load.

## [0.3] - 2026-10-09

Forge's GPT aliases now run the GPT-6 generation, with a new astra alias for GPT-6 Astra, and Fractal can decide on its own when to split work further, with a redesigned run inspector to follow it.

### Highlights

- `sol` and `luna` now run GPT-6.1 Sol and GPT-6 Luna on codex and openclaude, and a new `astra` alias runs GPT-6 Astra, the frontier model. `terra` stays on GPT-5.6 Terra, which has no GPT-6 successor. Claude models run through opencode move to Opus 5.5, Sonnet 5.5, Fable 5.1 and Haiku 5.5.

### Added

- Fractal can now decide at every eligible node whether to split the work or implement it directly: `--fractal-auto-decompose` turns it on (opt-in; without it Fractal behaves as before) and `--fractal-planner <spec>` picks a separate model for those decisions. The planning decisions show up in reports.

### Changed

- The Fractal run dashboard and offline HTML reports are redesigned as a run inspector: it opens on progress and problems, lets you drill into task QA and history or a node's decisions, logs and changes, and refreshes in place every couple of seconds without losing your search, selection or scroll position.

## [0.2] - 2026-10-06

A failed review no longer stalls a run: tasks retry inside it, quota and auth stops pause without spending an attempt, and an optional whole-run review checks the combined result before it merges.

### Highlights

- Quota, auth, network and empty-output failures no longer cost an attempt or fail a task. The run pauses with the new status `INFRA` and exit code 8 (details in `tasks/<id>/infra.txt`), and `--infra-retries M` waits the limit out and carries on. A task whose review was cut off re-runs only the reviewer, not the implementer.
- A task whose reviewer says FAIL can now be retried inside the same run: `run --retry-failed N` re-queues it with the reviewer's findings while the other tasks keep going, instead of waiting for the whole run to end. It is off unless you pass it.
- An optional whole-run review looks at the combined result of all tasks, where per-task reviewers cannot see cross-task problems: `review <plan>` is read-only, and `integrate --final-review <spec>` blocks the merge on a FAIL. It spends reviewer quota only when you ask for it.

### Added

- `--qa-threshold Pn` accepts a reviewer FAIL made only of labelled findings milder than Pn, merges the work and records the findings in `known_issues.md`; unlabelled findings always block. `accept <plan> <task> --reason "..." --approved` makes the same decision for a single task by hand.
- Forge now warns before spending when the project defines test suites that `--verify` does not run, and re-runs a failing verify command once if the source did not change. A pass on the rerun is reported as FLAKY. `--verify-retries 0` turns the rerun off.
- Forge now records when an implementer commits on its own, switches branch, leaves background processes running (they are cleaned up) or promises later work. The notes go to `guard.txt`, the reviewer prompt and the run summary; they are warnings and never change a task's status.
- `split` moves tasks, with their attempts, findings and reviewed work, out of a plan into a new one, and `combine` joins plans whose tasks are all merged. Both refuse unsafe cases without changing anything and roll back on interrupt or conflict.

### Changed

- Default timeouts now scale with the model's effort (the reviewer and planner default of 3600s is up from 2700s), `registry.tsv` takes an optional per-model timeout column, and `--timeout` now also applies to decomposed runs.

### Fixed

- A reviewer verdict followed by a carriage return, or a memory note placed after the verdict line, no longer turns a PASS into UNKNOWN.

### For contributors

- `forge jev test-subset` now reports a usage error for a missing `{files}` placeholder or repo directory before asking for an API key.
