---
type: added
breaking: false
audience: user
commits:
---
forge-ui has an Open findings view: the findings a run, or every run of its repository, left open, with their filed state. Select some to file as GitHub issues or dismiss ones that need no work. `forge issues` gains `--offline`, `--only`, `--all-runs`, `--dismiss`/`--undismiss` (with `--reason`), `--include-dismissed` and a `findings` list in its JSON; the whole-run review's findings are now collected, and filing and dismissal take a repository lock (exit 3 when busy).
