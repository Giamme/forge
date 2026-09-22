#!/usr/bin/env bash
# Source in runners. Arrays and syntax remain compatible with macOS Bash 3.2.
FRACTAL_CHOICE=""; FRACTAL_ARGS=()
forge_fractal_help() {
  cat <<'HELP'
Optional Fractal execution (solo or parallel run):
  --fractal | --no-fractal   Explicit choice; unattended defaults off
  --fractal-auto-decompose  Enable Fractal with automatic split-or-atomic planning
  --fractal-planner SPEC    Decision model (top-level planner, then task root model)
  --fractal-depth N         Nested levels below implementation (2)
  --fractal-children N      Unsettled direct children per node (3)
  --fractal-nodes N         Lifetime nodes per task (12)
  --fractal-iterations N    Iterations per node per attempt (6)
  --fractal-concurrency N   Model slots across the run, including QA (3)
  --fractal-deadline N      Task attempt seconds excluding pauses (2700)
  --fractal-max-cost N      Unsupported: costs are observational
Run choice, planners, limits and pools persist for resume/retry. Installation never activates it.
Solo child routing: --dwarf-low, --dwarf-medium, --dwarf-high (comma pools).
Decomposed routing is configured by the plan command.
Use ./forge fractal --help for installation, inspection, controls and HTML reports.
HELP
}
forge_fractal_flag() {
  local choice="$1"
  [ -z "$FRACTAL_CHOICE" ] || [ "$FRACTAL_CHOICE" = "$choice" ] || { echo '--fractal and --no-fractal are mutually exclusive' >&2; return 2; }
  FRACTAL_CHOICE="$choice"
}
forge_fractal_select() {
  local run="$1" repo="$2" mode="$3" dry="$4" selected
  shift 4
  local args=(select --run-dir "$run" --repo "$repo" --mode "$mode" --choice "$FRACTAL_CHOICE")
  [ "$dry" != 1 ] || args+=(--dry-run)
  selected="$(python3 "$SKILL_DIR/scripts/forge-fractal.py" "${args[@]}" ${FRACTAL_ARGS[@]+"${FRACTAL_ARGS[@]}"} "$@")" || return $?
  if [ "$selected" != off ] && [ "$selected" != dry-fractal ]; then
    export FORGE_FRACTAL_RUN="$selected"
    DISPATCH="$SKILL_DIR/scripts/forge-fractal-dispatch.sh"
  elif [ "$selected" = dry-fractal ]; then
    unset FORGE_FRACTAL_RUN
    echo 'backend: fractal (dry run)' >&2
  else
    unset FORGE_FRACTAL_RUN
  fi
}

forge_fractal_parallel_run() {
  local plan="$1" repo="$2" dry="$3"; shift 3
  local flags=()
  [ ! -f "$plan/yolo_dwarf" ] || flags+=(--yolo-dwarf)
  [ ! -f "$plan/yolo_qa" ] || flags+=(--yolo-qa)
  forge_fractal_select "$plan" "$repo" parallel "$dry" ${flags[@]+"${flags[@]}"} || return $?
  if [ -n "${FORGE_FRACTAL_RUN:-}" ]; then
    python3 "$SKILL_DIR/scripts/forge-fractal.py" _remember "$SELF" run "$plan" "$@" || return $?
  fi
}
