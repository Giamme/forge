---
type: fixed
breaking: false
audience: user
---
A decision-model request made from inside a repo, with no `--repo` and no `FORGE_DECIDE_REPO`, now records in `jev.jsonl` and the run dataset the gate mode it was actually sent under, the one that repo's `.forge/decide.json` sets, instead of the mode resolved with no repo at all (#82).
