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
```

Capture is fail-open and never read by anything that builds a prompt. See
[references/dataset.md](../references/dataset.md) for the event schema and its invariants.
