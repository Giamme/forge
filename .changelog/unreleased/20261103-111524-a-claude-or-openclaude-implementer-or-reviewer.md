---
type: fixed
breaking: false
audience: user
commits: 
---
A claude or openclaude implementer or reviewer that gave its final result but stayed open on a background task is no longer killed as a timeout. forge ends it after 30 minutes of silence (`FORGE_RESULT_GRACE`, `0` turns it off), or at the watchdog, and its result goes to review; a turn the session starts after a result is never cut short (#110).
