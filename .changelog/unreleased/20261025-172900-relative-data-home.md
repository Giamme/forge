---
type: fixed
breaking: false
audience: user
commits: 
---
A relative `XDG_DATA_HOME` now counts as unset, as the XDG spec says, so the run dataset, run archives and exports stay in `~/.local/share/forge` instead of a folder inside whichever repo forge ran from. `forge config show` reports the same place.
