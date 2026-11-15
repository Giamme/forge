---
type: changed
breaking: false
audience: user
commits: 
---
The bundled model registry now follows the latest releases. `terra` runs GPT-6.1 Terra. `gemini` and `gemini-flash` run Gemini 3.8 Flash on antigravity, and `gemini-pro` runs Gemini 4 Argon, which replaces Gemini 3.1 Pro (still available as `gemini-3.1`); new `argon` and `carbon` aliases run Gemini 4 Argon and Gemini 4 Carbon. `opus` and `sonnet` on antigravity move to Claude Opus 5.5 and Sonnet 5.5, since Google removed the 4.6 models and `gpt-oss` on November 2 (the `gpt-oss` alias is gone). `grok` on opencode runs Grok 4.7, and `glm` runs GLM-5.3 (`glm-flash` for 5.3 Flash, `glm-5.2` to keep the old one). New `kimi-k3.1` and `qwen-flash` aliases run Kimi K3.1 Pro and Qwen3.8 Flash through opencode Go.
