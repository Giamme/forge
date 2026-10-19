---
type: added
breaking: false
audience: user
commits: 
---
After three failed attempts on one model, a decomposed task's next retry goes a tier up in the pools you gave `plan`, instead of repeating a dispatch whose fourth try passes least often. When no stronger model exists, the task stops with a split suggestion, and a plain `retry` refuses until you split the task or name a model with `--dwarf`. Use `--escalate-after N` to change the threshold, or `--escalate-after 0` to turn this off.
