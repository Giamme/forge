---
type: added
breaking: false
audience: user
commits: 
---
`forge update` and `forge version` now work from the forge command. `forge version` shows your version, checkout and upstream (`--check` asks for the newest release), and a finished solo run, `plan` or `run` prints one line when a newer forge is out, at most once a day; it stays quiet offline, in CI or with `FORGE_UPDATE_CHECK=off`.
