---
type: fixed
breaking: false
audience: user
commits: 
---
forge-ui started directly (`python3 -m forge_ui`) now finds forge's run registry the way forge writes it: an `XDG_CONFIG_HOME` that starts with `~` lists your runs instead of none, and a relative `XDG_CONFIG_HOME`, where forge never registers runs, no longer reads run lists from whatever directory you started it in.
