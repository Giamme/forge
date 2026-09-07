## Optional Ripwire context

Forge automatically uses Ripwire **0.4.0** when found on `PATH`, then in
`~/.local/bin`. Other versions are preserved but skipped until compatibility is verified.
All five harnesses use the same preparation step. No provider calls are needed for it.

Use `--no-ripwire` on dispatch, solo, or parallel `plan`, `run`, and `retry`, or set
`export FORGE_RIPWIRE=off` in your shell configuration. Opt-out is resolved before
probing or offering installation. Parallel plans remember opt-out in `no_ripwire`
for later resumes and retries; remove that marker to enable it again.

Forge setup and solo/parallel run entrypoints offer installation once before agents
start. The helper shows v0.4.0 and `~/.local/bin/ripwire`, then asks
“Ripwire is missing. Install it now? [Y/n]”. Enter accepts; no or EOF continues.
It reads the controlling terminal separately from task stdin. Without a terminal it
prints the helper command and continues. Dispatch itself never prompts, so background
workers cannot wait for installation input.

```bash
bash <skill_dir>/scripts/forge-install-ripwire.sh           # offer installation
bash <skill_dir>/scripts/forge-install-ripwire.sh --dry-run # preview, no downloads
bash <skill_dir>/scripts/forge-install-ripwire.sh --yes     # explicit unattended consent
```

The helper pins the [upstream v0.4.0 installer](https://github.com/redhat-et/ripwire/blob/v0.4.0/scripts/install.sh)
and its SHA-256, retains upstream archive checksum and binary version checks, and sets
`RIPWIRE_NO_ACTIVATE=1`. Existing installations are preserved. Failed installation
reports its cause; Forge runners continue without requiring Ripwire.

Planner/dwarf use `--for` in their own repository or worktree. Runners pass the
requirements via `--ripwire-query-file`; direct dispatch defaults to the original
prompt. Only this search excerpt is limited to 2,048 characters. QA uses
`--pr-context=<review-base>` against the disposable review snapshot; direct QA without
a baseline uses task context. See the [pinned command reference](https://github.com/redhat-et/ripwire/blob/v0.4.0/docs/COMMANDS.md).

The full prompt remains intact, followed by one marked advisory section. Requirements,
ownership, retry findings and the actual diff remain authoritative; suggested tests
supplement required verification. Each attempt prepares fresh context with `--no-cache`,
excluding `.forge` and Git internal paths. Temporary preparation files live with the
external attempt artifacts. Preparation has a shared 30-second deadline, a 4,096 estimated
token budget and a 16 KiB subprocess output ceiling. Failed, empty, oversized, incompatible
or timed-out results are skipped with a reason; timeout cleanup kills the process group.

Each `attempts/<role>-*/ripwire.json` records the command, binary version, repository,
baseline, duration, status and delivered section (or skip reason). Dry runs and preflight
skip preparation. Doctor reports availability without installation. Native Codex review
skips context because its dispatch cannot accept an additional prompt.
