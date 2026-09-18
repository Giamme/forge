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
  if [ "$JEV_CHOICE" = off ]; then
    export FORGE_JEV=off
  elif [ "$JEV_CHOICE" = on ]; then
    export FORGE_JEV=on
  else
    unset FORGE_JEV 2>/dev/null || true
  fi
  unset FORGE_JEV_ROUTING FORGE_JEV_TESTS FORGE_JEV_GATES FORGE_JEV_MEMORY 2>/dev/null || true
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
