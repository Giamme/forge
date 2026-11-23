---
type: fixed
breaking: false
audience: user
commits: 
---
A run stopped while a task's committed attempt was under review (a crash, Ctrl-C, or `forge handoff resume` after one) retries that task with the review only. It used to send the task back to its implementer, whose unchanged code was then refused as identical to the previous attempt and failed without a review; `forge handoff resume` also keeps that attempt's number.
