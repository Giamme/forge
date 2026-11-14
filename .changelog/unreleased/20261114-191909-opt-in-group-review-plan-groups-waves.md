---
type: added
breaking: false
audience: user
commits: 
---
Opt-in group review (`plan --groups waves --group-review`): related tasks share one QA dispatch over their combined diff. Each finding goes back only to the member whose files it names (or the members a `{tasks=…}` tag names), the group merges in one step once it passes, and a finding that names no member stops the group for your decision (`forge group show|retry`). Groups and their review show in forge-ui. Group dispatches are recorded as `scope=group` and kept out of reviewer statistics. Fewer dispatches are not yet a measured saving.
