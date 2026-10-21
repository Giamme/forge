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
| task status `INFRA`, `run` exits `8` | a quota/usage limit, auth problem, rate limit, network error or empty answer stopped a dispatch — not a task failure, and no attempt was spent. Read `tasks/<id>/infra.txt` (`class=`, `retry_after=`), fix the cause, run the same command again. `run --infra-retries M` waits it out instead. Dependents stay `PENDING`, never `BLOCKED` |
| a task merged although its reviewer said FAIL | `--qa-threshold` accepted it: every CONFIRMED finding was labelled and milder than the threshold. See `tasks/<id>/known_issues.md` and `qa.gate`; nothing is accepted without an explicit threshold |
| `accept` exits `3` "changed since it was reviewed" | the branch or worktree moved after QA, so accepting would sign off on unreviewed code. `retry` for a fresh review |
| `combine` / `split` refuses | the message names why: unmerged task left behind, duplicate ids, locked or dirty plan, a Fractal-backed plan, or a selection that strands a dependent. Nothing was changed |
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
| `retry` says "produced no new changes" | the dwarf returned a byte-identical diff, so no reviewer was paid to read the same code twice. Escalate with `--dwarf`, or fix the prompt |
| a new file's code never reached QA | fixed: untracked files are diffed against `/dev/null`. A hand-built capture using only `git status --porcelain` shows the reviewer a filename and no content |
| claude dwarf ends "I need your permission to edit" | a permission mode that prompts for Edit. forge uses `acceptEdits` plus an explicit Bash allowance, because `auto` allows shell but prompts for Edit, and `acceptEdits` alone allows Edit but prompts for real shell commands |
| `.forge/` turns up in a review | a diff capture is missing `':(exclude).forge'` |
| memory stays empty | nothing durable was learned — the common, correct case. The ledger still has a row per dispatch |
| a wrong fact keeps reappearing | `memory.md` is rebuilt from the ledger; delete the ledger rows, not the memory line |
| dwarves build incompatible interfaces | the seam was never agreed — plan the approach, or use `--planner` |
| Fractal was installed but a new unattended run uses ordinary execution | installation never activates it; pass `--fractal` explicitly |
| `Fractal selected but unavailable` | run `forge fractal doctor`, then the suggested managed install; resume retains the selected backend |
| existing run rejects a new backend, pool or limit | selection and bounds are recorded; create a new run directory for changed settings |
| Fractal reports completion but Forge has not accepted the work | inspect QA, verdict and verification; execution completion does not establish acceptance |
| Fractal import stops with `source_drift` | the source changed after its snapshot; preserve both versions and inspect the candidate instead of forcing import |
| Fractal work was paused or its coordinator exited | inspect `status` and `logs`, then use explicit `resume RUN_ID`; retry starts a new attempt |
| browser dashboard closed while work is running | execution is independent; reopen with `forge fractal open RUN_ID` |
| costs are unknown or a dollar cap is rejected | the bridge only reports available usage; v1 cannot enforce a spending cap |

For per-harness invocation details, effort ladders and known CLI failure modes, see
[`references/harnesses.md`](../references/harnesses.md). For decomposition mechanics, see
[`references/decompose.md`](../references/decompose.md). For memory internals, see
[`references/memory.md`](../references/memory.md).
