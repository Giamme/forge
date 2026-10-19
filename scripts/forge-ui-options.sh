#!/usr/bin/env bash
# Source in runners (forge-solo.sh, forge-parallel.sh plan). Compatible with macOS Bash 3.2.
#
# forge-ui is opt-in per run: the orchestrator asks the user and passes the answer as
# --ui. The flag is never persisted, and launching never decides whether the run goes on.
FORGE_UI_WANTED=0

# forge_ui_flag ARG — returns 0 and records the choice for --ui / --no-ui, 1 for anything else.
forge_ui_flag() {
  case "${1:-}" in
    --ui)    FORGE_UI_WANTED=1 ;;
    --no-ui) FORGE_UI_WANTED=0 ;;
    *)       return 1 ;;
  esac
}

# forge_ui_launch DIR — open DIR in the shared forge-ui server when --ui was given.
# Always returns 0: a UI that cannot start must not change the run's exit code. The
# launcher's stdin is /dev/null so it can never wait for a terminal.
forge_ui_launch() {
  [ "${FORGE_UI_WANTED:-0}" = 1 ] || return 0
  if [ "${FORGE_UI:-}" = off ]; then
    printf 'forge: FORGE_UI=off; not starting forge-ui\n' >&2
    return 0
  fi
  python3 "$SKILL_DIR/scripts/forge-ui.py" open "$1" </dev/null \
    || printf 'forge: forge-ui did not start; the run goes on (later: %s/forge ui open %s)\n' "$SKILL_DIR" "$1" >&2
  return 0
}
