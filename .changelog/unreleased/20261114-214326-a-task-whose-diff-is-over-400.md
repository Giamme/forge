---
type: fixed
breaking: false
audience: user
commits: f0f5455
---
A task whose diff is over 400 KB is reviewed: the reviewer gets the diff's stat and reads the diff in its snapshot, as the whole-run review already does, instead of a prompt its harness refuses. If a harness still refuses a prompt as too large, the task's note says so and points to `adopt` or a split, since a retry would send the same prompt.
