---
type: added
breaking: false
audience: user
commits: 
---
forge-ui: each adopted range listed above the final review now opens its review (verdict, gate, findings, what a person kept, the reviewer's reply and prompt) and the range's diff with the findings pinned, through `/api/runs/<key>/adoptions/<name>` and its `/diff`. An export includes them, and keeps each adoption's reason private unless texts are included (#131).
