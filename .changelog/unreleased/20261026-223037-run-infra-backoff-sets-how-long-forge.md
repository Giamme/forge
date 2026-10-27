---
type: added
breaking: false
audience: user
commits: ca42755
---
`run --infra-backoff` sets how long forge waits between infrastructure retries: `exp:<base>[,<cap>]` (the default, `exp:60`), `linear:<start>,<step>[,<cap>]` or a list of waits such as `10,20,30`. `forge control set <plan> infra-backoff` changes it while the run goes on, and each wait message names the shape it used.
