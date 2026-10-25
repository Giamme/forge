# Run dataset

[← README](../README.md) · [All docs](README.md)

Run dirs get deleted, and the ledger keeps spec strings and verdicts but no inputs. So
every run also records its raw inputs and outcomes outside every repo, in
`~/.local/share/forge/dataset/` (`FORGE_DATA_HOME` moves it, `FORGE_DATASET=off` turns
it off). That covers resolved models, prompts, per-attempt diffs, verdicts and findings,
tokens, merge and verification outcomes, and the full request behind every Jev judgment.
It exists to train and calibrate decision models on what forge actually did.

```sh
forge dataset status            # size, counts, and anything that looks incomplete
forge dataset import ~/.local/share/forge/legacy   # runs that predate live capture
forge dataset export            # labeled decision cases for benchmarks and fine-tuning
forge dataset outcomes          # which integrated lines survived, read from git history
forge dataset reviewers         # how often work each reviewer PASSed was later reverted or rewritten
forge dataset forecast <plan>   # likely tokens, time and first-try PASS rate for a plan's tasks
```

`forge dataset reviewers` is a report card for reviewer models, built on those outcomes. It is
advisory only and never changes routing, and it shows "not enough data" below 10 outcomes per
reviewer rather than a rate a handful of tasks would make up. See
[the reviewer report card](../references/dataset.md#reviewer-report-card).

`forge dataset forecast <plan>` estimates a plan from past tasks at the same tier and model:
median and p90 tokens and time, and the first-try PASS rate with a 95% interval, per task and in
total. It flags tasks below 50% and says "not enough history" under 5 past tasks. `run` prints
the same block before it dispatches (`--no-forecast` turns it off) and records it, so its accuracy
can be checked later. It is advisory only. See [plan forecast](../references/dataset.md#plan-forecast).

Deleting a run keeps what it taught the dataset. `forge runs rm <run-dir>` first imports the run
**strictly** (every event must be stored, or nothing is deleted), then writes a self-describing
archive under `<data home>/archive/`: a `.tar.gz` whose `FORGE_ARCHIVE.json` records the run
dir's original path, run id and dataset uid, so `forge dataset import` maps it back to the same
run later. It records `run_delete_started` and `run_deleted` events around the removal. The same
import also reads a plan's `escalations.tsv` (with who decided: `policy`, `retry` or `pin`) and
`control.log`, so human interventions made with `forge control` are in the dataset as
`control` events. See [forge runs rm](commands.md#forge-runs-rm).

Capture is fail-open and never read by anything that builds a prompt. See
[references/dataset.md](../references/dataset.md) for the event schema and its invariants.
