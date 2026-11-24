---
type: fixed
breaking: false
audience: user
commits: 
---
Editing a task's brief while its review runs no longer lets that review's `FAIL` count: the attempt is redone on the new text at no cost, with the review's findings passed along. Every other brief change after `plan` (between attempts, while a task waits) is now recorded in `control.log` when the next attempt starts, and the run summary marks a drifted file the brief names as `(named in the brief)` (#212, #220).
