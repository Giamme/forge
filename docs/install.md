# Install and update

[← README](../README.md) · [All docs](README.md)

Install forge from npm or a git checkout, check which agent CLIs it can reach, and keep every harness installation current.

## Requirements

forge runs on macOS, Linux, and Windows. It needs bash, git and Python 3.9 or later, plus the
agent CLIs you route models through; the doctor below shows which ones it finds. Installing from
npm brings none of these with it.

### Windows

On Windows, forge runs natively through Git Bash (`bin/forge.js` for npm, `forge.cmd` for CMD or
PowerShell) and standard Windows Python 3.9+ (no WSL or MSYS2 Python install is required).
This is native mode, the default. MSYS2 mode, opt-in, runs forge inside MSYS2 instead:
see [Windows: MSYS2 mode](#windows-msys2-mode).

- **Prerequisites:** [Git for Windows](https://gitforwindows.org/) (provides `git` and Git Bash)
  and native Python 3.9 or later on `PATH` (or discoverable via `py -3`).
- **`FORGE_BASH`:** If Git Bash lives in a non-standard location, set `FORGE_BASH` to the full
  path of your `bash.exe`; forge ignores Windows `System32\bash.exe` / `WindowsApps` WSL stubs.
- **`python3` shim:** When `python3` resolves to the Microsoft Store alias, forge writes a
  per-user wrapper in `%LOCALAPPDATA%\forge\bin` (`python3` and `python3.cmd`) pointing at your
  installed `python` or `py -3`.
- **Known platform differences:**
  - Skill links under `.claude`, `.codex`, and `.openclaude` use NTFS directory junctions
    (`mklink /J`), which do not require Administrator privileges or Developer Mode; Antigravity still
    receives a copied directory, so re-run `forge install` / `forge update` after pulling changes.
  - Process-tree termination (timeouts, Ctrl-C, orphan reaping) uses Win32 Job Objects and
    `TerminateProcess` because Windows lacks POSIX `SIGTERM`/`os.killpg`, so background helpers are
    ended forcefully rather than via a graceful shutdown signal. Because `ProcessJob.attach` runs
    right after `Popen`, a grandchild spawned in the first few milliseconds can escape the job
    (cleanup is best-effort), and killing the scheduler abruptly ends running tasks attached to its
    Job Object rather than leaving them in separate sessions as on macOS/Linux.
  - `O_NOFOLLOW` / `O_DIRECTORY` symlink checks fall back to `os.lstat` (reparse-point) checks
    before open, which is best-effort rather than kernel-atomic, and POSIX `chmod` permission bits
    are not enforced natively on Windows.

## From npm

```bash
npm install -g @giamme/forge
forge install
```

`forge install` links the installed package into each harness, as the installer does for a
checkout below. It is a step of its own rather than an npm install script, so it works where
scripts are turned off (`--ignore-scripts`). To update:

```bash
npm install -g @giamme/forge@latest && forge install
```

The links point into npm's global package directory, so they keep working across updates;
`forge install` refreshes antigravity's copied plugin. Under nvm, that directory belongs to
one Node version: after switching Node, install forge on the new one and run `forge install`
again.

> **Foundry users:** Foundry's `forge` has the same name, and whichever directory comes first
> on your `PATH` wins; npm's global bin usually comes first. Runs start from your harness
> (`/forge`), so this command is only for `forge install` and the optional tools. If you keep
> Foundry's `forge` first, run ours as `"$(npm root -g)/@giamme/forge/forge"`.

## From a git checkout

```bash
git clone https://github.com/Giamme/forge.git ~/.claude/skills/forge
bash ~/.claude/skills/forge/scripts/forge-install.sh
```

Clone anywhere you like — the installer works out its own location and links from there.
`~/.claude/skills/forge` is just the tidiest home: claude reads that path directly, and
opencode and grok scan it on their own.

The installer also offers optional Fractal support in an interactive terminal. Declining
or a Fractal installation failure does not prevent ordinary Forge setup. See
[Fractal installation](fractal.md#install-the-optional-runtime) for prerequisites and a separate install.

## Windows: MSYS2 mode

On Windows forge has two modes. Native mode, the default, runs it in Git Bash with Windows' own
Python. MSYS2 mode runs it inside [MSYS2](https://www.msys2.org/) instead, where bash, git,
python3 and ps agree on process IDs, process groups, signals, `flock` and symlinks as they do on
Linux. MSYS2 mode is opt-in:

- Set `FORGE_WIN_MODE=msys2` where forge starts, and start forge from Git Bash as usual: each
  entry script re-runs itself inside MSYS2, and the agent CLIs still get the `PATH` they had in
  Git Bash. For Claude Code, put the variable under `"env"` in `~/.claude/settings.json`; for a
  terminal, in your shell profile.
- Or start forge from an MSYS2 shell. There it always runs in MSYS2 mode.

`FORGE_WIN_MODE` is `native` (the default) or `msys2`; any other value stops forge with an error.

To set MSYS2 mode up:

1. Install MSYS2 in `C:\msys64`, or set `FORGE_MSYS2_ROOT` to where it is. In an MSYS2 shell:

   ```bash
   pacman -Syu
   pacman -S python git procps-ng diffutils
   ```

2. Use Git 2.48 or newer, both MSYS2's and the Git for Windows the agent CLIs use. forge's
   worktrees link back to your repository with relative paths, which marks the repository as
   needing Git 2.48 (`extensions.relativeWorktrees` in its config): older Git, and libgit2- or
   JGit-based tools such as some IDEs, then refuse to open it. forge checks both versions and
   stops with a message when one is older.
3. Keep a native Windows Python on the Git Bash `PATH` (`python3`, `python` or `py`): the
   hand-over to MSYS2 goes through it, because two MSYS runtimes cannot pass their environment
   to each other directly.
4. Turn on Developer Mode in Windows settings, so MSYS2 can create real symlinks.
5. Clone forge where Claude Code reads skills:

   ```bash
   git clone https://github.com/Giamme/forge.git ~/.claude/skills/forge
   ```

   The npm package is macOS and Linux only, and `scripts/forge-install.sh` (links into the
   other harnesses, optional Fractal) has not been tried in MSYS2 mode.

What differs in MSYS2 mode:

- File modes are not enforced: MSYS2 mounts Windows drives with `noacl`, so files forge writes
  as 0600 (provider keys, settings) are as private as your user profile, no more.
- A script task's `tasks/<id>/run` counts as executable when it starts with `#!`.
- `forge watch` raises no desktop notification, and Ripwire has no Windows build.
- `stop` recognises a harness by its start time, which Cygwin gives as a time of day for the
  first 24 hours and as a date after that: a harness that has run for more than a day is left
  running.

If MSYS2 programs fail at random to start (`0xC0000142`, or `fork: Resource temporarily
unavailable`), look at Windows' Exploit protection. Cygwin's `fork` rebuilds the parent's memory
layout in the child, and forced ASLR gets in its way. First exempt MSYS2's own programs: in
Windows Security, App & browser control, Exploit protection, Program settings, add the programs
in `C:\msys64\usr\bin` and turn off "Force randomization for images (Mandatory ASLR)" and
"Randomize memory allocations (Bottom-up ASLR)" for them. Change those settings system-wide only
as a last resort: that weakens every program on the machine.

## Check what forge can reach

`<forge>` is your checkout, or `"$(npm root -g)/@giamme/forge"` for an npm install:

```bash
bash <forge>/scripts/forge-dispatch.sh doctor
```

```
HARNESS      STATUS     DETAIL
codex        found      /Users/you/.nvm/versions/node/v24.18.0/bin/codex
claude       found      /Users/you/.local/bin/claude
openclaude   found      /Users/you/.nvm/versions/node/v24.18.0/bin/openclaude
opencode     found      /Users/you/.opencode/bin/opencode
antigravity  MISSING    agy not on PATH
grok         found      /Users/you/.grok/bin/grok
```

A `MISSING` harness is fine — forge only needs the ones you actually route models through.

**Binary presence is not reachability.** A harness can be installed and still fail on auth or
billing (opencode in particular fails this way), and that failure only surfaces mid-dispatch.
To prove a pairing end to end before spending a real run:

```bash
bash <forge>/scripts/forge-dispatch.sh dwarf <spec> --prompt-file /dev/stdin --repo <scratch dir> \
    <<< 'Reply with exactly: FORGE_OK'
```

## What the installer does per harness

Each CLI discovers skills differently, and the differences are not guessable — so verify
with the listed check rather than trusting file placement.

| Harness | Mechanism | Verify with |
|---|---|---|
| claude | `~/.claude/skills/forge/` | `/forge` appears in the skill list |
| openclaude | symlink into `~/.openclaude/skills/` | `openclaude skills list --json` |
| codex | symlink into **`~/.codex/skills/`** | `codex exec "do you have a skill named forge?"` |
| opencode | **nothing** — it scans `~/.claude/` itself | `opencode debug skill` |
| antigravity | `agy plugin install` of a generated wrapper | `agy --print='/forge'` |

> **The one ongoing caveat:** `agy plugin install` *copies* the files instead of referencing
> them. The other four track the source directory live, so an edit reaches them instantly —
> antigravity keeps running whatever it copied. **Re-run `forge-install.sh` after any edit**,
> or agy silently runs a stale version.

`forge-install.sh --dry-run` prints what it would do and touches nothing. It never clobbers
a real directory that isn't a symlink.

## Updating all harness installations

Run the updater from your Forge checkout (or through one of its skill symlinks):

```bash
./forge update            # same as: bash ~/.claude/skills/forge/scripts/forge-update.sh
./forge version           # VERSION, checkout commit, tracked upstream, last known release
./forge version --check   # ask the upstream for its newest release now
```

It fetches the current branch's configured upstream, fast-forwards the checkout, and
runs Forge's installer to refresh Claude, OpenClaude and Codex links, OpenCode discovery,
and Antigravity's copied plugin. Harnesses that are not present are skipped. Restart
active harness sessions afterward to load the updated skill.

```bash
# Preview without fetching or changing files:
bash <forge-checkout>/scripts/forge-update.sh --dry-run

# Refresh harness installations after local edits, without fetching:
bash <forge-checkout>/scripts/forge-update.sh --local

# Point a copied installation's updater at the original checkout:
bash <path>/scripts/forge-update.sh --source <forge-checkout>
```

When a solo run, `plan` or `run` finishes, forge checks the checkout's upstream for a newer
release tag at most once a day (`git ls-remote`, 5-second timeout, cached in
`${XDG_STATE_HOME:-~/.local/state}/forge/update-check.json`) and, if there is one, prints a
single line on stderr: `forge 0.8 is available (you have 0.7.1): forge update`. The check is
silent offline, in a copied installation without Git, in CI (`CI` set) and with
`FORGE_UPDATE_CHECK=off`, and it never changes a run's outcome or exit code.

An npm installation has no Git upstream, so there the same daily check asks the npm registry
for the package's `latest` (`npm_config_registry` when you set one, as npm does), and the line
names npm's command instead: `forge 0.9.0 is available (you have 0.8): npm install -g
@giamme/forge@latest && forge install`. `forge version` shows the same command.

The default update requires a clean Git checkout and a branch with an upstream. Local
changes, detached HEAD, fetch failures and diverged branches stop the update without
resetting or stashing your work. `--local` deliberately allows current local edits.
Real skill directories are preserved; a conflicting directory or plugin installation
failure produces a nonzero exit and an explanation. After fixing an installation
failure, use `--local` to retry from the updated source. Updating Forge does not prompt
for optional Ripwire installation.

An npm installation is not a Git checkout, so the updater stops there and prints the npm
command [above](#from-npm) instead. `--local` still refreshes its harness installations.

## `scripts/forge-install.sh`

```
forge-install.sh [--dry-run]
```

Links this directory into every installed harness. Skips harnesses that aren't present,
skips a destination that exists and isn't a symlink (it will never clobber a directory you
may have edited in place), and reports opencode as auto-detected.
