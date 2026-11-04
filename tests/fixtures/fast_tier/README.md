Real harness output for the fast-tier tests (#68), recorded 2026-10-30 with one-word prompts and trimmed to the keys forge reads.

- `claude-no-flag.jsonl`: `claude -p --output-format stream-json --verbose` without `--fast-mode` (Claude Code 2.1.314, opus): fast mode off, `sdk_opt_in_required`, `usage.speed` standard.
- `claude-fast-flag-extra-usage-off.jsonl`: the same with `--fast-mode on` on an account whose extra usage is off: still standard speed, `extra_usage_disabled`. The CLI exits 0.
- `codex-fast.jsonl`: `codex exec --json -c service_tier=fast` (codex CLI 0.164.0); the session rollout recorded `service_tier: priority`, the stream says nothing about it.
- `codex-unsupported-tier.jsonl`: a tier the model does not advertise (`-c service_tier=bogus`): an `error` item, then the run continues at the default tier and exits 0.
