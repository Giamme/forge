#!/usr/bin/env bash
# forge-install.sh — make /forge reachable from codex, claude, openclaude, opencode, antigravity and grok.
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

SKILL_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$SKILL_DIR/scripts/forge-lib-env.sh"
forge_ensure_python3
RESULT=0
DRY=0
# `forge install --help` must never install; nor may a mistyped --dry-run.
case "${1:-}" in
  '') ;;
  --dry-run) DRY=1 ;;
  -h|--help) echo 'usage: forge install [--dry-run]'; exit 0 ;;
  *) echo "forge: unknown install option: $1" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }
do_() { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }

link_dir() { # link_dir <target-parent> <label>
  local parent="$1" label="$2" dest="$1/forge" target=""
  if [ ! -d "$parent" ]; then say "$label: skipped ($parent does not exist)"; return; fi
  if [ "$dest" = "$SKILL_DIR" ]; then say "$label: is the source directory"; return; fi
  if [ -L "$dest" ]; then
    # Canonicalize physically so /tmp mounts, 8.3 short names, and relative symlinks match SKILL_DIR.
    target="$(cd -P -- "$dest" 2>/dev/null && pwd -P || true)"
    if [ -n "$target" ] && [ "$target" = "$SKILL_DIR" ]; then
      say "$label: already linked"; return
    fi
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    # Never clobber a real directory the user may have edited in place.
    say "$label: SKIPPED — $dest exists and is not a symlink; move it aside first"; return 1
  fi
  if forge_is_windows; then
    if [ "$DRY" = 1 ]; then
      say "  would: mklink /J $(forge_win_path "$dest") $(forge_win_path "$SKILL_DIR")"
      say "$label: linked -> $dest"
      return 0
    fi
    if forge_link_dir "$SKILL_DIR" "$dest"; then
      say "$label: linked (junction) -> $dest"
    else
      cp -R "$SKILL_DIR" "$dest" && say "$label: copied -> $dest (re-run forge install after editing)"
    fi
    return $?
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

copy_skill_dir() { # copy_skill_dir <target-parent> <label> — real directory copy for scanners that ignore junctions
  local parent="$1" label="$2" dest="$1/forge" item stage
  if [ ! -d "$parent" ]; then say "$label: skipped ($parent does not exist)"; return; fi
  if [ "$dest" = "$SKILL_DIR" ]; then say "$label: is the source directory"; return; fi
  if [ "$DRY" = 1 ]; then
    say "  would: copy skill files to $dest"
    say "$label: copied -> $dest (re-run forge install after editing)"
    return 0
  fi
  if [ -L "$dest" ]; then
    rmdir "$dest" 2>/dev/null || rm -f "$dest" 2>/dev/null || rm -rf "$dest"
  elif [ -e "$dest" ] && [ ! -f "$dest/.forge-installed" ]; then
    say "$label: SKIPPED — $dest exists and is not managed by forge; move it aside first"; return 1
  fi
  stage="$dest.tmp.$$"
  rm -rf "$stage"
  mkdir -p "$stage" || return 1
  for item in SKILL.md VERSION LICENSE package.json forge forge.cmd bin scripts references ui; do
    if [ -e "$SKILL_DIR/$item" ]; then
      cp -R "$SKILL_DIR/$item" "$stage/$item" || { rm -rf "$stage"; return 1; }
    fi
  done
  find "$stage" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  : > "$stage/.forge-installed"
  rm -rf "$dest"
  mv "$stage" "$dest" || { rm -rf "$stage"; return 1; }
  say "$label: copied -> $dest (re-run forge install after editing)"
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
  local label="antigravity" gemini_home="$HOME/.gemini" wrapper agy_skills has_agy=0 rc=0
  if forge_is_windows && [ ! -d "$gemini_home" ] && [ -n "${USERPROFILE:-}" ]; then
    gemini_home="$(forge_posix_path "$USERPROFILE")/.gemini"
  fi
  wrapper="$gemini_home/forge-plugin"
  agy_skills="$gemini_home/config/skills"
  if command -v agy >/dev/null 2>&1; then
    has_agy=1
  fi
  if forge_is_windows && [ -d "$agy_skills" ]; then
    # Windows only. Electron/Node scanners check dirent.isDirectory(), which is false for Windows
    # junctions, and Antigravity loads skills directly from ~/.gemini/config/skills without
    # `agy plugin install` (which hits Access is denied on Windows while Antigravity.exe watches
    # ~/.gemini/config/plugins). macOS and Linux keep the plugin below.
    copy_skill_dir "$agy_skills" "antigravity" || rc=1
    return "$rc"
  fi
  if [ "$has_agy" = 0 ]; then
    say "$label: skipped (agy not on PATH)"; return
  fi
  if [ "$DRY" = 1 ]; then
    say "  would: write plugin wrapper $wrapper"
    say "  would: agy plugin install $wrapper"
    say "$label: plugin -> $wrapper"; return "$rc"
  fi
  mkdir -p "$wrapper/skills" || return 1
  # The plugin's version is forge's own (VERSION), not a fixed placeholder.
  local version=""
  read -r version < "$SKILL_DIR/VERSION" 2>/dev/null || true
  [ -n "$version" ] || version="0"
  cat > "$wrapper/plugin.json" <<EOF
{
  "name": "forge",
  "description": "Dispatch a coding task to a dwarf model to implement, then an independent qa model to review its actual diff, across codex/claude/openclaude/opencode/antigravity/grok.",
  "version": "$version",
  "author": { "name": "forge" }
}
EOF
  [ "$?" = 0 ] || return 1
  local agy_wrapper
  if forge_is_windows; then
    # `agy plugin install` on Windows rejects junctions/symlinks inside the plugin bundle.
    copy_skill_dir "$wrapper/skills" "antigravity" >/dev/null || return 1
    agy_wrapper="$(forge_win_path "$wrapper")"
  else
    forge_link_dir "$SKILL_DIR" "$wrapper/skills/forge" || return 1
    agy_wrapper="$wrapper"
  fi
  if agy plugin install "$agy_wrapper"; then
    say "$label: plugin installed (copied — re-run this script after editing the skill)"
  else
    say "$label: FAILED — try: agy plugin install $wrapper"
    return 1
  fi
  return "$rc"
}

say "forge source: $SKILL_DIR"
[ "$DRY" = 1 ] && say "(dry run)"

link_dir "$HOME/.claude/skills"     "claude    " || RESULT=1
link_dir "$HOME/.openclaude/skills" "openclaude" || RESULT=1
link_dir "$HOME/.codex/skills"      "codex     " || RESULT=1
install_antigravity || RESULT=1
report_opencode

say
say "Verify with: bash $SKILL_DIR/scripts/forge-dispatch.sh doctor"

# What a first run needs (models, reviews, forge-ui, memory, Ripwire, decision models),
# asked once on a terminal and saved as defaults (#162). `forge update` re-runs this installer with
# FORGE_SETUP=off: an update never asks again. Setup failing never fails the install.
if [ "$DRY" = 1 ]; then
  say "  would: run forge setup on a terminal (models, reviews, forge-ui, memory, Ripwire; skipped once done)"
elif [ "${FORGE_SETUP:-}" = off ]; then
  :
elif [ -t 0 ] && [ -t 1 ]; then
  say
  python3 "$SKILL_DIR/scripts/forge-setup.py" --after-install || true
elif ! python3 "$SKILL_DIR/scripts/forge_prefs.py" done 2>/dev/null; then
  say "Next: forge setup (models, reviews and add-ons, asked once), or forge setup --defaults"
fi

exit "$RESULT"
