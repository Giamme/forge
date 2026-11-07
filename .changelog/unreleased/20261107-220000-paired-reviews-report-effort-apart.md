---
type: added
breaking: false
audience: user
commits: 
---
A shadow review (`--shadow-qa`) no longer repeats its primary's exact settings when another listed spec differs. If every listed spec is identical to the primary, the shadow still runs but is labelled a control, so it measures noise and never counts as an effort comparison. The new `forge dataset pairs` command groups paired reviews by what differs between the two reviewers: effort within one model, another model, another family, or nothing (controls). For each pair of settings it reports verdict agreement with a 95% interval, the blocking findings only one side found, and how many times the tokens and time each side used. Listing a reviewer's own model at another effort, such as `--shadow-qa sol:medium` beside `sol:high` reviewers, now measures what that effort buys (#121).
