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
- Bypass flags are never added on forge's own initiative. `--yolo-dwarf` / `--yolo-qa` remove
  whatever fence forge put around a role, so that choice is always explicit and visible. How
  much fence there is without them depends on the harness: forge sets an OS sandbox only on
  codex. See [what each role can do](#what-each-role-can-do-per-harness).
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

## What each role can do, per harness

forge sets an operating-system sandbox only on **codex**. On the other harnesses a role without
yolo is limited by the harness's permission mode, and a role that has a shell can use it for
anything your user account can do: install dependencies, write outside the repository, reach the
network. Disabled editing tools stop the editor tools, not a shell command that writes a file.
Your own harness settings (a sandbox in its config, deny rules) still apply on top of what forge
passes.

| Harness | Dwarf without yolo | Reviewer (QA, planner) without yolo | What yolo changes |
|---|---|---|---|
| codex | `--approve-for-me`: commands run inside the sandbox your codex configuration gives `codex exec`; forge does not choose one | **read-only sandbox**: reads everything, writes only its own temp dir, no edits to the tree | drops the sandbox and approvals (`--dangerously-bypass-approvals-and-sandbox`) |
| claude, openclaude | `--permission-mode acceptEdits --allowedTools Bash`: edits plus an unrestricted shell | the same shell, with `Edit`, `Write` and `NotebookEdit` disabled; it can still write through Bash | `--dangerously-skip-permissions`: every tool, no prompts |
| opencode | `--auto`, always: every tool, including the shell | `--auto --agent plan`: opencode's read-only agent, not a permission boundary | nothing for the dwarf (it already has `--auto`); a reviewer loses `--agent plan` |
| grok | `--permission-mode acceptEdits --allow Bash`: edits plus an unrestricted shell | the same shell, with `Edit` and `Write` denied; it can still write through Bash | `--always-approve`: every tool, no prompts |
| antigravity | `--mode accept-edits`: edits, but **no shell command at all** (a denied command aborts the run) | `--mode plan`: reads only, no shell | `--dangerously-skip-permissions`: the only way to run commands |

What protects your work whatever the harness: a dwarf works in a worktree or on your uncommitted
tree as described above, a reviewer works on a disposable snapshot, and forge fingerprints the
tree before and after each review, so a reviewer that changed what it was judging does not pass.
None of that stops a shell command from writing elsewhere on your machine. If you need that, run
the harness inside its own sandbox (a container, or the harness's sandbox setting).

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
