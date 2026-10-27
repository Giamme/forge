---
type: added
breaking: false
audience: user
commits: b93d71d, b77bacb, 374eb46
---
Each attempt in forge-ui lists the tests, type checks, lints and builds its dwarf and reviewer ran, read from their logs, with each one's exit code, time and pass/fail counts and a link to that command in the conversation, and says which commands of the brief's acceptance criteria were never run. forge now keeps a clock beside each agent's log (<role>.clock; FORGE_LOG_CLOCK=off turns it off) so codex and grok commands are timed too.
