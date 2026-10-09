# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow the format `{major}.{minor}` ([...] parts appear only when non-zero).

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
