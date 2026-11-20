---
type: fixed
breaking: false
audience: user
---
Fixed Bash redirection order (`2>/dev/null < "$file"` instead of `< "$file" 2>/dev/null`) when reading optional plan/group files such as `retry_per_model`, `round`, `conflict.files`, and `/proc/<pid>/winpid`, preventing spurious `No such file or directory` stderr noise during escalations and group operations.
