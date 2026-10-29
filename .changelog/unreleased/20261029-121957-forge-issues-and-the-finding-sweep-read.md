---
type: fixed
breaking: false
audience: user
commits: 6a9980c
---
`forge issues` and the finding sweep read a location that lists several lines or adds a note (`path:442,674`, `path:117-119 (also …)`) as its file and first line, instead of dropping the finding as having no location.
