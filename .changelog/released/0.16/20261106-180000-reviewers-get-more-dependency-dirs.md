---
type: changed
breaking: false
audience: user
commits: 
---
Review snapshots now also get `bower_components`, `.bundle`, `Pods`, Mix's `deps` and `_build`, `.dart_tool`, `elm-stuff` and SwiftPM's `.build` linked from the checkout under review, beside `node_modules`, `.venv`, `venv` and `vendor`, so reviewers can run dependency-backed tests in more ecosystems. The fail-first check links the same directories (#127).
