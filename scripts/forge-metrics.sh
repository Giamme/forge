#!/usr/bin/env bash
# Each runner owns its metric state in a process/subshell, including failure exits.
forge_metric_begin() {
  FORGE_METRIC_ROOT="$1"; FORGE_METRIC_ROLE="$2"
  FORGE_METRIC="$(python3 "$SKILL_DIR/scripts/forge-runtime.py" begin "$1" "$2")" || return 3
  trap 'metric_rc=$?; python3 "$SKILL_DIR/scripts/forge-runtime.py" finish "$FORGE_METRIC" "$FORGE_METRIC_ROOT" "$FORGE_METRIC_ROLE" pipeline "$metric_rc"; exit "$metric_rc"' EXIT
}
forge_metric_phase() {
  python3 "$SKILL_DIR/scripts/forge-runtime.py" phase "$FORGE_METRIC" "$1"
}
