---
type: security
breaking: false
audience: user
commits: 
---
A decision-model API key or token that straddled the 200-character cut of an error message could leave its first characters in `jev.jsonl` and `jev.skip`; error text is now redacted before it is shortened.
