---
type: added
breaking: false
audience: user
---
`plan --explore E` (0 to 0.5, or `FORGE_ROUTE_EXPLORE`) now and then gives a task you left to forge another of the models you passed to that plan, instead of its pool's turn, and records each decision with its probability in the run dataset. Until now every outcome was the outcome of the model the router chose, so nothing could say whether another model would have done better; these recorded odds make that estimable later. Forge still never uses a model you did not name, explored rows are marked `*` in the approval table so the spend is visible before you approve, pinned rows are never touched, and without the flag a plan is exactly as before. A model change made by `retry --dwarf` is now recorded too, at the moment it happens, and Jev's routing calibration leaves explored tasks out.
