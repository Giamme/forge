---
type: added
breaking: false
audience: user
commits: 128ab9e, f39d797, 548ec1b, ecea317, f2f5ed5, be9b4a7, fcdb0c6
---
A gate goes `on` only for a provider and model with a passing replay: `forge decide gate G on`, and `forge config set` of an `on` gate's provider or model, refuse (exit 3) and name the replay to run. `--force` turns it on anyway, and a forced gate is marked in `forge decide status` and `report`, `forge config show`, the plan's decide line and forge-ui's Settings, which shows the whole refusal. `forge decide migrate` marks the gates it turns on as forced.
