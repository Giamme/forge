---
type: added
breaking: false
audience: user
commits: a40b250
---
`forge-parallel.sh adopt` brings a commit made outside forge into a run, where `review` used to refuse. It cherry-picks a commit or range, or with `--head` takes what is already on the integration branch, and the run's QA reviews just that range against your reason, with no implementer. A failed review puts the branch back unless `--approved` keeps it, and every adoption is recorded and shown in forge-ui (#79).
