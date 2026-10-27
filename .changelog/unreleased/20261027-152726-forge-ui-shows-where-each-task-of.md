---
type: added
breaking: false
audience: user
commits: b93d71d, b77bacb, 374eb46
---
forge-ui shows where each task of a decomposed run is: the task table's Now column and a task's Now section name the command a running dwarf or reviewer is running and for how long, flag it once its log has been quiet for five minutes, and say why a task that is not running waits (its dependencies, a task it shares files with, no free slot, a drain or stop, an infrastructure pause with its countdown, or the retry policy's stop with the split it suggests). The run's inspector says what the scheduler is doing.
