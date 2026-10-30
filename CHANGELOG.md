# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow the format `{major}.{minor}[.{patch}]` ([...] parts appear only when non-zero).

## [0.13] - 2026-10-30

Decision-model gates are now yours to set, off, shadow or on, per user or per repository with `forge decide`, and when on they act: drift and forward edit the plan, prompt and coverage send it back to the planner, and five new gates bounce unaddressed retries, read the implementer's final message, find the merge that broke verification and triage leftover findings. QA verdicts now follow a P2 blocking threshold in both directions, and a retry can continue the implementer's own session.

### Added

- A gate can ask any TypeSafe-compatible (jev) or Cloudflare-compatible (clef) endpoint, hosted or on your machine: `forge decide provider add` takes a URL, a model, a context limit and a price, and a key only on stdin or as the name of an environment variable.
- Decision models can be capped per run and per day, in tokens and estimated cost (`forge decide budget`, or `--decide-budget tokens=N,usd=X` on a run); a gate past a cap stops asking for the rest of the run or day.
- `forge decide` sets each decision-model gate (drift, forward, prompt, memory-curate, memory-slice, memory-stale, verify-discover) to off, shadow or on, for you or per repository (`.forge/decide.json`): `status`, `gate`, `provider`, `budget`, `privacy`, `doctor` and `migrate`. Shadow asks and records but changes nothing. Its first change migrates `jev.json`; until then every run behaves exactly as before. `forge decide privacy never-send --repo DIR` stops every gate from sending anything about that repository.
- forge-ui's Settings page sets each gate's mode, the providers, the budgets and a repository's privacy floor, through `forge decide`.
- `--gate name=mode` and `--no-gates` on `plan` and solo runs choose the gates for that run; they are kept with the run, so resume and retry decide the same way.
- A decision-model question asked again in the same run is answered from the run directory's request cache: free, outside the budget, and logged as `cached`. `FORGE_DECIDE_CACHE=off` turns it off.
- A gate goes `on` only for a provider and model with a passing replay: `forge decide gate G on`, and `forge config set` of an `on` gate's provider or model, refuse (exit 3) and name the replay to run. `--force` turns it on anyway, and a forced gate is marked in `forge decide status` and `report`, `forge config show`, the plan's decide line and forge-ui's Settings, which shows the whole refusal. `forge decide migrate` marks the gates it turns on as forced.
- `forge decide replay --site S --provider P --model M` re-asks a site's labelled past requests on that provider and model, compares the answers with the live model's and the site's no-model baseline, and records whether it passes. Without `--send` it is a dry run that sends nothing; a replay skips repos under the privacy floor and is charged to no budget. The decision-models guide gains a "Measuring a gate" section.
- `forge decide report` shows, per decision-model site over recent runs, what it cost (calls, cache hits, errors, skips, tokens, and dollars at the provider's price) and how often it fired right, wrong or missed, judged against labels taken from what the runs did next. It asks no model.
- forge-ui marks a PASS that forge turned into FAIL, and shows the QA threshold with where it comes from, such as `P2 (default)`.
- Task and integration worktrees now contain the repository's submodules, each at its recorded commit, copied from your checkout's own copy without network access and invisible to git. A submodule forge cannot fill is named in the implementer's brief.
- `plan --pre-review-tests` runs only the tests an attempt affects (from `.forge/test-map`, naming conventions or references) before paying for its review; a red result goes back to the implementer as findings, unreviewed. `--pre-review-parallel N` caps how many run at once, `forge control set` changes both during a run, and forge-ui shows the result.
- `plan --implementer-session` lets a retry continue the implementer's own claude or openclaude session, so it does not re-read what it already knows, when its spec is unchanged and its work is still what the review judged; otherwise the retry starts fresh as before. Each attempt is logged in `tasks/<id>/implementer_sessions.tsv`, and `forge dataset status` counts implementer continues apart from reviewer ones.
- `forge-parallel.sh attribute <plan>` (or `run --attribute`) finds the merged task that broke the combined verification, by re-running the same verification at merge boundaries. With the new `attribution` gate on, the likeliest culprit is checked first.
- New `dwarf-message` gate: an implementer whose final message says it did not finish, or was blocked, is sent back without a review (`INCOMPLETE`), keeping the findings it was working on. One that asks a question only you can answer stops the task as `NEEDS_INPUT`; the run summary shows the question, and `forge-parallel.sh retry <plan> <task> --answer "…"` (or `--answer-file`) resumes it.
- New `retry-addressed` gate: a retry whose change leaves one of its blocking findings untouched goes back to the implementer without paying for a review. The attempt is recorded as `BOUNCED`, and the next one is always reviewed.
- When `on`, the prompt gate and the new coverage gate send work back to the planner once: a task whose prompt reads as too vague to check, or a requirement in `request.md` that no task covers. `plan` then exits 4 and writes `plan.sendback.md`; fix the plan and run `plan` again. A block under the task table shows what each plan-time gate did.
- `forge decide status`, `report` and `replay` cover the five new gates (coverage, retry-addressed, dwarf-message, attribution, findings), which start `off` and need `--force` to go `on` until a replay passes. Every label now means "the gate should have acted"; the memory gates report how often they acted. `references/decision-models.md` describes what each gate does when on.
- forge-ui shows a task waiting for your answer with its question, `BOUNCED` and `INCOMPLETE` attempts, and the result of `attribute`.
- New `findings` gate for `forge-issues.py`: a leftover finding that rewords an open issue becomes a comment on that issue instead of a new one, and the `--file-issues ask` dialog pre-selects the findings that name a concrete defect, with a reason for each one left out. `forge-jev.py findings` offers the same check to other tools.

### Changed

- `forge issues` and the finding sweep read the findings a review tolerated the same way the gate does, at the run's actual threshold, so each one is filed.
- QA verdicts now follow a blocking threshold in both directions, `P2` unless `--qa-threshold` sets another, in solo runs, decomposed tasks and the whole-run review alike. A PASS that lists a CONFIRMED finding at the threshold or worse becomes FAIL, and its retry says why. A FAIL made only of CONFIRMED findings below it is accepted, and those findings are kept in `known_issues.md`. `--qa-threshold P3` (or `none`) makes every CONFIRMED finding block; `plan` now takes the flag too.
- Implementers and reviewers are told to verify within the change: the tests it affects, not the whole suite or repo-wide lint, format and type checks unless the task asks, with a reviewer spending about 5 minutes at most on test runs.
- QA reviews now run at `high` effort when the reviewer spec names none (it was `xhigh`), and the approval table says so; planning stays at `xhigh`. Name an effort (`--qa opus:xhigh`) to keep the old one.
- The drift gate asks about the 25 files a task could plausibly touch, ranked, instead of the first 120 in alphabetical order. When `on`, drift adds the files it predicts to the task before the waves are computed, and the forward gate adds the dependency it predicts (never one that would form a cycle); both are recorded in `plan-gates.json`. In `shadow`, and under `--jev-shadow`, they only record what they would do.
- Verify discovery says when the command it picks may test only part of the repository (for example Python tests in a repo that also has Rust code), and reads remembered test commands from the project memory itself.

### Deprecated

- `forge jev` still works for one release and names the `forge decide` command that replaces it; `--jev-act` does nothing and says so; `forge config`'s `jev.enabled` is replaced by `decide.gate.<gate>`.

### Removed

- The decision-model sites that did not earn their place are gone: `forge jev backtest` (and the tracked `jev-backtests/` data), review triage, same-wave coupling (coupling keeps forward dependencies), the Clef first-pass and retry-pass predictions and their forge-ui drawing, verify flake triage (the deterministic rerun stays), the test-subset site (`.forge/verify-subset` stays for `--fail-first`) and score-plan's routing tiers. `references/decision-models.md` replaces the Jev reference.

### Fixed

- A dispatch whose harness exits very quickly now still records its log clock, so forge-ui can time its commands.
- A reviewer that runs the tests no longer gets a passing attempt thrown away: files it leaves untracked in its review copy, such as `__pycache__/` in a repository with no `.gitignore`, no longer void the review. A task `INVALIDATED` because its review copy changed is now retried by `--retry-failed`, and that retry, like an explicit `retry`, only reviews the unchanged work again; forge-ui offers Retry now for it.
- `forge issues` and the finding sweep read a location that lists several lines or adds a note (`path:442,674`, `path:117-119 (also …)`) as its file and first line, instead of dropping the finding as having no location.
- A dispatch no longer hangs forever when its run directory is removed, or cannot be written, while the agent is running: it used to wait for its log-clock sampler, which waited for it. Test runs left one such pair of processes behind each time.

### For contributors

- The dataset records each review's gate decision, PASS verdicts that forge turned into FAIL, the solo gate, and where the threshold came from.

## [0.12.1] - 2026-10-28

A run can now fix the review findings it tolerated before it ends (`run --sweep Pn`), and forge-ui shows where each task stands, the checks each attempt ran and the environment problems several tasks hit. A task that deletes large files no longer breaks its review.

### Highlights

- forge-ui shows where each task of a decomposed run is: the task table's Now column and a task's Now section name the command a running dwarf or reviewer is running and for how long, flag it once its log has been quiet for five minutes, and say why a task that is not running waits (its dependencies, a task it shares files with, no free slot, a drain or stop, an infrastructure pause with its countdown, or the retry policy's stop with the split it suggests). The run's inspector says what the scheduler is doing.
- `run --sweep Pn` fixes the review findings a run tolerated while its context is fresh. Once every task has merged, and before the combined verification, the open findings at Pn or worse become a few extra fix tasks, one per file (at most 8 findings each, `--sweep-max N` tasks, default 5; `--sweep-plausible` adds PLAUSIBLE ones). Each fix task runs on the plan's low tier, with a narrow review that says, finding by finding, whether it was fixed. The sweep plan, with every finding it left out and why, is printed and kept in `sweep/plan.md`. `forge issues` no longer offers a finding the sweep fixed, and a fix task that fails does not fail the run.

### Added

- Each attempt in forge-ui lists the tests, type checks, lints and builds its dwarf and reviewer ran, read from their logs, with each one's exit code, time and pass/fail counts and a link to that command in the conversation, and says which commands of the brief's acceptance criteria were never run. forge now keeps a clock beside each agent's log (<role>.clock; FORGE_LOG_CLOCK=off turns it off) so codex and grok commands are timed too.
- A banner in a run's header names an environment problem that more than one task ran into (Docker down or out of address pools, a missing container, a port in use, the sandbox's EPERM, a refused local service, no network, a full disk, a missing command), with the tasks it hit and the commands that failed on it.
- forge-ui shows a run's finding sweep: a fix task lists the findings it took with what its review said of each, the task a finding came from shows where it stands, the run's Finish says what the sweep fixed and left, and Open findings marks findings the sweep fixed as swept instead of offering to file them.
- `run --sweep-ahead` tells a task the open review findings in files it owns, to fix only when its change already goes through that code; its reviewer is told the same, and no dispatch is added.

### Fixed

- A codex command run through zsh -lc "…" now shows without its shell wrapper in a conversation, and a message containing a Unicode line separator no longer breaks the reading of its log.
- `FORGE_FORECAST_BUDGET_S=inf` (or any budget too large to wait for, such as `1e10`) now means no time limit for the plan forecast that `run` prints, instead of silently dropping the forecast.
- A task that deletes large files no longer breaks its review: deleted files now appear in the reviewer's diff by name only, without their contents. Previously, deleting a big data directory could make the prompt too long for the reviewer's CLI, which refused it and left the task in ERROR.

## [0.12] - 2026-10-27

Decomposed runs can now be steered while they run and stop cleanly when interrupted: drain, stop, retry or resize a live run from forge control or forge-ui, an infrastructure stop pauses only the model or CLI it hit, and verification can run as parallel lanes that rerun only what failed and name the failing tests.

### Highlights

- A running decomposed plan can be steered: `forge control drain` lets running tasks finish and starts nothing new until `forge control resume`; `forge control stop` ends the run once running tasks are done (exit 130 without verifying, unless every task merged; not-started tasks stay pending for the next `run`), and `stop --now` interrupts them; `forge control set <plan> max-parallel N` changes how many tasks run at once.
- Verification can run as lanes: declare them in `.forge/verify.d/` (one file per lane) or with `run --verify-lane 'name[options]=command'`, and forge runs them side by side (`--verify-parallel`, 3 by default), reruns only a lane that failed, and calls a lane that passed on rerun flaky. Lanes can read JUnit or TAP reports to name the failing tests, and a baseline of known failures keeps those from failing the run. The run summary and the whole-run review name each lane and its state.
- An infrastructure stop no longer pauses the whole decomposed run: a rate limit pauses only that model and a quota or sign-in stop only that CLI, each with its own wait and --infra-retries budget, while tasks routed elsewhere keep running. `forge control show` lists the active pauses.

### Added

- `run --infra-backoff` sets how long forge waits between infrastructure retries: `exp:<base>[,<cap>]` (the default, `exp:60`), `linear:<start>,<step>[,<cap>]` or a list of waits such as `10,20,30`. `forge control set <plan> infra-backoff` changes it while the run goes on, and each wait message names the shape it used.
- A running decomposed plan can retry a stopped or failed task, optionally on another model (`forge control retry <plan> <task> [--dwarf <spec>]`), and pick up a row you added to tasks.tsv (`forge control add <plan> <task>`) without ending the run. The scheduler applies or refuses each request at its next pass and records the outcome in control.log.
- `run --infra-fallback`: when an implementer's model is at capacity, the task moves at once to the next model of its own tier pool instead of waiting, and the move is recorded like an escalation. The busy model is still paused and counted, so a pool that is entirely at capacity still ends the run.
- forge-ui now shows a verification while it runs: its current step, last output line and idle time, and, for a lanes verification, each lane's state, runs, test counts and failing tests, with known failures marked.
- forge-ui serves every verification log, each lane's and each rerun's, as a numbered tail or in 400-line pages, so the cause of a failed verification can be read without opening the run dir.
- The verification panel in forge-ui now follows a running verification live (current step, last line, elapsed time, a warning after five silent minutes, the log's tail) and, when it fails, opens on the failing tests, marked new or known, each linking to its line in the log.
- forge-ui's Execution view shows verification lanes as their own rows, with every run and rerun at its real time, and the 3D finish node shows one light per lane; a verification that is still running reads RUNNING instead of its previous result.
- `--escalate-effort` (on `run` and `retry`, or `forge control set <plan> escalate-effort on`): when a task has failed its escalation limit on the top tier's model, forge retries it once more at the next effort that model accepts before stopping it with a split suggestion. Turning it on also releases tasks the policy already stopped.
- forge-ui's Controls panel can now drain, resume and stop a live run (gracefully or at once), change max parallel, the infrastructure backoff and the effort step, and retry a stopped task now with a chosen model. It also shows each infrastructure pause with a countdown, and whether a queued retry was applied or refused.

### Changed

- The forecast `run` prints before dispatching lists the tasks that don't have enough history yet on one line instead of a row each, and is a single line when no task has enough history, so a new setup no longer opens every run with a table of "not enough history" rows. `forge dataset forecast <plan>` still shows every task.

### Fixed

- When the plan forecast that `run` prints runs out of its time budget while saving, it no longer leaves a stray `.forecast.json.<pid>` file in the plan.
- Interrupting a decomposed run no longer leaves an implementer or reviewer CLI running: forge waits up to 10 s (FORGE_INTERRUPT_GRACE) for each task to stop its harness, and a harness left behind by a killed run is stopped when the next run starts.
- Interrupting `forge-parallel.sh run` (Ctrl-C or SIGTERM) no longer goes on to verify a half-merged integration branch: the run stops, frees the plan lock within seconds, exits 130 and prints the command that continues each interrupted task. A signal during verification stops the whole verify command and skips its rerun.
- A task that finished just as a decomposed run was interrupted is now reaped first: its passing work still merges and its result is kept, and only the tasks still running are marked INTERRUPTED.
- On macOS, a Ripwire context call whose output was too long or that ran too long no longer fails with a PermissionError; it is skipped with its reason, as intended.

## [0.11.1] - 2026-10-26

A bug-fix release for forge-ui and forge issues: the Settings and Open findings views keep your edits and selections honest, forge ui open handles a read-only server with another configuration, and refused labels and warnings say what's wrong.

### Fixed

- forge-ui's Open findings view files exactly what its preview showed, even if you change the scope or the plausible toggle while the drafts load, and shows forge's notes when nothing was filed. Findings hidden by a filter are no longer counted or acted on, and "Select shown open" counts only shown findings toward its limit of 25.
- forge-ui's Settings page no longer drops a repository-switch confirm when the run list changes. It warns before saving over a file that changed on disk while a confirm was open, and a failed read keeps your unsaved edits on screen with a note instead of replacing the page with an error.
- `forge ui open` resolves a `FORGE_REGISTRY` that starts with `~` the same way forge's commands do, and replaces a read-only forge-ui running with another configuration instead of refusing to open.
- The docs now match the code on who creates `control.lock`, `forge config set` and `unset` needing `--repo`, when `forge runs rm` keeps a run directory, and a Fractal plan's hidden pin control.
- `forge issues` prints its warnings as `warning: …` in the listing too, as it already did for dismissals.
- When forge-ui refuses an issue label, its message now names the rule the label broke (blank, over 50 characters, starting with `-`, or holding control characters).

## [0.11] - 2026-10-26

Reviews now remember the last round: a retry's review starts from the previous findings, and a repeated whole-run review from the last report. forge dataset can forecast a plan before it runs and grade how often each reviewer's PASS held, and forge-ui shows a superseded final review as OUTDATED instead of FAIL.

### Highlights

- `forge dataset reviewers`: a report card for reviewer models. For each reviewer, how often work it PASSed was later reverted or mostly rewritten within the outcome horizon (14 days by default), with a 95% interval, or "not enough data" below 10 outcomes. forge-ui's Models view shows it as the Reviewers table's Did not hold column. Advisory only: it never changes routing.
- `forge dataset forecast <plan>` estimates a plan from past tasks at the same tier and model: median and p90 tokens and time, and the first-try PASS rate with a 95% interval, per task and in total, flagging tasks unlikely to pass first time. `forge-parallel.sh run` prints it before dispatching and records it for later checks (`--no-forecast` turns it off). It is advisory only.
- On a retry, the per-task reviewer now first checks the previous round's findings, sees the diff between the two attempts and reads the implementer's reply, instead of starting cold. This applies on every harness, so a finding that was fixed is confirmed as fixed and one that was not is raised again.
- A final review that later merges superseded now shows as OUTDATED in the forge-ui run view and on the cloud map, not as the run's verdict, and tasks merged with `accept` show as accepted instead of failed. `forge issues` and the Open-findings view mark the findings of an outdated review, and accepted findings get their own origin and issue text.

### Added

- `plan --reviewer-session` is an opt-in experiment that continues the reviewer's own session across rounds on claude, openclaude and codex, and falls back to a fresh session whenever it cannot. Those reviews are tagged in the dataset and kept out of the reviewer statistics, so the experiment does not skew the independence numbers.

### Changed

- A mistyped command now says which one you most likely meant, for example `forge rnus` answers "Did you mean `forge runs`?" above the usage.
- A repeated whole-run review now carries the previous final review's report and the diff since it, so it checks what the earlier review found rather than reviewing the run from scratch.

### Fixed

- forge-ui started directly (`python3 -m forge_ui`) now finds forge's run registry the way forge writes it: an `XDG_CONFIG_HOME` that starts with `~` lists your runs instead of none.
- A grok dwarf or reviewer that runs its tests in the background no longer ends as an error after 10 minutes: grok now waits for that work as long as forge's own time limit allows, and `forge-dispatch.sh doctor` checks that the installed grok supports this.
- A relative `XDG_CONFIG_HOME` now counts as unset, as the XDG spec says, so forge registers runs and keeps `jev.json` in `~/.config` instead of recording nothing. Before, `forge ui open` printed a link to a run that forge-ui then did not list.
- The dataset now imports the traces from the archive of a run whose directory name starts with `review-`; before, nothing from it was imported.
- An escalation pin, or any escalation, no longer writes through a symlinked `escalations.tsv`.
- The run summary, and `escalation.next` in the router's `--check`, no longer say a pin waits for a QA-only re-run when the next retry will really be a full attempt.
- A relative `XDG_DATA_HOME` now counts as unset, as the XDG spec says, so the run dataset, run archives and exports stay in `~/.local/share/forge` instead of a folder inside whichever repo forge ran from. `forge config show` reports the same place.

## [0.10] - 2026-10-23

forge-ui can now change a run: live QA threshold, retries and timeout, a one-shot model pin, open findings filed as GitHub issues, deleting runs while the dataset keeps them, and every config source in one view. Each control is also a forge CLI: forge control, forge runs rm, forge config and new forge issues modes.

### Highlights

- After a run, review findings nobody would act on can become GitHub issues: forge offers once to file the findings `--qa-threshold` tolerated, the PLAUSIBLE ones and whatever a whole-run review left open. Where the gh-proman skill is set up for the repository they go on the project board through it; anywhere else forge files them itself with plain `gh` (`forge issues <plan-dir>`). A finding is never filed twice. `--file-issues auto` files them without asking, `--file-issues off` never offers.
- `forge control show|set|pin|unpin <plan-dir>` changes a plan while it runs: the QA threshold (read once per attempt, per task too, and `none` exempts a task), the timeout, `--escalate-after`, verify retries, and, while a scheduler is live, the retry and infrastructure-retry budgets. The budgets keep their per-invocation rule: a live scheduler publishes them for the length of the run and re-reads them at every decision. Every change is validated, logged in `control.log` and recorded in the dataset as a `control` event.
- forge-ui has an Open findings view: the findings a run, or every run of its repository, left open, with their filed state. Select some to file as GitHub issues or dismiss ones that need no work. `forge issues` gains `--offline`, `--only`, `--all-runs`, `--dismiss`/`--undismiss` (with `--reason`), `--include-dismissed` and a `findings` list in its JSON; the whole-run review's findings are now collected, and filing and dismissal take a repository lock (exit 3 when busy).
- `forge runs rm <run-dir>` deletes a run while the dataset keeps what it taught: a strict import, a self-describing archive (`FORGE_ARCHIVE.json`), and `run_delete_started` and `run_deleted` events, all under the run's own lock. `--worktrees` and `--branches` also remove the run's clean worktrees and its unchecked-out branches (foreign and dirty worktrees and UNMERGED branches are reported); `--dry-run` previews. forge-ui has a matching delete dialog. The dataset also imports `escalations.tsv` and `control.log`.
- forge-ui is no longer read-only: it gains write endpoints and Controls panels for a run and for each task (QA threshold, timeout, escalate-after, verify retries, budgets, threshold override, retry budget, model pin). It only ever calls forge's own CLIs, and every change must carry a per-launch token and pass the Host and Origin checks. `--read-only` (or `FORGE_UI_READONLY=1`) turns writing off, and `forge ui open` never reuses a server running in the other mode.

### Added

- `forge control pin <plan-dir> <task> <spec>` sets the model a task's next full retry uses, once: the spec is checked with `forge-dispatch.sh doctor` (no quota), applied at the retry and written to `tasks.tsv`, and `escalations.tsv` gains a `source` column (`policy`, `retry` or `pin`) saying who decided. `forge control unpin` drops it; Fractal plans never apply pins.
- `forge config show|set|unset` shows every config source in one view (the registry, Jev, a repository's `.forge/verify`, `setup` and `verify-subset`, the dataset and `FORGE_*` variables with secrets hidden) and edits the per-repo files and `jev.enabled`. forge-ui shows the same view as Settings.

### Changed

- A repeated `forge ui open` for a run that already has a browser tab no longer looks like it did nothing: it prints the link as before and says on stderr that no new tab was opened, so you know to use the link.

### Fixed

- `forge ui open DIR` now reads only the first line of the run's `repo` file, as the runners do, so a run whose `repo` file has extra lines is listed under its real repo instead of under a path that does not exist.
- A forge-ui export no longer leaks a local path spelled another way: the run, repository and home paths are also scrubbed in their real form (on macOS, `/private/var/...` and `/var/...`).

## [0.9] - 2026-10-21

forge-ui has a new look and layout, a 3D stage that marks every review finding on its task, and each dwarf's and reviewer's conversation, which runs on claude, openclaude and opencode now keep in full. Also new: `forge runs` and a first screen that says how to get runs; npm installations now hear about new releases.

### Highlights

- forge-ui shows what each dwarf and reviewer said and did. Every attempt in the inspector links to its conversation, which opens where a diff opens: the agent's messages, its thinking folded, each command it ran with its exit code and output, each file it changed with the patch and a link to that file in the diff its review read, its other tool calls, and the final answer with tokens and cost. Search it with `/`, keep only what it said, ran or changed, and step through its errors with `E`. While the agent works, the conversation follows its log as it grows. It is read from the CLI's own log: codex, grok, opencode and claude streams event by event; for a claude run that logged only its final answer, from Claude's session file for that run if it is still on this machine; any other log as plain text. An export includes conversations only when prompts and texts are included.
- forge-ui has a new look and better controls. It reads like a signal box's desk: engraved plates for headings, push buttons whose lamp lights when they are on, and lamps that keep one meaning everywhere (amber running, green merged, red failed, unlit pending), equally finished in light and dark. Choose between two layouts: *Immersive* (the default) floats the runs, the inspector and the tasks over a full-window 3D stage (on a first visit the inspector and the tasks start folded, so a run opens on its plate, and choosing a task opens the inspector), and *Workbench* docks them beside it with splitters you can drag. `1`, `2` and `3` fold each one away, the header switches layout and theme (system, light or dark), and the runs list sorts by latest activity, state or repo. Each model shows as one name plate everywhere, a finding's severity is a shape rather than a status colour, and the run's settings, request and verification output moved from the header into the inspector (*Run details*). The task table and the model leaderboard sort by any column, reorder columns by dragging a header (or Alt+← →), and show or hide columns from a *Columns* menu. forge-ui remembers all of it in your browser.

### Added

- `forge runs` lists the runs registered for the current repo (or `--all` repos), newest first, with each run's status, checks, goal and run directory; `--json` for scripts. Read-only.
- forge-ui's 3D stage shows more and is easier to steer. Every review finding now stands over its task's block as a mark shaped by its severity (P0 spike, P1 diamond, P2 cone, P3 pin); hover it for its location and summary, click it to open the diff that review read, scrolled to that finding. Choosing a task keeps its route lit, what it waits on and what waits on it (`I` switches this off), and hovering a task shows a card with its status, attempts, busy time, tokens, models and worst finding. Merges, failed attempts, escalations and final reviews play over the plate as they happen and as a replay passes them, and the replay now runs smoothly instead of in steps. The *View* menu switches each part of the drawing on or off (cables, flow, lamps, roof text, readouts, findings, finish line, districts, halos, route). New keys at the stage's edge fit what is shown (`F`), zoom (`+` `−`), switch between perspective, true isometric and top-down (`V`), reset the view (`0`), open a legend of what the stage draws now (`L`), go full screen, and list every key (`?`). A block whose roof is too small to read gets its name on a tag that faces you, and on the map ← → walk the runs, ↑ ↓ the repositories and Enter opens one.

### Changed

- forge-ui's first screen, with no runs yet, now says how to get one (start a run with `/forge "<goal>" --dwarf <model>`, or `forge ui open <run-dir>` for runs kept elsewhere) instead of an empty 3D plate under filters, and the run list no longer suggests `python3 -m forge_ui --root <dir>`, which only worked from inside forge's ui folder. The map takes over as soon as the first run appears.
- Runs on claude, openclaude and opencode now keep their whole conversation. forge starts claude and openclaude with `--output-format stream-json --verbose` and opencode with `--format json`, so `<role>.log` holds every message, tool call and result instead of only the final answer (claude) or plain text (openclaude, opencode). forge-ui shows it as the attempt's conversation. The final message, the token counts and the infrastructure checks are read from these streams: opencode now reports its tokens, its answer is the text of its last step, and an opencode `error` event that no answer followed (an expired token, a refused route) is classified like codex's. antigravity keeps its plain output until its stream has been seen on a working login.

### Fixed

- An npm installation now hears about new releases: the once-a-day notice after a run asks the npm registry (or `npm_config_registry`) and names npm's command, `npm install -g @giamme/forge@latest && forge install`. `forge version` shows that command too, instead of telling npm users to point `forge update --source` at a Git checkout they don't have.
- With `XDG_STATE_HOME` set but empty, Fractal keeps its state (its runtime and its runs) in `~/.local/state/forge/fractal`, where forge-ui looks, instead of in a `forge/fractal` folder under whatever directory forge was started from. `forge dataset import --fractal` reads the same place.

## [0.8] - 2026-10-21

forge installs from npm: `npm install -g @giamme/forge`, then `forge install`. The forge command also gains `forge update`, `forge version` and a real `--help`, reviews can ask for opt-in fail-first evidence, and forge is now under the MIT License.

### Highlights

- forge installs from npm: `npm install -g @giamme/forge`, then `forge install` links it into your agent harnesses, and `npm install -g @giamme/forge@latest && forge install` updates it. The `forge` command works through the link npm puts on your PATH, `forge install` runs the installer from a checkout too, and `forge update` on an npm installation names the npm command instead of asking for a Git checkout. The once-a-day update notice needs a Git checkout, so npm installations don't get it yet.

### Added

- forge is now released under the MIT License: anyone may use, change and share it as long as the copyright notice stays with it. Until now the repository had no license, which left nobody the right to use it.
- `forge update` and `forge version` now work from the forge command. `forge version` shows your version, checkout and upstream (`--check` asks for the newest release), and a finished solo run, `plan` or `run` prints one line when a newer forge is out, at most once a day; it stays quiet offline, in CI or with `FORGE_UPDATE_CHECK=off`.
- New opt-in `--fail-first` (solo runs and `plan`): forge also runs the tests a change adds or edits against the code without its change, using `.forge/verify-subset`, and asks the reviewer to report any test that still passes as a P2 finding. It is advice only and never changes a result by itself; forge-ui shows the evidence under the review findings, and each check is recorded in the run dataset.

### Changed

- `./forge --help` (or `-h`, or `forge help`) now lists the commands with a line on each and exits successfully, instead of failing with four bare usage lines. An unknown command is named in the error, and `forge fractal --help` and `forge jev --help` call themselves by the name you typed rather than their script file.

### Fixed

- With `XDG_CONFIG_HOME` set but empty, `forge ui open` wrote its run list into a `forge/` folder in the current directory (usually your repo), where forge-ui could not find it; runs were left off forge-ui's run map, and `forge jev` read `jev.json` from the current directory. An empty value now counts as unset, as the XDG spec says, so all of it lives in `~/.config/forge`.
- forge-ui now shows a review's verdict exactly as forge records it: only a FORGE_VERDICT line that ends the reply counts. A reply forge treated as having no verdict (and blocked on) no longer shows as passed or failed in forge-ui.
- `forge-install.sh --help` prints its usage instead of installing, and an option it does not know, such as a mistyped `--dryrun`, is refused rather than ignored, which used to run a real installation.

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
