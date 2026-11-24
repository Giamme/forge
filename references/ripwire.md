## Optional Ripwire context

Forge automatically uses Ripwire **0.6 or later** when found on `PATH`, then in
`~/.local/bin`. An older binary is skipped with `incompatible binary ... (forge needs Ripwire
0.6 or later: forge ripwire update)`. Every flag forge passes (`--no-cache`,
`--token-budget=`, `--exclude=`, `--for=`, `--pr-context=`) exists since 0.6.0.
All six harnesses use the same preparation step. No provider calls are needed for it.

Use `--no-ripwire` on dispatch, solo, or parallel `plan`, `run`, and `retry`, or set
`export FORGE_RIPWIRE=off` in your shell configuration. Opt-out is resolved before
probing or offering installation. Parallel plans remember opt-out in `no_ripwire`
for later resumes and retries; remove that marker to enable it again.

Forge setup and solo/parallel run entrypoints offer installation once before agents
start. The helper shows the release it would install and `~/.local/bin/ripwire`, then asks
“Ripwire is missing. Install it now? [Y/n]”. Enter accepts; no or EOF continues.
It reads the controlling terminal separately from task stdin. Without a terminal it
prints the helper command and continues. Dispatch itself never prompts, so background
workers cannot wait for installation input.

```bash
forge ripwire install                  # offer installation of the latest release
forge ripwire install --version v0.6.10  # a specific release instead
forge ripwire update                   # replace the Ripwire forge installed with the latest release
forge ripwire status [--check]         # installed vs latest (--check asks GitHub now)
bash <skill_dir>/scripts/forge-install-ripwire.sh --dry-run  # preview, no downloads
bash <skill_dir>/scripts/forge-install-ripwire.sh --yes      # explicit unattended consent
```

The helper resolves the latest release through the GitHub releases API
(`FORGE_RIPWIRE_VERSION` or `--version TAG` pins one; Intel Macs stop at v0.6.1, the last
release upstream built for them), downloads the upstream `scripts/install.sh` **at that tag**
over HTTPS and runs it with `RIPWIRE_NO_ACTIVATE=1`. What is verified: the upstream installer
refuses a release without a checksum, checks the archive's SHA-256, and runs the binary's
`--version` before and after installing it; forge then checks the result is 0.6 or later. What
is not: the installer script's own bytes. A moving version cannot be pinned by hash, so forge
only checks that the download is upstream's release installer before running it.

Without `update`, an existing installation is always preserved. `update` replaces only
`~/.local/bin/ripwire`, the binary forge installs, and only when that is the Ripwire on
`PATH`. A Ripwire installed anywhere else (Homebrew, a source build, a symlink) is reported
and never replaced. Failed installation reports its cause; Forge runners continue without
requiring Ripwire.

**Update notice.** When a solo run, `plan` or `run` ends, forge checks at most once a day
whether a newer Ripwire release exists (cached in
`${XDG_STATE_HOME:-~/.local/state}/forge/ripwire-check.json`; offline keeps the last known
release). When one does and forge installed yours, a terminal gets “Ripwire X is available
(you have Y). Update now? [Y/n]”, and a yes runs the update. Without a terminal it prints one
line with `forge ripwire update` instead. It shows at most once a day, never in CI, never with
`FORGE_RIPWIRE=off` or `FORGE_UPDATE_CHECK=off`, a network failure is silent, and it never
changes the run's exit code. `forge update` stays quiet about Ripwire.

Planner/dwarf use `--for` in their own repository or worktree. Runners pass the
requirements via `--ripwire-query-file`; direct dispatch defaults to the original
prompt. Only this search excerpt is limited to 2,048 characters. QA uses
`--pr-context=<review-base>` against the disposable review snapshot; direct QA without
a baseline uses task context. See the [command reference](https://github.com/redhat-et/ripwire/blob/main/docs/COMMANDS.md).

The full prompt remains intact, followed by one marked advisory section. Requirements,
ownership, retry findings and the actual diff remain authoritative; suggested tests
supplement required verification. Each attempt prepares fresh context with `--no-cache`,
excluding `.forge` and Git internal paths. Temporary preparation files live with the
external attempt artifacts. Preparation has a shared 30-second deadline, a 4,096 estimated
token budget and a 16 KiB subprocess output ceiling. Failed, empty, oversized, incompatible
or timed-out results are skipped with a reason; timeout cleanup kills the process group.

Each `attempts/<role>-*/ripwire.json` records the command, binary version, repository,
baseline, duration, status and delivered section (or skip reason). Dry runs and preflight
skip preparation. Doctor reports the installed version, whether forge can use it and the
newest release last seen, without installing or asking GitHub. Native Codex review
skips context because its dispatch cannot accept an additional prompt.
