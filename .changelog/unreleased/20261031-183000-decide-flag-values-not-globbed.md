---
type: fixed
breaking: false
audience: user
---
`--gate` and `--decide-budget` take a `*` or `?` in their value as typed: it is no longer matched against file names in the current directory, so `--gate 'drift=*'` next to a file named `drift=on` is refused and named as given instead of quietly becoming `drift=on` (#82).
