# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow the format `{major}.{minor}` ([...] parts appear only when non-zero).

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
