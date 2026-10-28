---
type: fixed
breaking: false
audience: user
commits: 
---
A task that deletes large files no longer breaks its review: deleted files now appear in the reviewer's diff by name only, without their contents. Previously, deleting a big data directory could make the prompt too long for the reviewer's CLI, which refused it and left the task in ERROR.
