---
type: fixed
breaking: false
audience: user
commits: 
---
An implementer whose CLI cancelled its session before it changed anything (grok does this when a tool call needs an approval a headless run cannot give) is now an infrastructure stop: the task waits as `INFRA` with no attempt spent. It used to fail as an empty diff marked never to retry, which also blocked every task that depended on it.
