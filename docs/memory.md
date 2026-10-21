# Project memory

[← README](../README.md) · [All docs](README.md)

What forge learns about a repository, where it keeps it, what each role is told, and what forge has spent there.

Every forge dispatch is a clean slate, so without help the tenth run in a repo rediscovers
what the first one learned. forge keeps that in the repo, in two files:

```
.forge/memory.md     small, capped, injected into every prompt
.forge/ledger.tsv    append-only, one row per dispatch, never injected
```

The split is the design. Injected context is paid for on every dispatch of every future run,
so "remember everything" and "stay small" pull against each other — the ledger remembers at
zero context cost and is read only when asked, while `memory.md` is capped at 40 lines / 4 KB.

```markdown
## Verify
- pytest -q runs the suite; make test also lints and is 4x slower

## Known traps
- tests/test_api.py::test_timeout is flaky under -n auto  [tests/test_api.py]

## Recurring QA findings
- 3x: route added without updating openapi.yaml  [src/routes]
```

## How a fact gets in

The role that just did the work writes it down, in its final message, next to the verdict:

```
FORGE_LEARNING: trap | src/proto/*_pb2.py is generated, never hand-edit [src/proto]
```

No extra dispatch and no separate model guessing what mattered — the one that hit the
landmine is the one that records it. Everything after that is deterministic bash.

**What stops it becoming a junk drawer:**

| Guard | Effect |
|---|---|
| recurrence threshold | a `finding` needs **two distinct runs** before it is injected. One occurrence is an incident, not a pattern |
| stale anchors | an entry ending in `[path]` is dropped once that path stops existing, so memory stays true as code moves |
| hard cap | over 40 lines / 4 KB, lowest-count and least-recent facts are trimmed; one entry is clamped to 180 chars |
| bias to silence | the prompt says most runs teach nothing durable and emitting nothing is correct. A false fact costs every future run; a missing one costs a single rediscovery |

Only `verify` and `trap` are kept on first sighting — those are facts, true the first time.

## Slices, and what forge writes

| Role | Gets |
|---|---|
| planner, dwarf | Verify + Known traps + Recurring findings |
| qa | Known traps + Recurring findings |

QA is not told the build command; it is not building anything. It *is* told the recurring
findings, so it can notice this is the fourth time the same bug shipped.

`.forge/` is the **only** thing forge writes into your working tree rather than onto a branch
of its own. It is excluded from every captured diff, so a reviewer never sees it as the
dwarf's work — but it *will* show up as untracked in `git status` after your first run.

## What to do with `.forge/` when it appears

forge never commits it. That decision is yours, and there are three sane answers:

| You want | Do this |
|---|---|
| memory shared with your team, and with future you | **commit `.forge/`** — the intended case; every teammate's runs then build on the same facts |
| memory kept to your machine | `echo '.forge/' >> .gitignore` |
| no memory at all | `--no-memory`, or `FORGE_MEMORY=off` in your environment |

Two things that are easy to get wrong:

**Gitignoring is not the same as turning it off.** An ignored `.forge/` is still written and
still injected into every prompt — it just stops travelling. Only `--no-memory` /
`FORGE_MEMORY=off` actually disables the feature.

**Never commit one file and ignore the other.** `memory.md` is *rebuilt from* `ledger.tsv` on
every write, so a clone that has the memory but not the ledger loses everything the moment
anyone runs forge there — the rebuild regenerates `memory.md` from that machine's own short
ledger and the shared facts are gone, silently. Verified behaviour, not a theoretical risk:

```
teammate A commits memory.md only, ledger.tsv gitignored
  memory.md: - pytest -q runs the suite
             - src/a.py is generated [src/a.py]

teammate B clones, runs forge once, learns one unrelated thing
  memory.md: - b learned something new        ← A's two facts destroyed
```

Commit both, or neither. If you commit them, expect `ledger.tsv` to gain a line per dispatch
— small, but it will appear in your diffs; that growth is the price of the recurrence
counting that keeps `memory.md` honest.

If you contribute to someone else's repo, gitignore it. A pull request containing forge's
notes to itself is noise to a reviewer who does not use forge.

## `scripts/forge-memory.sh`

```
forge-memory.sh inject <repo> <planner|dwarf|qa>   # the block to prepend to a prompt
forge-memory.sh note <planner|dwarf|qa>            # the instruction to append
forge-memory.sh record <repo> --last <file> --role <r> [--run-id X] [--task T]
                              [--model M] [--verdict V] [--duration S]
forge-memory.sh show <repo>
forge-memory.sh prune <repo>                       # re-apply staleness and the cap now
forge-memory.sh spend <repo> [--run <run-id>]      # what forge has cost this repo
```

`record` appends to the ledger and then rebuilds `memory.md` from the whole ledger — the
rebuild is idempotent, so a crashed run can leave the ledger a row short but can never leave
memory half-written. Editing `memory.md` by hand therefore does not stick; remove the ledger
rows instead.

```bash
# what would a dwarf actually be told about this repo?
bash scripts/forge-memory.sh inject ~/dev/api dwarf

# start over
rm -rf ~/dev/api/.forge
```

## Spend

The ledger already holds a row per dispatch and is never injected into a prompt, so
accounting from it costs nothing:

```bash
bash scripts/forge-memory.sh spend ~/dev/api
```
```
14 dispatch(es) across 3 run(s), 1h12m of model time
Durations sum concurrent work, so they exceed the wall-clock of a parallel run.

  by model                     disp     total      mean
  sol:xhigh                       4     38m20s     9m35s
  opus                            7     22m10s     3m10s
  gemini:low:antigravity          3     11m30s     3m50s

  by role                      disp     total
  dwarf                           7     49m50s
  qa                              7     22m10s
```

**Time and dispatch counts, never tokens or money.** The six CLIs expose usage differently
or not at all, and a number forge cannot actually measure would be worse than no number.

`duration_s` is the ledger's tenth column, appended at the end on purpose: a ledger written
by an older forge — possibly already committed and shared — has nine fields, and awk yields
an empty string for the missing one instead of shifting every column. Those rows are counted
but not timed, and the output says so.
