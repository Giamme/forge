---
type: fixed
breaking: false
audience: user
commits: 
---
The run dataset no longer drops an event when several writers in one process are the first ever to write to a new store: switching the new file to WAL is no longer raced by every connection, and losing that race is harmless.
