# Solo runs

Create a run directory outside the repository, write the full implementation requirements to
`prompt.md`, and optionally add `goal.txt` and an approach file. State the selected models,
efforts, harnesses and any explicit bypass flags before dispatch.

```bash
bash <skill_dir>/scripts/forge-solo.sh "$FORGE_RUN" --repo "$REPO" \
  --dwarf <spec> [--qa <spec>] [--approach <file>] \
  [--yolo-dwarf] [--yolo-qa] [--timeout <seconds>] [--no-memory] [--dry-run]
```

Both roles receive offline preflight before implementation starts. The runner records a
starting tree with a private index, then captures the net filesystem changes since that
snapshot, including staged, unstaged, binary and untracked files. It does not alter the
user's staging choices. `existing.diff` distinguishes pre-existing work from `changes.diff`.
Ignored files and Forge memory are outside the implementation diff. Concurrent writers
cannot be attributed reliably: keep one implementer per source tree.

QA receives the full implementation input, approach and actual diff in a disposable Git
repository. Source and review fingerprints are checked afterwards; mutations invalidate QA.
This detects mutations; it is not an operating-system security sandbox for unrestricted Bash.

Read `dwarf.last`, `qa.last`, `changes.diff` and `verdict`. Preserve CONFIRMED/PLAUSIBLE labels
when relaying findings. Report scope drift and unsupported implementation claims. Do not
automatically retry: one implementation followed by one review is the run.

Statuses: PASS, FAIL, UNKNOWN, NOCHANGES, INVALIDATED. Exit codes: 0 completed review
(inspect verdict), 2 usage, 3 precondition, 4 dispatch failure, 5 no changes or invalidated
review, 7 timeout. The default timeout is 2700 seconds; 0 disables it.

`--native-review` is an explicit Codex-only alternative: native review cannot accept the
custom requirements prompt or verdict instruction. Its result is informational and UNKNOWN;
it cannot establish requirements coverage. The disposable repository supplies the recorded
baseline via `--review-base`.

Changes remain in the source working tree. Forge does not commit or integrate solo work.
