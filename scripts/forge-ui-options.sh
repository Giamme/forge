#!/usr/bin/env bash
# Source in runners (forge-solo.sh, forge-parallel.sh plan and run). Compatible with macOS Bash 3.2.
#
# forge-ui is opt-in per run: the orchestrator asks the user and passes the answer as
# --ui. The flag is never persisted, and launching never decides whether the run goes on.
# Registering a run (forge_ui_register) is not part of that choice: it happens on every
# run, so forge-ui can later show all of a repo's runs, including those never opened in it.
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

# forge_ui_register DIR — note DIR in forge's run registry ($XDG_CONFIG_HOME/forge/runs),
# under the repo named in DIR/repo. Called on every run, whatever --ui and FORGE_UI say,
# because forge-ui's view of a repo's runs reads that registry. Always returns 0 and prints
# nothing: the registry can be unwritable or the repo file odd, and neither may change a
# run's output or exit code. -I keeps a PYTHON* variable or a stray module in the current
# directory from changing what runs. FORGE_RUN_REGISTRY=off turns it off.
forge_ui_register() {
  python3 -I "$SKILL_DIR/scripts/forge_registry.py" register "$1" </dev/null >/dev/null 2>&1 || true
  return 0
}
