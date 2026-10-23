---
type: added
breaking: false
audience: user
commits: 46cd4bd a57f16b c28b9aa dd75f58 26cd191 6039e91 b69d5d4 d91254c f3a8d10 1d2d87d
---
forge-ui has an Open findings view: the findings a run, or every run of its repository, left open, with their filed state. Select some to file as GitHub issues or dismiss ones that need no work. `forge issues` gains `--offline`, `--only`, `--all-runs`, `--dismiss`/`--undismiss` (with `--reason`), `--include-dismissed` and a `findings` list in its JSON; the whole-run review's findings are now collected, and filing and dismissal take a repository lock (exit 3 when busy).
