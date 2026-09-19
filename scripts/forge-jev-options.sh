#!/usr/bin/env bash
# Source in runners (forge-solo.sh / forge-parallel.sh, a later phase). Arrays and
# syntax remain compatible with macOS Bash 3.2: no associative arrays, no
# ${var,,}, and possibly-empty arrays are always expanded with the
# ${arr[@]+"${arr[@]}"} idiom.
#
# Precedence the Python side (scripts/forge_jev/) implements — keep these two in
# sync when either changes:
#   1. FORGE_JEV=off wins over everything: everything off, full stop.
#   2. Any FORGE_JEV_<CAP>=on acts as an allowlist — only those capabilities run
#      (so `--jev-tests` alone works without `--jev`).
#   3. Otherwise FORGE_JEV=on (from --jev) or the stored config's `enabled` flag
#      turns it on, and a capability explicitly off is subtracted.
#   4. Default: off.
#
# Phase 0 wires only the flags, env vars and freeze/restore files below. Nothing
# dispatches differently yet.

# What the environment said before any flag was parsed. Captured once, and exported so
# a second source of this file (there are several per run) sees the original answer
# rather than whatever forge_jev_export has since written into FORGE_JEV.
#
# This exists because wiring the flags up made a documented guarantee reachable for the
# first time: FORGE_JEV=off is a hard kill switch that wins over everything, and until
# forge_jev_export was actually called by a runner, it won by default -- nothing was
# there to override it. The first real run with --jev clobbered it.
if [ -z "${FORGE_JEV_ENV_CAPTURED:-}" ]; then
  FORGE_JEV_ENV_CAPTURED=1
  FORGE_JEV_ENV_KILL=0
  [ "${FORGE_JEV:-}" = off ] && FORGE_JEV_ENV_KILL=1
  FORGE_JEV_ENV_ROUTING="${FORGE_JEV_ROUTING:-}"
  FORGE_JEV_ENV_TESTS="${FORGE_JEV_TESTS:-}"
  FORGE_JEV_ENV_GATES="${FORGE_JEV_GATES:-}"
  FORGE_JEV_ENV_MEMORY="${FORGE_JEV_MEMORY:-}"
  export FORGE_JEV_ENV_CAPTURED FORGE_JEV_ENV_KILL FORGE_JEV_ENV_ROUTING \
         FORGE_JEV_ENV_TESTS FORGE_JEV_ENV_GATES FORGE_JEV_ENV_MEMORY
fi

JEV_CHOICE=""          # "" | on | off
JEV_ACT=0              # --jev-act
JEV_SHADOW=0           # --jev-shadow
JEV_CAPS=()            # entries like "tests=on" / "routing=off"

forge_jev_help() {
  cat <<'HELP'
Optional Jev (TypeSafe System One) advisory judgments (solo or parallel run):
  --jev | --no-jev           Explicit choice; unattended defaults off
  --jev-act                  Allow an applied judgment to change behavior
  --jev-shadow               Record judgments only; changes nothing
  --jev-<cap> | --no-jev-<cap>  Per-capability on/off: routing tests gates memory
FORGE_JEV=off is a hard kill switch and wins over every other setting.
Setup, status, doctor and enable/disable live under ./forge jev --help.
HELP
}

# forge_jev_flag "$@" — parse ONE option from the front of the argument list.
# Echoes nothing. Returns 0 and sets JEV_SHIFT to the number of argv items
# consumed (always 1 for our flags) when the option is ours; returns 1 and sets
# JEV_SHIFT=0 when the first argument is not one of ours; returns 2 on a usage
# error (message on stderr), also with JEV_SHIFT unset/irrelevant.
forge_jev_flag() {
  local opt="$1"
  JEV_SHIFT=0
  case "$opt" in
    --jev)
      [ -z "$JEV_CHOICE" ] || [ "$JEV_CHOICE" = on ] || { echo '--jev and --no-jev are mutually exclusive' >&2; return 2; }
      JEV_CHOICE=on; JEV_SHIFT=1; return 0
      ;;
    --no-jev)
      [ -z "$JEV_CHOICE" ] || [ "$JEV_CHOICE" = off ] || { echo '--jev and --no-jev are mutually exclusive' >&2; return 2; }
      JEV_CHOICE=off; JEV_SHIFT=1; return 0
      ;;
    --jev-act)
      JEV_ACT=1; JEV_SHIFT=1; return 0
      ;;
    --jev-shadow)
      JEV_SHADOW=1; JEV_SHIFT=1; return 0
      ;;
    --jev-routing|--jev-tests|--jev-gates|--jev-memory)
      JEV_CAPS+=("${opt#--jev-}=on"); JEV_SHIFT=1; return 0
      ;;
    --no-jev-routing|--no-jev-tests|--no-jev-gates|--no-jev-memory)
      JEV_CAPS+=("${opt#--no-jev-}=off"); JEV_SHIFT=1; return 0
      ;;
    *)
      return 1
      ;;
  esac
}

# forge_jev_export — translate the parsed state into the environment the
# Python side reads. Call after all flags are parsed (and after forge_jev_restore,
# if resuming, since restore re-parses the frozen selection through this path).
forge_jev_export() {
  if [ "${FORGE_JEV_ENV_KILL:-0}" = 1 ]; then
    # Rule 1 of the precedence above: it wins over everything, and "everything" has to
    # include this function. A flag must not be able to switch Jev back on.
    export FORGE_JEV=off
  elif [ "$JEV_CHOICE" = off ]; then
    export FORGE_JEV=off
  elif [ "$JEV_CHOICE" = on ]; then
    export FORGE_JEV=on
  else
    unset FORGE_JEV 2>/dev/null || true
  fi
  # Restore what the environment said, rather than clearing it: a per-capability
  # variable set by the caller is rule 2's allowlist, and wiping it here would have
  # made `FORGE_JEV_TESTS=on forge ...` silently do nothing. A flag for the same
  # capability still wins, because it is the more specific instruction.
  local cap_env
  for cap_env in ROUTING TESTS GATES MEMORY; do
    eval "local inherited=\"\${FORGE_JEV_ENV_$cap_env:-}\""
    if [ -n "$inherited" ]; then
      eval "export FORGE_JEV_$cap_env=\"\$inherited\""
    else
      unset "FORGE_JEV_$cap_env" 2>/dev/null || true
    fi
  done
  local entry cap val
  for entry in ${JEV_CAPS[@]+"${JEV_CAPS[@]}"}; do
    cap="${entry%%=*}"; val="${entry#*=}"
    case "$cap" in
      routing) export FORGE_JEV_ROUTING="$val" ;;
      tests)   export FORGE_JEV_TESTS="$val" ;;
      gates)   export FORGE_JEV_GATES="$val" ;;
      memory)  export FORGE_JEV_MEMORY="$val" ;;
    esac
  done
  if [ "$JEV_ACT" = 1 ]; then export FORGE_JEV_ACT=on; else unset FORGE_JEV_ACT 2>/dev/null || true; fi
  if [ "$JEV_SHADOW" = 1 ]; then export FORGE_JEV_SHADOW=on; else unset FORGE_JEV_SHADOW 2>/dev/null || true; fi
}

# forge_jev_record <run-dir> — freeze the resolved selection into
# <run-dir>/jev-selection.json. A resumed or retried run must make the same
# decisions as the original run, the same reason fractal-selection.json is
# frozen for Fractal: settings must not silently drift between the first
# attempt and a later resume/retry of the same run directory.
forge_jev_record() {
  local run="$1" caps_json="{}" entry cap val first=1
  for entry in ${JEV_CAPS[@]+"${JEV_CAPS[@]}"}; do
    cap="${entry%%=*}"; val="${entry#*=}"
    if [ "$first" = 1 ]; then caps_json="{\"$cap\": \"$val\""; first=0
    else caps_json="$caps_json, \"$cap\": \"$val\""; fi
  done
  [ "$first" = 1 ] || caps_json="$caps_json}"
  python3 -c '
import json, sys
run, choice, act, shadow, caps_json = sys.argv[1:6]
data = {
    "choice": choice if choice else None,
    "act": act == "1",
    "shadow": shadow == "1",
    "capabilities": json.loads(caps_json),
}
with open(run + "/jev-selection.json", "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
' "$run" "$JEV_CHOICE" "$JEV_ACT" "$JEV_SHADOW" "$caps_json"
}

# forge_jev_restore <run-dir> — re-export a previously frozen selection, for
# resume/retry of an existing run directory. Reads jev-selection.json (written
# by forge_jev_record) back into JEV_CHOICE/JEV_ACT/JEV_SHADOW/JEV_CAPS and
# calls forge_jev_export so the environment matches the original run exactly.
forge_jev_restore() {
  # Separate statements on purpose: bash expands every argument of a single `local`
  # before assigning any of them, so `selection="$run/..."` on this line would see an
  # empty run and silently resolve to /jev-selection.json.
  local run="$1" line selection
  selection="$run/jev-selection.json"
  [ -f "$selection" ] || return 0
  JEV_CAPS=()
  local parsed
  parsed="$(python3 -c '
import json, sys
with open(sys.argv[1]) as fh:
    data = json.load(fh)
choice = data.get("choice") or ""
print(choice)
print("1" if data.get("act") else "0")
print("1" if data.get("shadow") else "0")
for cap, val in sorted((data.get("capabilities") or {}).items()):
    print(cap + "=" + val)
' "$selection")" || return $?
  local i=0
  while IFS= read -r line; do
    case "$i" in
      0) JEV_CHOICE="$line" ;;
      1) JEV_ACT="$line" ;;
      2) JEV_SHADOW="$line" ;;
      *) [ -z "$line" ] || JEV_CAPS+=("$line") ;;
    esac
    i=$((i + 1))
  done <<EOF
$parsed
EOF
  forge_jev_export
}

# forge_jev_settle <plan-dir> — what `run` and `retry` do about Jev.
#
# `plan` is the command that makes the choice and freezes it. A later run or retry of
# the same plan must make the same decisions, for the reason fractal-selection.json is
# frozen: a resumed run that quietly changed its mind about whether a judgment may act
# is a run nobody can reason about afterwards.
#
# So a frozen selection wins over a flag passed here, and saying so out loud beats
# ignoring the flag in silence. When nothing was frozen — a plan dir written before
# selections existed — the flags parsed on this command apply and are then frozen, so
# the next retry of it is reproducible too.
forge_jev_settle() {
  local plan="$1"
  if [ -f "$plan/jev-selection.json" ]; then
    if [ -n "$JEV_CHOICE" ] || [ "$JEV_ACT" = 1 ] || [ "$JEV_SHADOW" = 1 ] \
       || [ "${#JEV_CAPS[@]}" -gt 0 ]; then
      printf 'forge: jev: using the selection frozen at plan time; ignoring the flags given here\n' >&2
    fi
    forge_jev_restore "$plan"
  else
    forge_jev_export
    forge_jev_record "$plan"
  fi
}

# forge_jev_active <capability> — succeeds (0) when Jev is enabled for that
# capability AND a key is configured; fails (non-zero) otherwise, including when
# the Python package is missing, the config is unreadable, or anything else goes
# wrong. Silent (no stdout/stderr) and never blocks: it only reads local config
# through `forge-jev.py status --json`, never the network.
#
# The status JSON is fetched at most once per shell (cached in
# FORGE_JEV_STATUS_JSON/FORGE_JEV_STATUS_FETCHED) so a caller checking several
# capabilities, or the same capability more than once in one verify_result call,
# pays for one subprocess instead of one per check.
FORGE_JEV_STATUS_JSON=""
FORGE_JEV_STATUS_FETCHED=0
forge_jev_active() {
  local cap="$1"
  # Two answers that need no subprocess, mirroring rules the Python side already
  # implements: the kill switch wins over everything, and a key can only come from
  # TYPESAFE_API_KEY or the config file, so with neither present nothing can be active.
  # Every runner shell asks this at least once, and a run that never mentioned Jev was
  # paying a python3 spawn per shell for the answer "no" (measured with fake CLIs:
  # +0.15s per plan and +0.35s per two-task run).
  [ "${FORGE_JEV:-}" = off ] && return 1
  if [ -z "${TYPESAFE_API_KEY:-}" ] \
     && [ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/forge/jev.json" ]; then
    return 1
  fi
  if [ "$FORGE_JEV_STATUS_FETCHED" != 1 ]; then
    FORGE_JEV_STATUS_FETCHED=1
    FORGE_JEV_STATUS_JSON="$(python3 "$SKILL_DIR/scripts/forge-jev.py" status --json 2>/dev/null)" \
      || FORGE_JEV_STATUS_JSON=""
  fi
  [ -n "$FORGE_JEV_STATUS_JSON" ] || return 1
  printf '%s' "$FORGE_JEV_STATUS_JSON" | python3 -c '
import json, sys
cap = sys.argv[1]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
sys.exit(0 if data.get("key_present") and data.get("capabilities", {}).get(cap) else 1)
' "$cap" 2>/dev/null
}
