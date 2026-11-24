---
type: added
breaking: false
audience: user
commits: 
---
Three limits bound every split: `plan --split-depth N` (default 2 levels; 0 turns splitting off), `--split-children N` (6 subtasks per split) and `--split-nodes N` (24 subtasks in the run). They are shown under the approval table and frozen by the first run. A split that would pass one is not made: the task runs as one piece and is recorded as `bounded`. One `--max-parallel` bounds the whole tree (#153).
