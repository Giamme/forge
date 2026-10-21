# forge documentation

[← README](../README.md)

The README is the short overview. These pages hold the rest, one topic each.

| Page | What's in it |
|---|---|
| [Install and update](install.md) | npm or a git checkout, the doctor, what the installer does per CLI, updating |
| [Command reference](commands.md) | every `/forge` flag with examples, the `forge` command, the runner scripts and their exit codes |
| [Models, efforts and harnesses](models.md) | the `alias:effort:harness` spec, effort ladders and clamping, yolo mode, the model registry |
| [Decomposed runs](decomposed-runs.md) | decompose levels and difficulty, routing, waves, capsules, planning, resuming, split and combine, `tasks.tsv` |
| [Reviews, retries and recovery](reviews.md) | verdicts, retries, infrastructure stops, severity thresholds, accepting, whole-run and shadow reviews, guards, verification, fail-first |
| [Fractal execution](fractal.md) | the optional nested-execution backend: install, run, inspect, bounds and recovery |
| [Project memory](memory.md) | `.forge/memory.md` and the ledger, what each role is told, whether to commit it, spend |
| [forge-ui](forge-ui.md) | the run viewer: commands, the shared server, environment variables, privacy |
| [Run dataset](dataset.md) | every run's inputs and outcomes, kept for training decision models |
| [Run artifacts](artifacts.md) | the run directory, a plan's files, worktrees and branches |
| [Safety](safety.md) | what forge never does to your branch, preconditions, execution integrity |
| [Ripwire and Jev](add-ons.md) | optional repository context and advisory judgments |
| [Troubleshooting](troubleshooting.md) | symptoms, causes and fixes |
| [Development](development.md) | repository layout, offline checks, runtime measurement |

Deeper material that the skill itself reads lives in [`references/`](../references/): the
per-harness details, the decomposition and Fractal contracts, memory internals, Jev and the
dataset schema.
