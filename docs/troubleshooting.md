# Troubleshooting

[← README](../README.md) · [All docs](README.md)

Symptoms, causes and fixes, from a skill a harness cannot see to a task that will not merge.

| Symptom | Cause / fix |
|---|---|
| skill missing in **codex** only | frontmatter isn't strict YAML. Codex refuses to load and logs `invalid YAML`; the other four are lenient and load it fine. Note codex's line numbers exclude the opening `---`, so its "line 3" is the file's line 4 |
| skill missing in **antigravity** | `agy` ignores `~/.gemini/skills` entirely — skills load only inside a plugin. Re-run `forge-install.sh` |
| antigravity runs an **old version** | `agy plugin install` copies. Re-run `forge-install.sh` after every edit |
| duplicate entry in **opencode** | opencode auto-scans `~/.claude/`; a `~/.config/opencode/command/forge.md` on top is redundant |
| agy: `Find command timed out` | missing `--add-dir <repo>` — forge passes it; a hand-built command must too |
| agy dwarf ends with an error and an empty diff | it tried to run a shell command without yolo. A single denial aborts the run — use `--yolo-dwarf` |
| dwarf produced no changes | usually an ambiguous prompt it couldn't resolve headlessly. Nobody can answer a question mid-run |
| task status `ERROR` | worktree creation or a dispatch failed — read `tasks/<id>/dwarf.out` / `qa.out` |
| task status `UNKNOWN` | QA never emitted a verdict line; read `tasks/<id>/qa.last`. `retry` (or `run --retry-failed N`) asks QA again on the unchanged work — no dwarf runs, no attempt is spent |
| task status `INVALIDATED`, "artifact changed during review" | the reviewer changed a tracked file in its review copy, so its verdict counts for nothing. `retry` (or `run --retry-failed N`) reviews the unchanged work again — no dwarf runs, no attempt is spent. Untracked leftovers such as `__pycache__/` never cause this |
| task status `INFRA`, `run` exits `8` | an infrastructure stop, not a task failure, and no attempt was spent. The summary names the cause (`capacity`, `rate_limit`, `usage_limit`, `budget`, `auth`, `network`, `provider_down`, `empty`, `cancelled`; an old run may say `quota`). Read `tasks/<id>/infra.txt`. A line that does not say "needs you" can be waited out with `run --infra-retries M`, or the same command run again. Dependents stay `PENDING`, never `BLOCKED`. See [infrastructure failures](../references/decompose-recovery.md#infrastructure-failures) |
| "needs you" and the cause is **budget** | the harness is out of money. Add credits, or check that harness's billing. The pause spends no waits. A healthy peer of the same tier may already have taken the task |
| "needs you" and the cause is **auth** | log in to that harness again. The pause spends no waits |
| "needs you" and the cause is a **usage limit** | the account's usage window, no peer could take the work, and the reset is unknown or more than six hours away. Wait for the reset, or add another harness to this tier's pool |
| "needs you" and the cause is **cancelled** | the harness cancelled the dispatch. Check that harness's approval and permission settings. The first such stop is tried again at once; with nowhere to move, the pause needs you and spends no waits |
| a task merged although its reviewer said FAIL | the threshold (`P2` by default) accepted it: every CONFIRMED finding was labelled and milder than the threshold. See `tasks/<id>/known_issues.md` and `qa.gate` (decision `accept`); [the verdict rule](reviews.md#verdicts-and-failure) |
| a task FAILed although its reviewer said PASS | forge's gate: a CONFIRMED finding at or above the threshold was listed. `qa.gate` says `fail`, and the next attempt starts with a note saying so. Fix those findings, or `accept` the task if a person decides to; [the verdict rule](reviews.md#verdicts-and-failure) |
| `accept` exits `3` "changed since it was reviewed" | the branch or worktree moved after QA, so accepting would sign off on unreviewed code. `retry` for a fresh review |
| `combine` / `split` refuses | the message names why: unmerged task left behind, duplicate ids, locked or dirty plan, a plan made with Fractal (removed in 0.23), or a selection that strands a dependent. Nothing was changed |
| `integrate --final-review` stops with exit `5` | the whole-run reviewer found a blocking defect (or never reached a verdict); findings are in `<plan>/…/final-review/qa.last`. Your branch is untouched. Exit `8` means the reviewer hit a quota/auth stop: run it again |
| `FLAKY` in the summary, verification `PASS` | the verify command failed once and passed on rerun. Read `verification.log` (the first run) — an intermittent failure can be a real bug. `--verify-retries 0` restores the strict single run |
| `verify coverage: suite '…' is not run by the verify command` | the project defines a suite your `--verify` skips. Advisory only; add it to the command (or `.forge/verify`) if it should gate the merge |
| implementer guard notes in the summary | the dwarf committed, switched branch, left processes running, or promised later work. Recorded in `tasks/<id>/guard.txt`; the diff and the reviewer still decide. Leftover processes were stopped |
| task status `CONFLICT` | QA passed but the merge conflicted — `files` was understated somewhere. Check the run's scope-drift list: it usually names the file |
| task status `TIMEOUT` | that role exceeded the dispatch timeout and was killed. Raise `--timeout`, or `retry` — the partial edit is still in the worktree |
| a dispatch never returns | it can't any more: every backend is killed at its timeout (`--timeout`, else `FORGE_TIMEOUT`, else the registry's `timeout` column, else 45 min — 60 min for the default `xhigh` QA/planner effort) |
| a task won't re-run: "already exists" | a worktree directory was deleted by hand. `run` and `retry` prune stale registrations first, so re-run rather than deleting the branch |
| a failed task is expensive to redo | it isn't — `forge-parallel.sh retry <dir> <id> [--dwarf <spec>]` re-runs that one task with its reviewer's findings, in the worktree it already has |
| an interrupted run seems lost | run `run` again; merged tasks are skipped, not re-dispatched |
| every wave has exactly one task | `files` sets overlap across most tasks; the decomposition isn't actually parallel |
| dwarves reimplement already-merged work | `goal.txt` or `files` missing, so the capsule carries no useful status |
| several tasks report "produced no changes" | the decomposition made tasks that weren't independently actionable — use a coarser `--decompose-level`, not more retries |
| `retry` says "produced no new changes" | the dwarf returned a byte-identical diff against the same brief, so no reviewer was paid to read the same code twice. Escalate with `--dwarf`, or clarify `prompt.md` / `approach.md` (an updated brief re-reviews even unchanged code) |
| a new file's code never reached QA | fixed: untracked files are diffed against `/dev/null`. A hand-built capture using only `git status --porcelain` shows the reviewer a filename and no content |
| claude dwarf ends "I need your permission to edit" | a permission mode that prompts for Edit. forge uses `acceptEdits` plus an explicit Bash allowance, because `auto` allows shell but prompts for Edit, and `acceptEdits` alone allows Edit but prompts for real shell commands |
| `.forge/` turns up in a review | a diff capture is missing `':(exclude).forge'` |
| memory stays empty | nothing durable was learned — the common, correct case. The ledger still has a row per dispatch |
| a wrong fact keeps reappearing | `memory.md` is rebuilt from the ledger; delete the ledger rows, not the memory line |
| dwarves build incompatible interfaces | the seam was never agreed — plan the approach, or use `--planner` |
| `--fractal…` or `forge fractal` says it `was removed with Fractal` | forge 0.23 removed that backend; drop the flag. A task is split into subtasks by forge itself: [Splitting a task into subtasks](decomposed-runs.md#splitting-a-task-into-subtasks) |
| `run`, `retry` or `forge handoff resume` says a plan `was made with Fractal` | an old plan stays readable (`forge runs`, forge-ui) but is never run again; start a new run for the work that is left. `forge du` shows what the old runtime left on disk |
| a task is `BLOCKED` and the summary says `waiting for their subtasks` | a subtask failed or waits for you; no attempt of the task was spent. Retry it in its own plan (`retry <plan-dir>/tasks/<id>/sub <subtask>`), then `run <plan-dir>` again |
| `not split`, and the node reads `bounded` | the split would pass a limit of the run (`--split-depth`, `--split-children`, `--split-nodes`); the task runs as one task. The limits are frozen by the first run |
| a `plan` that changes `--split-…` is refused | the limits cannot change once the plan has run; make a new plan for other limits |

For per-harness invocation details, effort ladders and known CLI failure modes, see
[`references/harnesses.md`](../references/harnesses.md). For decomposition mechanics, see
[`references/decompose.md`](../references/decompose.md). For memory internals, see
[`references/memory.md`](../references/memory.md).
