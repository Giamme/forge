---
type: added
breaking: false
audience: user
commits: 
---
`plan --implementer-session` lets a retry continue the implementer's own claude or openclaude session, so it does not re-read what it already knows, when its spec is unchanged and its work is still what the review judged; otherwise the retry starts fresh as before. Each attempt is logged in `tasks/<id>/implementer_sessions.tsv`, and `forge dataset status` counts implementer continues apart from reviewer ones.
