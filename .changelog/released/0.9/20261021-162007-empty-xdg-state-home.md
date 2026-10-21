---
type: fixed
breaking: false
audience: user
commits: 
---
With `XDG_STATE_HOME` set but empty, Fractal keeps its state (its runtime and its runs) in `~/.local/state/forge/fractal`, where forge-ui looks, instead of in a `forge/fractal` folder under whatever directory forge was started from. `forge dataset import --fractal` reads the same place.
