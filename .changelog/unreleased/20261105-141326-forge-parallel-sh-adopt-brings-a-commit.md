---
type: added
breaking: false
audience: user
commits: a40b250
---
`forge-parallel.sh adopt` brings a commit made outside forge into a run, where `review` used to refuse. It cherry-picks a commit or range, or with `--head` takes what is already on the integration branch, and the run's QA reviews just that range against your reason, with no implementer. A failed review puts the branch back unless `--approved` keeps it, whose findings then reach `forge issues` and the run's report. A review that concludes nothing (a crash, a timeout, a stop) always puts it back. Every adoption is recorded and shown in forge-ui, and `attribute` can name one as the step that broke verification (#79).
