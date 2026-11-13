# Safety

[← README](../README.md) · [All docs](README.md)

What forge never does to your branch and working tree, what it refuses to run on, and how it keeps reviewed work and merged work the same.

## Safety guarantees

- Solo runs leave source changes uncommitted. Forge creates private review commits, but
  does not commit on the solo source branch, push, or reset the user tree.
- In a decomposed run, commits and merges happen **only** on the `forge/<run-id>/*` task
  branches and the `forge/<run-id>-integration` branch — branches forge created for itself.
  Never your branch, never a push. Your branch and working tree are untouched for the whole
  run.
- Bypass flags are never added on forge's own initiative. `--yolo-dwarf` / `--yolo-qa` are the
  only route to an unsandboxed run, so that choice is always explicit and visible.
- **One dwarf per working tree, always.** Two agents editing one tree interleave their edits
  into a diff neither of them wrote and qa cannot untangle. Parallelism comes from separate
  worktrees, which is why worktrees are mandatory there rather than an optimization.
- A failed task's branch and worktree are **never** deleted — they're the only record of what
  went wrong and the only thing to retry from. A successful task's worktree is removed only
  after `results.tsv` is written and read back.
- QA runs in a disposable repository. Mutations to the source or review snapshot invalidate
  acceptance. Disabled editing tools alone do not prevent writes through Bash.
- A review snapshot is as large as the repository's tree, so forge removes it once its review
  ends: task, final, shadow, solo and adopt reviews, whatever the outcome. `review.base`,
  `review.deps`, the prompt, the log and the verdict stay beside it. `FORGE_KEEP_SNAPSHOTS=1`
  keeps snapshots, for debugging. A review ended by a signal can leave its snapshot behind; the
  task's next review, or the plan's next final review, removes it, and so does `forge runs compact`.
- `forge runs compact` and the background compaction at run start never touch an active run (they
  hold its lock), a worktree, or a file forge does not create, and they import the run into the
  dataset strictly before anything goes. Log copies are gzipped, never deleted.

## Preconditions

- Not a git repo → decomposed runs refuse. Worktrees are impossible and there's no honest
  fallback.
- Uncommitted changes → **warned prominently.** Worktrees branch from a commit, so your local
  edits are invisible to every dwarf. This is the likeliest route to a confusingly wrong
  result.

## Execution integrity

Forge now captures solo changes against a recorded starting snapshot, including staged edits,
without altering the user's index. Existing work is recorded separately in `existing.diff`.
QA receives the full implementation requirements and reviews a disposable repository; source
or snapshot changes invalidate the result. Only the recorded reviewed task commit can merge.

Parallel execution rechecks dependencies before each task and retry. Failed prerequisites leave
consumers BLOCKED. Directory and child file declarations are serialized.

`forge-dispatch.sh doctor --spec sol --role dwarf` validates a selected model recipe using local
CLI help. Solo and parallel runners preflight the whole selected pipeline before implementation.
These checks do not establish authentication, billing or live model availability.

Use `forge-parallel.sh run <plan> --verify 'your check command'` or put the command in
`.forge/verify` to verify the combined result. Output and exit status are saved with the plan.
Integration checks a candidate containing the current user branch before updating it. A missing
command is reported as UNVERIFIED; a failing configured check blocks integration. The project's
own test suites that the command does not run are named as warnings (advisory), and a failing
command is rerun once so a flaky lane is reported rather than failing the run — see
[verification coverage and flaky reruns](reviews.md#verification-coverage-and-flaky-reruns).
`integrate` also names any task that is not merged and therefore not part of what it delivers.
