---
type: added
breaking: false
audience: user
commits: e27b871, d88b88f, a08a35e, 3d4ec0e, c070b04
---
`forge decide replay --site S --provider P --model M` re-asks a site's labelled past requests on that provider and model, compares the answers with the live model's and the site's no-model baseline, and records whether it passes. Without `--send` it is a dry run that sends nothing; a replay skips repos under the privacy floor and is charged to no budget. The decision-models guide gains a "Measuring a gate" section.
