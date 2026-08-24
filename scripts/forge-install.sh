#!/usr/bin/env bash
# forge-install.sh — make /forge reachable from codex, claude, openclaude and opencode.
#
# This skill directory stays the single source of truth.
#
#   claude / openclaude / codex  symlink into their skills dir — edits are live
#   opencode                     nothing to do; it scans ~/.claude/ for skills itself
#   antigravity                  plugin install, which COPIES — see below
#
# Codex reads ~/.codex/skills, not ~/.codex/prompts: a prompts entry gives you a
# slash command, but not the skill, and it is not where any other skill here lives.
#
#   forge-install.sh [--dry-run]
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
do_() { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }

link_dir() { # link_dir <target-parent> <label>
  local parent="$1" label="$2" dest="$1/forge"
  if [ ! -d "$parent" ]; then say "$label: skipped ($parent does not exist)"; return; fi
  if [ "$dest" = "$SKILL_DIR" ]; then say "$label: is the source directory"; return; fi
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$SKILL_DIR" ]; then
    say "$label: already linked"; return
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    # Never clobber a real directory the user may have edited in place.
    say "$label: SKIPPED — $dest exists and is not a symlink; move it aside first"; return
  fi
  do_ ln -sfn "$SKILL_DIR" "$dest" && say "$label: linked -> $dest"
}

# opencode needs no install at all: it scans ~/.claude/ for skills by default
# (disabled only by OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1), so it picks this
# directory up directly. Verify with `opencode debug skill`.
report_opencode() {
  if ! command -v opencode >/dev/null 2>&1; then
    say "opencode  : skipped (not on PATH)"; return
  fi
  say "opencode  : auto-detected from ~/.claude/skills (no install needed)"
}

# Antigravity loads skills only as part of a plugin — a loose directory under
# ~/.gemini/skills is ignored (agy cannot see skills placed there at all). So forge
# is wrapped in a minimal plugin whose skills/ entry points back at this directory.
#
# Note that `agy plugin install` COPIES the files into ~/.gemini/config/plugins
# rather than referencing them. Antigravity is therefore the one harness that does
# not track edits to this directory automatically: re-run this installer after
# changing the skill, or agy keeps running the version it copied.
install_antigravity() {
  local label="antigravity" wrapper="$HOME/.gemini/forge-plugin"
  if ! command -v agy >/dev/null 2>&1; then
    say "$label: skipped (agy not on PATH)"; return
  fi
  if [ "$DRY" = 1 ]; then
    say "  would: write plugin wrapper $wrapper"
    say "  would: agy plugin install $wrapper"
    say "$label: plugin -> $wrapper"; return
  fi
  mkdir -p "$wrapper/skills"
  cat > "$wrapper/plugin.json" <<EOF
{
  "name": "forge",
  "description": "Dispatch a coding task to a dwarf model to implement, then an independent qa model to review its actual diff, across codex/claude/openclaude/opencode/antigravity.",
  "version": "1.0.0",
  "author": { "name": "forge" }
}
EOF
  ln -sfn "$SKILL_DIR" "$wrapper/skills/forge"
  if agy plugin install "$wrapper" >/dev/null 2>&1; then
    say "$label: plugin installed (copied — re-run this script after editing the skill)"
  else
    say "$label: FAILED — try: agy plugin install $wrapper"
  fi
}

say "forge source: $SKILL_DIR"
[ "$DRY" = 1 ] && say "(dry run)"

link_dir "$HOME/.claude/skills"     "claude    "
link_dir "$HOME/.openclaude/skills" "openclaude"
link_dir "$HOME/.codex/skills"      "codex     "
install_antigravity
report_opencode

say
say "Verify with: bash $SKILL_DIR/scripts/forge-dispatch.sh doctor"
