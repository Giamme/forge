---
type: fixed
breaking: false
audience: user
commits: 
---
A grok dwarf or reviewer that runs its tests in the background no longer ends as an error after 10 minutes: grok now waits for that work as long as forge's own time limit allows, and `forge-dispatch.sh doctor` checks that the installed grok supports this.
