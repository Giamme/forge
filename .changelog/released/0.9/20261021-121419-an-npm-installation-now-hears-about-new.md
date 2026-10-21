---
type: fixed
breaking: false
audience: user
commits: 
---
An npm installation now hears about new releases: the once-a-day notice after a run asks the npm registry (or `npm_config_registry`) and names npm's command, `npm install -g @giamme/forge@latest && forge install`. `forge version` shows that command too, instead of telling npm users to point `forge update --source` at a Git checkout they don't have.
