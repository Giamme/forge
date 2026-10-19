---
type: added
breaking: false
audience: user
commits: 
---
With a Cloudflare provider configured, forge records two predictions it never acts on: whether each task's first attempt will pass review (once per plan, after models are assigned) and whether a retry will pass with the findings it was handed. They are written beside the task (`clef.first_pass`, `clef.retry_pass.<n>`) and into the run dataset, and the export attaches them to its routing and retry cases, so their accuracy on new runs can be measured before anything relies on them. No prompt, verdict, table or retry ever sees them; `FORGE_JEV_OUTCOMES=off` turns them off.
