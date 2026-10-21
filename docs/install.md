# Install and update

[← README](../README.md) · [All docs](README.md)

Install forge from npm or a git checkout, check which agent CLIs it can reach, and keep every harness installation current.

## Requirements

forge runs on macOS and Linux. It needs bash, git and Python 3.9 or later, plus the agent
CLIs you route models through; the doctor below shows which ones it finds. Installing from
npm brings none of these with it.

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
