# Ripwire and Jev

[← README](../README.md) · [All docs](README.md)

Two optional add-ons: Ripwire adds repository evidence to every prompt, and Jev gives advisory judgments. forge works without either.

## Ripwire context

Forge automatically adds optional Ripwire 0.4.0 repository evidence to planner, dwarf,
and QA prompts across all six harnesses. Setup and interactive runners offer a pinned,
verified installation; headless runs continue when it is missing. Use `--no-ripwire`
or `FORGE_RIPWIRE=off` to disable it. Decomposed plans preserve opt-out across resumes
and retries. See [installation, controls and attempt artifacts](../references/ripwire.md).

## Jev

Jev is TypeSafe's "System One" model. It answers narrow, typed questions over the state it is
given, quickly and with calibrated probabilities. forge can ask it for advice on routing, test
selection, gates and memory curation. That advice sits beside forge's own decisions and never
replaces a model choice, a test run, a review or verification.

It is **off by default**. Each enabled capability sends a narrow slice of the run to an external
API: task prompts, diff hunks, test names or memory text, and never `.forge/` or `.git`. The
installer offers setup and declines by default.

```sh
forge jev setup                        # store an API key
forge jev status                       # resolved config and enabled capabilities
forge jev enable [--capability NAME]   # turn it on, optionally for one capability
```

Chosen sites can go to Cloudflare's hosted Clef instead (`forge jev setup --provider cloudflare`).
The [full Jev reference](../references/jev.md) covers capabilities, privacy and backtests.
