---
type: changed
breaking: false
audience: user
commits: 
---
Runs on claude, openclaude and opencode now keep their whole conversation. forge starts claude and openclaude with `--output-format stream-json --verbose` and opencode with `--format json`, so `<role>.log` holds every message, tool call and result instead of only the final answer (claude) or plain text (openclaude, opencode). forge-ui shows it as the attempt's conversation. The final message, the token counts and the infrastructure checks are read from these streams: opencode now reports its tokens, its answer is the text of its last step, and an opencode `error` event that no answer followed (an expired token, a refused route) is classified like codex's. antigravity keeps its plain output until its stream has been seen on a working login.
