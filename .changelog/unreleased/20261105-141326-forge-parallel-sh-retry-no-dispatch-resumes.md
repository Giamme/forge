---
type: added
breaking: false
audience: user
commits: 2c7dd54
---
`forge-parallel.sh retry --no-dispatch` resumes several stopped tasks side by side: it records the decision, carries the findings forward and starts nothing, and one `run` then dispatches them all under `--max-parallel`. `--decide` and `--dwarf` now combine, so "this model, two more attempts" is one decision, on `retry` and on `forge control retry`. The run summary shows how (#118).
