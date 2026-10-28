---
type: fixed
breaking: false
audience: user
commits: 
---
`FORGE_FORECAST_BUDGET_S=inf` (or any budget too large to wait for, such as `1e10`) now means no time limit for the plan forecast that `run` prints, instead of silently dropping the forecast.
