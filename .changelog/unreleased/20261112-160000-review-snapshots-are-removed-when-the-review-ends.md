---
type: fixed
breaking: false
audience: user
commits: 
---
Run directories no longer grow by a full copy of the repository for every review: forge removes each review snapshot (task, final, shadow, solo and adopt reviews) once the review ends, whatever its outcome, and a task's next review removes one an interrupted review left behind. `review.base`, `review.deps`, the prompt, the log and the verdict stay. `FORGE_KEEP_SNAPSHOTS=1` keeps them for debugging. Snapshots that existing runs already hold are untouched (#139).
