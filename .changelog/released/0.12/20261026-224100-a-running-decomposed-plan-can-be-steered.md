---
type: added
breaking: false
audience: user
highlight: true
commits: dd4ff7b, 9dda2f8
---
A running decomposed plan can be steered: `forge control drain` lets running tasks finish and starts nothing new until `forge control resume`; `forge control stop` ends the run once running tasks are done (exit 130 without verifying, unless every task merged; not-started tasks stay pending for the next `run`), and `stop --now` interrupts them; `forge control set <plan> max-parallel N` changes how many tasks run at once.
