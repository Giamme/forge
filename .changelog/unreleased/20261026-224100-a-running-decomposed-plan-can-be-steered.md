---
type: added
breaking: false
audience: user
commits: dd4ff7b
---
A running decomposed plan can be steered: `forge control drain` lets running tasks finish and starts nothing new until `forge control resume`; `forge control stop` ends the run once running tasks are done (exit 130, not-started tasks stay pending for the next `run`), and `stop --now` interrupts them; `forge control set <plan> max-parallel N` changes how many tasks run at once.
