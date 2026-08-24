# Project memory

How forge remembers what it learned about a repository across runs: the `FORGE_LEARNING`
grammar, what gets promoted into a prompt and what only reaches the ledger, and how memory
stays small and true as the code moves under it.

Read this when memory looks empty, looks wrong, or has grown past its budget.

## Contents

- [Why two files](#why-two-files)
- [Committing, ignoring, disabling](#committing-ignoring-disabling)
- [The sentinel](#the-sentinel)
- [Promotion](#promotion)
- [Dedup, and the limits of it](#dedup-and-the-limits-of-it)
- [Staleness and the cap](#staleness-and-the-cap)
- [Injection slices](#injection-slices)
- [The diff exclusion](#the-diff-exclusion)
- [Commands](#commands)
- [Failure modes](#failure-modes)

## Why two files

```
.forge/memory.md     curated, hard-capped, injected into every prompt
.forge/ledger.tsv    append-only, one row per dispatch, NEVER injected
```

Injected context is paid for on every dispatch of every future run — with a decomposed run
that is 2N prompts, forever. So the two things a memory has to do pull in opposite
directions: remembering everything makes it big, and staying small means forgetting.

Splitting them resolves that. The ledger is unbounded and costs nothing, because nothing
reads it unless asked; it is what recurrence is counted from and what answers "has this been
tried here before". Only `memory.md` is in prompts, and it is capped hard.

Never inline the ledger into a prompt. That would undo the whole arrangement.

Both live in the repo so the memory travels with it, and both are written by forge but
committed by the user — forge does not commit on anyone's behalf.

## Committing, ignoring, disabling

`.forge/` appears untracked after the first run. Three coherent choices:

| Intent | Action |
|---|---|
| share memory with the team | commit `.forge/` |
| keep it to this machine | `echo '.forge/' >> .gitignore` |
| no memory at all | `--no-memory` or `FORGE_MEMORY=off` |

Gitignoring does **not** disable anything: an ignored `.forge/` is still written and still
injected into every prompt. Only the third row turns the feature off.

**The two files must share a fate.** Because `memory.md` is rebuilt from `ledger.tsv` on
every write, committing the memory while ignoring the ledger destroys the shared facts the
first time anyone runs forge on a fresh clone — the rebuild regenerates `memory.md` from that
machine's own short ledger, and nothing warns:

```
A commits memory.md only:   - pytest -q runs the suite
                            - src/a.py is generated [src/a.py]
B clones, runs forge once:  - b learned something new     <- A's facts gone
```

Committing both means `ledger.tsv` grows a line per dispatch and shows up in diffs. That is
the price of the recurrence counting; the alternative is memory that cannot tell a one-off
from a pattern.

For a repo you contribute to but do not own, ignore it — a PR carrying forge's notes to
itself is noise to a reviewer who does not use forge.

## The sentinel

Extraction happens in-band, in the final message of the role that just did the work:

```
FORGE_LEARNING: verify | pytest -q runs the suite; make test also lints and is 4x slower
FORGE_LEARNING: trap | test_api.py::test_timeout is flaky under -n auto [tests/test_api.py]
FORGE_LEARNING: finding | route added without updating openapi.yaml [src/routes]
```

`<category> | <text> [optional anchor]`. Categories:

| Category | Is |
|---|---|
| `verify` | how this project is actually built, tested or checked |
| `trap` | a landmine: a flaky test, a generated file, a sharp edge |
| `finding` | a *mistake pattern* likely to recur — emitted by qa, not a description of one bug |

This costs no extra dispatch, which is the reason it works at all: a separate "librarian"
model would have to re-derive from a transcript what the working model already knew. The
role that hit the landmine is the one that writes it down.

`forge-memory.sh note <role>` produces the instruction. It is deliberately biased toward
silence — "most runs teach nothing durable; emitting no line is the normal, correct
outcome" — because a false fact is worse than a missing one. A missing fact costs one
rediscovery; a false one is injected into every future prompt until someone notices.
Measured behaviour matches: on a trivial task with nothing to learn, models emit nothing;
on a task with a real trap, they emit it.

## Promotion

| Category | Enters `memory.md` |
|---|---|
| `verify`, `trap` | on first sighting — these are facts, true the first time |
| `finding` | only after **two distinct runs** |

Everything reaches the ledger immediately regardless; promotion governs only what gets
injected. The threshold is the main guard against a junk drawer: one QA noticing one bug is
an incident, and putting it in every future prompt would be noise. Two separate runs finding
the same thing is a pattern worth telling the next dwarf about.

Recurrence counts **distinct runs**, not lines — one run emitting the same learning twice is
still one occurrence. Promoted findings display their count (`3x:`), which is also how a
human spots a systemic problem worth fixing properly.

## Dedup, and the limits of it

The key is the text lowercased with every run of non-alphanumerics collapsed to one space.
So `Route added without updating openapi.yaml` and `route added, without updating
openapi.yaml!` are the same key; a genuinely different phrasing is not.

This is exact matching, not fuzzy, and pretending otherwise would be worse than stating it.
What makes it work in practice is a feedback loop rather than cleverness: memory is *in the
prompt*, and `note` asks explicitly to reuse the wording of anything already listed. A
reviewer that can see `2x: route added without updating openapi.yaml` tends to re-emit that
line rather than invent a synonym, so repeats converge on one phrasing.

`memory.md` is rebuilt from the whole ledger on every write rather than edited in place.
Rebuilding is idempotent and order-independent, so a crashed or concurrent run cannot leave
memory half-updated — at worst the ledger is one row short.

## Staleness and the cap

An entry ending in `[path]` is anchored. When that path stops existing the entry is dropped:
the code it describes is gone, so the fact is at best useless and at worst actively
misleading. This is what keeps memory true as the codebase moves, and it is why `note` asks
for an anchor wherever one makes sense.

Caps are 40 lines and 4 KB (`FORGE_MEMORY_MAX_LINES`, `FORGE_MEMORY_MAX_BYTES`). Over cap,
entries are trimmed from the bottom — lowest recurrence count first, then least recent.
A single entry is clamped to 180 characters (`FORGE_MEMORY_MAX_FACT_CHARS`), truncated at a
word boundary with its anchor preserved, so one discursive model cannot spend the whole
budget on one line.

## Injection slices

| Role | Gets |
|---|---|
| planner | Verify + Known traps + Recurring findings |
| dwarf | Verify + Known traps + Recurring findings |
| qa | Known traps + Recurring findings |

QA is not told the build command because it is not building anything. It *is* told the
recurring findings, so it can notice this is the fourth time the same bug shipped, and the
traps, so it does not report a known-generated file as a defect.

## The diff exclusion

`.forge/` must be excluded from every diff capture:

```bash
git diff -- . ':(exclude).forge'
git add -A -- . ':(exclude).forge' && git diff --cached -- . ':(exclude).forge'
```

Two distinct failures otherwise, both quiet:

- memory is rewritten in the working tree at the end of every run, so the *next* run's
  capture hands it to qa as if a dwarf had written it — an unrelated file in the diff, which
  a good reviewer will correctly flag as scope creep;
- if `.forge/` ever entered a task's `files`, every task would overlap every other task and
  the wave planner would serialize the entire run while reporting it as deferrals.

## Commands

```bash
forge-memory.sh inject <repo> <planner|dwarf|qa>   # block to prepend; empty if no memory
forge-memory.sh note <planner|dwarf|qa>            # the instruction to append
forge-memory.sh record <repo> --last <file> --role <r> [--run-id X] [--task T]
                              [--model M] [--verdict V]
forge-memory.sh show <repo>
forge-memory.sh prune <repo>                       # re-apply staleness and cap now
```

`--no-memory`, or `FORGE_MEMORY=off` in the environment, disables all of it: nothing is
injected, nothing is recorded, and no `.forge/` is created.

## Failure modes

| Symptom | Cause |
|---|---|
| memory stays empty | nothing durable was learned — the common and correct case. Check the ledger: rows with `-` categories mean dispatches ran and emitted nothing |
| a fact you wanted is missing | it was a `finding` seen once; it is in the ledger and will promote on its second run |
| the same fact appears twice, worded differently | dedup is exact-after-normalization; delete the weaker line from `memory.md`, or let the cap evict it |
| a fact is wrong and keeps coming back | it is in the ledger. Editing `memory.md` alone will not hold — the next rebuild regenerates from the ledger; remove the ledger rows |
| memory vanished after a refactor | its entries were anchored to paths that no longer exist and were dropped as stale. Working as intended |
| `.forge/` shows up in a review | a diff capture is missing the exclude pathspec |
| memory in a worktree is stale | inject reads the **main repo**, not the worktree; on a repo's first run `.forge/` is not committed, so a worktree branched from the base commit does not have it |
| a teammate's clone lost every shared fact | `memory.md` was committed but `ledger.tsv` was not; the first `record` there rebuilt memory from an empty ledger. Commit both, or neither |
| `.forge/` keeps appearing in `git status` | expected — forge never commits it. Commit it to share, or gitignore it to keep it local |
