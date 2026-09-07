# Offline validation

Run `bash tests/check.sh`. Python's standard-library unittest suite uses temporary Git
repositories and fake model CLIs. PATH is restricted to those fakes and an explicit utility
allowlist, so installed model clients cannot be used accidentally. No provider account is needed.
CI runs the same checks on macOS (system Bash 3.2) and Linux.

`skill-cases.json` contains invocation and approval scenarios with expected observable behavior.
Use these for a manual or independent skill evaluation: provide each request and SKILL.md,
allow only fake dispatch, and compare actions with the rubric. Mechanical checks validate
reference links and scenario structure; they do not prove a model follows the instructions.
Live model routing, authentication and provider behavior are separate, explicitly requested checks.

The suite covers large Unicode prompts via regular files, stdin and FIFOs; exact context
preservation; distinct memory; summary/full output; native final-response and usage
extraction; attempt history; help cache invalidation; readiness, overlaps, interruptions and
restart behavior; and independent review object equivalence. CI runs these on both platforms.

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
