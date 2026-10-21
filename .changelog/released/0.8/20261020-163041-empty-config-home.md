---
type: fixed
breaking: false
audience: user
commits: 
---
With `XDG_CONFIG_HOME` set but empty, `forge ui open` wrote its run list into a `forge/` folder in the current directory (usually your repo), where forge-ui could not find it; runs were left off forge-ui's run map, and `forge jev` read `jev.json` from the current directory. An empty value now counts as unset, as the XDG spec says, so all of it lives in `~/.config/forge`.
