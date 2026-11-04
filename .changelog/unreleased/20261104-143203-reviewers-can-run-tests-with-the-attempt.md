---
type: fixed
breaking: false
audience: user
commits: 4b65cb4
---
Reviewers can run tests with the attempt's own dependencies: forge links `node_modules`, `.venv`, `venv` and `vendor` from the checkout under review into its review snapshot, at the root and in workspace packages, and the prompt says what is there and tells the reviewer to report rather than search or install (#117).
