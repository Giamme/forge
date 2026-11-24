---
type: changed
breaking: true
audience: user
commits: 
---
An alias now names the same model on every harness. Rows that used to give a different model under the same name have aliases of their own: Antigravity's Claude models are `opus-4.6` and `sonnet-4.6`, GitHub Copilot's older Fable and Grok on opencode are `fable-5.1` and `grok-4.6`, and Gemini Pro on opencode is `gemini-pro:...:opencode`. `opus::antigravity` or `gemini::opencode` no longer quietly runs another model: forge passes the name through as a literal id and its note names the alias that harness serves. Dispatches recorded under the old rows are counted under the new aliases in review statistics and forge-ui's leaderboard.
