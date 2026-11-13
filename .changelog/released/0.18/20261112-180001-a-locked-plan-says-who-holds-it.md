---
type: fixed
breaking: false
audience: user
commits: 
---
A forge command refused by a plan's lock (`accept`, `retry`, `integrate` and the others while another one runs) now says which command holds the lock, with its pid and start time, and on a live run which `forge control` command to use instead, rather than a bare `[Errno 35] Resource temporarily unavailable` (#138).
