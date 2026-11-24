# Offline validation

Tests make no paid provider requests: model CLIs are fakes. On a host with real Ripwire
installed, set `FORGE_RIPWIRE=off` for deterministic ordinary-dispatch tests; dedicated
Ripwire tests supply their own fake binaries.

Run `bash tests/check.sh`. Python's standard-library unittest suite uses temporary Git
repositories and fake model CLIs. `tests/run_tests.py` runs it and forge-ui's suite with every
test class in a process of its own, several at a time (the CPU count, at most 8;
`FORGE_TEST_JOBS=1` runs them one after another, `-j N` sets it). It prints the slowest tests,
and `python3 tests/run_tests.py PATTERN` runs only the classes whose id contains the pattern.
A single module still runs as usual: `cd tests && python3 -m unittest test_interrupt`. PATH is restricted to those fakes and an explicit utility
allowlist, so installed model clients cannot be used accidentally. No provider account is needed.
`.github/workflows/test.yml` runs `tests/check.sh` on Ubuntu under Python 3.9 for every pull request. Every push to `dev`, and a run started by hand from the Actions tab, also covers Ubuntu with the latest 3.x and macOS (with the system Bash 3.2) under both, so a macOS-only break shows up after the merge. A pull request that only changes documentation (the `DOCS_ONLY` list in the workflow) runs no tests, only the lint job (ruff and `check_links.py`). Each OS and Python pair splits the suite into four jobs with `tests/check.sh --shard I/4` (arguments pass through to `run_tests.py`), and the `Tests passed` job sums them up: it is the check to require. Every pull request and every push to `dev` also run one Windows job (`windows-latest`, Git Bash, the latest Python 3.x, no shards). forge does not run natively on Windows yet, so `run_tests.py` leaves out there the modules and classes listed in `tests/windows-pending.txt`, each under the reason it fails; `--run-pending` runs them anyway. Delete a line once its tests pass on Windows.

`skill-cases.json` contains invocation and approval scenarios with expected observable behavior.
Use these for a manual or independent skill evaluation: provide each request and SKILL.md,
allow only fake dispatch, and compare actions with the rubric. Mechanical checks
(`test_skill.py`) validate scenario structure, SKILL.md's size budget and frontmatter, each
reference's size, and every relative link and `#anchor` in SKILL.md, the README, `references/`
and `docs/` (`check_links.py`, which CI also runs on documentation-only pull requests). They do
not prove a model follows the instructions. When SKILL.md changes a rule, update the case that
covers it.
Live model routing, authentication and provider behavior are separate, explicitly requested checks.

The suite covers large Unicode prompts via regular files, stdin and FIFOs; exact context
preservation; distinct memory; summary/full output; native final-response and usage
extraction; attempt history; help cache invalidation; readiness, overlaps, interruptions and
restart behavior; and independent review object equivalence.

The fake agent CLI in `test_forge.py` can be steered per call with a `SCENARIO` JSON file keyed
`"<task>.<role>.<call number>"` (`*` matches any task or call; task = the prompt's `TASK_<id>`
marker, `change` when absent). Entry keys: `rc`, `stderr`, `stdout`, `empty`, `message`, `content`,
`verdict` (`none` omits the verdict line), `findings`, `is_error`, `no_edit`, `self_commit`,
`background`, `sleep`, `files` (extra `{path: content}` the dwarf writes). `ForgeTests.scenario({...})` writes it and `calls()` returns the
`dwarf`/`qa` call log; new test modules borrow the fixtures (`setUp = fixtures.ForgeTests.setUp`)
rather than subclassing, which would re-run every `ForgeTests` test. Modules by area:
`test_schedule` (scheduler retries and infrastructure pauses, no model or git), `test_retry`
(retry, QA-only resume, verify reruns), `test_infra` (exit-8 classification end to end and the
implementer guards), `test_policy` (severity gate, shared prompt contract, verify coverage),
`test_qa` and `test_verify_coverage` (the pure modules), `test_fail_first` (`--fail-first` evidence), `test_dispatch` and `test_guards`
(dispatch timeouts, `FORGE_REGISTRY`, process hygiene), `test_plan_ops` and `test_review`
(accept/combine/split and the opt-in final review). Scripts must not use external commands
outside the allowlist above (no `cut`, `seq`, `ps`, `find`, `perl`, `setsid`).

The forge-ui suite lives in `ui/tests` and runs from `ui/`:
`(cd ui && python3 -m unittest discover -s tests)`. `tests/test_ui.py` covers the launcher
(`forge ui`) and the runners' `--ui` flag. `tests/check.sh` runs both suites and, when `node`
is on PATH, `node --check` on every `ui/forge_ui/static/*.js`; without `node` that syntax
check is silently skipped.

`live_compare.py` is deliberately outside unittest discovery. It requires `--execute`, creates
isolated fixtures and revision copies, and never changes the source checkout. The approved
comparison uses baseline `f436210`, implementer `sol:medium:codex`, reviewer
`terra:xhigh:codex`, no bypass flags, and a 180-second dispatch timeout on both revisions.
A locked append-only ledger counts every model invocation, including failures, against a
hard cap of 24. Allocation: two paired solo fixtures (8), one paired three-task pipeline
(12), and paired clean/buggy QA snapshots (4). No implicit retries. Baseline JSON event
instrumentation matches candidate output without changing model settings. Acceptance scripts
are checked for mutation. Results and complete artifacts are retained in the requested directory.

```sh
python3 tests/live_compare.py /absolute/path/to/fresh-artifacts --execute
```

Only run this command with explicit authorization to spend model quota. Observed token and
latency differences in this small sample do not establish universal savings or quality parity.

Ripwire tests use fake binaries and mocked downloads, including a byte-pinned copy
of the upstream v0.4.0 installer in `fixtures/`. They exercise all harnesses, FIFO/stdin
prompts, opt-out persistence, QA baselines, bounded failures and terminal consent.
The consent tests need access to `/dev/tty`; run outside a sandbox that blocks it.
The real v0.4.0 smoke check is separate from the offline suite and uses a temporary
installation with `RIPWIRE_NO_ACTIVATE=1`, never a provider call.
