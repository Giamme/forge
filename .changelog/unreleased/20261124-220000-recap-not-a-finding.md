---
type: fixed
breaking: false
audience: user
commits: 
---
A retry review that passes no longer turns into `FAIL` because the reviewer recapped the last round's findings. A bullet marked `(fixed)`, `(resolved)` or `(addressed)`, and the list under a recap title such as "Previous findings", are no longer read as blocking findings of this round; a recap that says a finding is not fixed, still present or regressed still blocks, and so does a `CONFIRMED` finding listed with the recap that never says it was fixed. Reviewers are also asked not to lead a fixed finding with its severity (#254).
