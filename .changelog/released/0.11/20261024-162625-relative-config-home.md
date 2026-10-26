---
type: fixed
breaking: false
audience: user
commits: 
---
A relative `XDG_CONFIG_HOME` now counts as unset, as the XDG spec says, so forge registers runs and keeps `jev.json` in `~/.config` instead of recording nothing. Before, `forge ui open` printed a link to a run that forge-ui then did not list.
