---
type: fixed
breaking: false
audience: user
commits: 
---
`forge ui open DIR` now reads only the first line of the run's `repo` file, as the runners do, so a run whose `repo` file has extra lines is listed under its real repo instead of under a path that does not exist.
