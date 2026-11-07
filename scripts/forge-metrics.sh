#!/usr/bin/env bash
# Each runner owns its metric state in a process/subshell, including failure exits.
forge_metric_begin() {
  FORGE_METRIC_ROOT="$1"; FORGE_METRIC_ROLE="$2"
  FORGE_METRIC="$(python3 "$SKILL_DIR/scripts/forge-runtime.py" begin "$1" "$2")" || return 3
  # Dispatches started from here name this execution as their parent in the run dataset.
  export FORGE_DATASET_PARENT="${FORGE_METRIC##*/}"
  # The hook, when the caller defines it, records status before finish and Fractal's _outcome.
  trap 'metric_rc=$?; ! declare -F forge_metric_on_exit >/dev/null || forge_metric_on_exit "$metric_rc"; python3 "$SKILL_DIR/scripts/forge-runtime.py" finish "$FORGE_METRIC" "$FORGE_METRIC_ROOT" "$FORGE_METRIC_ROLE" pipeline "$metric_rc"; if [ -n "${FORGE_FRACTAL_RUN:-}" ]; then python3 "$SKILL_DIR/scripts/forge-fractal.py" _outcome "$FORGE_METRIC_ROOT" "$metric_rc"; fi; exit "$metric_rc"' EXIT
}
forge_metric_phase() {
  python3 "$SKILL_DIR/scripts/forge-runtime.py" phase "$FORGE_METRIC" "$1"
}

# --- run dataset (scripts/forge_dataset) --------------------------------------------
# Facts that are gone before any EXIT trap could read them: an aborted merge, a combine
# that rolled back, a branch that moved. Fail-open and silent, like every capture.
forge_dataset_off() {
  case "${FORGE_DATASET:-on}" in off|OFF|Off|0|no|false) return 0 ;; esac
  return 1
}
forge_dataset_event() { # forge_dataset_event <name> --dir <dir> [--task id] [--set k=v]... [--file label=path]...
  forge_dataset_off && return 0
  python3 "$SKILL_DIR/scripts/forge-dataset.py" event "$@" >/dev/null 2>&1 || true
}
# A worktree forge added or removed (#125): forge-ui's Worktrees screen lists every one, removed ones too.
# <kind> is task, integration, candidate or attribute (scripts/forge_worktrees.py); <branch> is - when detached.
# Off with the dataset (FORGE_DATASET=off), as forge_dataset_event is.
forge_worktree_event() { # forge_worktree_event created|removed <run dir> <repo> <path> <branch|-> <kind> [task]
  forge_dataset_event "worktree_$1" --dir "$2" ${7:+--task "$7"} --set repo_path="$3" --set path="$4" \
    --set branch="$5" --set kind="$6"
}
# The outcomes that came due for a run's repository (#120): what older integrations' lines became,
# read from git alone. At the end of `run` and `integrate`; bounded (FORGE_OUTCOMES_TIMEOUT seconds,
# default 20, plus a kill 10 s later), never changes the caller's exit code, logs its errors to the
# dataset's errors.log, and is off with FORGE_OUTCOMES=off or with the dataset.
forge_outcomes_harvest() { # forge_outcomes_harvest <plan dir>
  forge_dataset_off && return 0
  case "${FORGE_OUTCOMES:-on}" in off|OFF|Off|0|no|false) return 0 ;; esac
  local repo limit pid watcher
  repo="$(cat "$1/repo" 2>/dev/null)"
  [ -n "$repo" ] && [ -d "$repo" ] || return 0
  limit="${FORGE_OUTCOMES_TIMEOUT:-20}"
  case "$limit" in ''|*[!0-9]*) limit=20 ;; esac
  [ "$limit" -gt 0 ] || return 0
  python3 "$SKILL_DIR/scripts/forge-dataset.py" outcomes --repo "$repo" --deadline "$limit" --quiet \
    </dev/null >/dev/null 2>&1 &
  pid=$!
  ( sleep $(( limit + 10 )); kill -KILL "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  watcher=$!
  wait "$pid" 2>/dev/null
  pkill -P "$watcher" 2>/dev/null; kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
  return 0
}
forge_dataset_invocation() { # forge_dataset_invocation <command> <rc> <started> [argv...]
  forge_dataset_off && return 0
  local command="$1" rc="$2" started="$3"; shift 3
  # Options before the command name: everything after it is the subcommand's own argv.
  python3 "$SKILL_DIR/scripts/forge-dataset.py" invocation --dir "${1:-}" --rc "$rc" \
    --started "$started" --pid "$$" "$command" -- "$@" >/dev/null 2>&1 || true
}

# --- update notice (scripts/forge-version.py) ----------------------------------------
# One line on stderr when a newer forge release exists, checked at most once a day. Silent
# offline, in CI and with FORGE_UPDATE_CHECK=off; it never changes a run's exit code.
forge_update_notice() {
  python3 "$SKILL_DIR/scripts/forge-version.py" notice --source "$SKILL_DIR" </dev/null >&2 2>/dev/null || true
}
