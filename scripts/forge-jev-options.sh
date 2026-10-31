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
JEV_ACT=0              # --jev-act: still frozen as "act", no longer exported
JEV_SHADOW=0           # --jev-shadow
JEV_CAPS=()            # entries like "tests=on" / "routing=off"
# Decision-model gate flags (#36): the resolver's run-flag level, read from
# FORGE_DECIDE_FLAGS (scripts/forge_decide/resolve.py, level 6).
DECIDE_GATES=()        # --gate entries like "drift=on", in order; a later one wins
DECIDE_NO_GATES=0      # --no-gates
DECIDE_BUDGET_TOKENS="" # --decide-budget tokens=N
DECIDE_BUDGET_USD=""   # --decide-budget usd=X
# forge_decide.GATES and MODES; tests/test_decide_flags.py keeps these in sync.
FORGE_DECIDE_GATE_NAMES="drift forward prompt memory-curate memory-slice memory-stale verify-discover \
coverage retry-addressed dwarf-message attribution findings"
FORGE_DECIDE_MODE_NAMES="off shadow on"

forge_jev_help() {
  cat <<'HELP'
Optional Jev (TypeSafe System One) advisory judgments (solo or parallel run):
  --jev | --no-jev           Explicit choice; unattended defaults off
  --jev-act                  (no effect; kept for old commands)
  --jev-shadow               Record judgments only; changes nothing
  --jev-<cap> | --no-jev-<cap>  Per-capability on/off: routing tests gates memory
Decision-model gates for this run (frozen at plan time; win over every config level):
  --gate NAME=MODE[,NAME=MODE...]  Set gates off, shadow or on; repeatable, a later one wins
                             gates: drift forward prompt memory-curate memory-slice
                                    memory-stale verify-discover coverage retry-addressed
                                    dwarf-message attribution findings
  --no-gates                 Every gate off: no decision-model request at all
  --decide-budget tokens=N[,usd=X]  Cap this run's decision-model spend
FORGE_JEV=off is a hard kill switch and wins over every other setting, and a repo's
privacy floor (.forge/decide.json) wins over that.
Setup, status, doctor and enable/disable live under ./forge jev --help and ./forge decide --help.
HELP
}

# _forge_decide_word <word> <list> — succeeds when <word> is one of the
# space-separated <list>.
_forge_decide_word() {
  case " $2 " in *" $1 "*) return 0 ;; esac
  return 1
}

# _forge_decide_split <array-name> <text> — split <text> on IFS into the named array
# without pathname expansion, so a `*` or `?` in a flag value stays as typed (GitHub #82).
# The caller's noglob setting is left as it was.
_forge_decide_split() {
  local had_noglob=0
  case $- in *f*) had_noglob=1 ;; esac
  set -f
  eval "$1=(\$2)"
  [ "$had_noglob" = 1 ] || set +f
}

# _forge_decide_gate_list <list> — validate and append `name=mode[,name=mode...]` to
# DECIDE_GATES. Nothing is appended unless every entry is valid.
_forge_decide_gate_list() {
  local list="$1" entry name mode added=()
  if [ -z "$list" ]; then
    echo "--gate needs name=mode[,name=mode...]; gates: $FORGE_DECIDE_GATE_NAMES; modes: $FORGE_DECIDE_MODE_NAMES" >&2
    return 2
  fi
  local IFS=, entries
  _forge_decide_split entries "$list"
  for entry in ${entries[@]+"${entries[@]}"}; do
    name="${entry%%=*}"; mode="${entry#*=}"
    if [ "$name" = "$entry" ] || [ -z "$name" ]; then
      echo "--gate: '$entry' is not name=mode; gates: $FORGE_DECIDE_GATE_NAMES; modes: $FORGE_DECIDE_MODE_NAMES" >&2
      return 2
    fi
    if ! _forge_decide_word "$name" "$FORGE_DECIDE_GATE_NAMES"; then
      echo "--gate: unknown gate '$name'; gates: $FORGE_DECIDE_GATE_NAMES" >&2
      return 2
    fi
    if ! _forge_decide_word "$mode" "$FORGE_DECIDE_MODE_NAMES"; then
      echo "--gate: $name=$mode is not a mode; modes: $FORGE_DECIDE_MODE_NAMES" >&2
      return 2
    fi
    added+=("$name=$mode")
  done
  if [ "${#added[@]}" -eq 0 ]; then
    echo "--gate needs name=mode[,name=mode...]; gates: $FORGE_DECIDE_GATE_NAMES; modes: $FORGE_DECIDE_MODE_NAMES" >&2
    return 2
  fi
  DECIDE_GATES+=("${added[@]}")
  return 0
}

# _forge_decide_budget_spec <spec> — validate `tokens=N[,usd=X]` into
# DECIDE_BUDGET_TOKENS / DECIDE_BUDGET_USD; a field it does not name is kept.
_forge_decide_budget_spec() {
  local spec="$1" part field value tokens="$DECIDE_BUDGET_TOKENS" usd="$DECIDE_BUDGET_USD"
  local usage="--decide-budget expects tokens=N[,usd=X] (N a whole number, X a number, both >= 0)"
  [ -n "$spec" ] || { echo "$usage" >&2; return 2; }
  local IFS=, parts
  _forge_decide_split parts "$spec"
  for part in ${parts[@]+"${parts[@]}"}; do
    field="${part%%=*}"; value="${part#*=}"
    [ "$field" != "$part" ] || { echo "$usage; got '$part'" >&2; return 2; }
    case "$field" in
      tokens)
        printf '%s' "$value" | grep -Eq '^[0-9]+$' || { echo "$usage; got tokens=$value" >&2; return 2; }
        tokens="$value" ;;
      usd)
        printf '%s' "$value" | grep -Eq '^([0-9]+(\.[0-9]*)?|\.[0-9]+)$' || { echo "$usage; got usd=$value" >&2; return 2; }
        usd="$value" ;;
      *) echo "$usage; got '$part'" >&2; return 2 ;;
    esac
  done
  DECIDE_BUDGET_TOKENS="$tokens"; DECIDE_BUDGET_USD="$usd"
  return 0
}

# Any of the decide flags was given (or restored).
_forge_decide_flags_set() {
  [ "${#DECIDE_GATES[@]}" -gt 0 ] || [ "$DECIDE_NO_GATES" = 1 ] \
    || [ -n "$DECIDE_BUDGET_TOKENS" ] || [ -n "$DECIDE_BUDGET_USD" ]
}

# forge_jev_flag "$@" — parse ONE option from the front of the argument list.
# Echoes nothing. Returns 0 and sets JEV_SHIFT to the number of argv items
# consumed (2 for `--gate LIST` and `--decide-budget SPEC`, else 1) when the option
# is ours; returns 1 and sets JEV_SHIFT=0 when the first argument is not one of ours;
# returns 2 on a usage error (message on stderr), also with JEV_SHIFT unset/irrelevant.
forge_jev_flag() {
  local opt="$1"
  JEV_SHIFT=0
  case "$opt" in
    --gate)
      [ $# -ge 2 ] || { echo "--gate needs name=mode[,name=mode...]; gates: $FORGE_DECIDE_GATE_NAMES; modes: $FORGE_DECIDE_MODE_NAMES" >&2; return 2; }
      _forge_decide_gate_list "$2" || return 2
      JEV_SHIFT=2; return 0
      ;;
    --gate=*)
      _forge_decide_gate_list "${opt#--gate=}" || return 2
      JEV_SHIFT=1; return 0
      ;;
    --no-gates)
      DECIDE_NO_GATES=1; JEV_SHIFT=1; return 0
      ;;
    --decide-budget)
      [ $# -ge 2 ] || { echo "--decide-budget expects tokens=N[,usd=X]" >&2; return 2; }
      _forge_decide_budget_spec "$2" || return 2
      JEV_SHIFT=2; return 0
      ;;
    --decide-budget=*)
      _forge_decide_budget_spec "${opt#--decide-budget=}" || return 2
      JEV_SHIFT=1; return 0
      ;;
    --jev)
      [ -z "$JEV_CHOICE" ] || [ "$JEV_CHOICE" = on ] || { echo '--jev and --no-jev are mutually exclusive' >&2; return 2; }
      JEV_CHOICE=on; JEV_SHIFT=1; return 0
      ;;
    --no-jev)
      [ -z "$JEV_CHOICE" ] || [ "$JEV_CHOICE" = off ] || { echo '--jev and --no-jev are mutually exclusive' >&2; return 2; }
      JEV_CHOICE=off; JEV_SHIFT=1; return 0
      ;;
    --jev-act)
      # Once per command. Restore and settle of a frozen selection set JEV_ACT
      # without coming through here, so they stay silent.
      JEV_ACT=1
      if [ "${JEV_ACT_WARNED:-}" != 1 ]; then
        echo "forge: --jev-act no longer does anything; decision models never change a task's tier or model" >&2
        JEV_ACT_WARNED=1
      fi
      JEV_SHIFT=1
      return 0
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
  # Recorded in jev-selection.json and never exported: nothing reads it any more.
  unset FORGE_JEV_ACT 2>/dev/null || true
  if [ "$JEV_SHADOW" = 1 ]; then export FORGE_JEV_SHADOW=on; else unset FORGE_JEV_SHADOW 2>/dev/null || true; fi
  # The run-flag level of the gate resolver. Unset with no decide flag, so a run
  # without them never carries an earlier command's choice.
  if _forge_decide_flags_set; then
    FORGE_DECIDE_FLAGS="$(_forge_decide_flags_json)" || return $?
    export FORGE_DECIDE_FLAGS
  else
    unset FORGE_DECIDE_FLAGS 2>/dev/null || true
  fi
}

# _forge_decide_flags_json — the parsed decide flags as one JSON object,
# {"gates": {gate: mode}, "no_gates": bool, "budget": {"run_tokens": N, "run_usd": X}},
# the shape FORGE_DECIDE_FLAGS and jev-selection.json hold. A later --gate for the
# same gate wins.
_forge_decide_flags_json() {
  python3 -c '
import json, sys
no_gates, tokens, usd = sys.argv[1:4]
gates = {}
for entry in sys.argv[4:]:
    name, _, mode = entry.partition("=")
    gates[name] = mode
budget = {}
if tokens:
    budget["run_tokens"] = int(tokens)
if usd:
    budget["run_usd"] = float(usd)
print(json.dumps(dict(gates=gates, no_gates=no_gates == "1", budget=budget), sort_keys=True))
' "$DECIDE_NO_GATES" "$DECIDE_BUDGET_TOKENS" "$DECIDE_BUDGET_USD" \
    ${DECIDE_GATES[@]+"${DECIDE_GATES[@]}"}
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
  local decide_json
  decide_json="$(_forge_decide_flags_json)" || return $?
  python3 -c '
import json, sys
run, choice, act, shadow, caps_json, decide_json = sys.argv[1:7]
decide = json.loads(decide_json)
data = {
    "choice": choice if choice else None,
    "act": act == "1",
    "shadow": shadow == "1",
    "capabilities": json.loads(caps_json),
    # The decide run flags (--gate, --no-gates, --decide-budget). A file without
    # these keys, from an older plan, restores as no flags.
    "gates": decide["gates"],
    "no_gates": decide["no_gates"],
    "decide_budget": decide["budget"],
}
with open(run + "/jev-selection.json", "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
' "$run" "$JEV_CHOICE" "$JEV_ACT" "$JEV_SHADOW" "$caps_json" "$decide_json"
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
  DECIDE_GATES=(); DECIDE_NO_GATES=0; DECIDE_BUDGET_TOKENS=""; DECIDE_BUDGET_USD=""
  local parsed
  # After the three fixed lines, each line is tagged: cap:, gate:, no_gates:, tokens:
  # or usd:. A value the flags could not have produced is dropped, as the resolver
  # would drop it.
  parsed="$(python3 -c '
import json, sys
gate_names, mode_names = sys.argv[2].split(), sys.argv[3].split()
with open(sys.argv[1]) as fh:
    data = json.load(fh)
choice = data.get("choice") or ""
print(choice)
print("1" if data.get("act") else "0")
print("1" if data.get("shadow") else "0")
for cap, val in sorted((data.get("capabilities") or {}).items()):
    print("cap:" + cap + "=" + val)
gates = data.get("gates")
for name, mode in sorted((gates if isinstance(gates, dict) else {}).items()):
    if name in gate_names and mode in mode_names:
        print("gate:" + name + "=" + mode)
if data.get("no_gates") is True:
    print("no_gates:1")
budget = data.get("decide_budget")
budget = budget if isinstance(budget, dict) else {}
tokens, usd = budget.get("run_tokens"), budget.get("run_usd")
if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
    print("tokens:%d" % tokens)
if isinstance(usd, (int, float)) and not isinstance(usd, bool) and 0 <= usd < float("inf"):
    print("usd:%r" % float(usd))
' "$selection" "$FORGE_DECIDE_GATE_NAMES" "$FORGE_DECIDE_MODE_NAMES")" || return $?
  local i=0
  while IFS= read -r line; do
    case "$i" in
      0) JEV_CHOICE="$line" ;;
      1) JEV_ACT="$line" ;;
      2) JEV_SHADOW="$line" ;;
      *)
        case "$line" in
          cap:*)      JEV_CAPS+=("${line#cap:}") ;;
          gate:*)     DECIDE_GATES+=("${line#gate:}") ;;
          no_gates:1) DECIDE_NO_GATES=1 ;;
          tokens:*)   DECIDE_BUDGET_TOKENS="${line#tokens:}" ;;
          usd:*)      DECIDE_BUDGET_USD="${line#usd:}" ;;
        esac
        ;;
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
       || [ "${#JEV_CAPS[@]}" -gt 0 ] || _forge_decide_flags_set; then
      printf 'forge: jev: using the selection frozen at plan time; ignoring the flags given here\n' >&2
    fi
    forge_jev_restore "$plan"
  else
    forge_jev_export
    forge_jev_record "$plan"
  fi
}

# forge_decide_active <gate> — succeeds (0) when that gate's site can be asked:
# `forge-jev.py status --json` → gates.<gate>.ready, which means the gate is shadow or
# on after every level (run flags, the kill switch and the privacy floor included) and
# its provider has a key or is local. Fails (non-zero) otherwise, including when the
# Python package is missing, the config is unreadable, or anything else goes wrong.
# Silent (no stdout/stderr) and never blocks: it only reads local config, never the
# network. A shadowed gate passes too; the Python command asks it and prints nothing.
#
# The status JSON is fetched at most once per shell (cached in
# FORGE_DECIDE_STATUS_JSON/FORGE_DECIDE_STATUS_FETCHED) so a caller checking several
# gates, or the same gate more than once in one verify_result call, pays for one
# subprocess instead of one per check. It reads FORGE_DECIDE_FLAGS and
# FORGE_DECIDE_REPO, so the runners export both before the first check.
FORGE_DECIDE_STATUS_JSON=""
FORGE_DECIDE_STATUS_FETCHED=0
forge_decide_active() {
  local gate="$1"
  if [ "$FORGE_DECIDE_STATUS_FETCHED" != 1 ]; then
    FORGE_DECIDE_STATUS_FETCHED=1
    FORGE_DECIDE_STATUS_JSON="$(python3 "$SKILL_DIR/scripts/forge-jev.py" status --json 2>/dev/null)" \
      || FORGE_DECIDE_STATUS_JSON=""
  fi
  [ -n "$FORGE_DECIDE_STATUS_JSON" ] || return 1
  printf '%s' "$FORGE_DECIDE_STATUS_JSON" | python3 -c '
import json, sys
try:
    gate = json.load(sys.stdin)["gates"][sys.argv[1]]
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(gate, dict) and gate.get("ready") is True else 1)
' "$gate" 2>/dev/null
}

# forge_decide_env <repo> <run-dir> — export what the privacy check and the budget's
# per-run counter read: FORGE_DECIDE_REPO (the repo's top level) and
# FORGE_DECIDE_RUN_DIR (the plan dir, or a solo run dir), both absolute. Call it
# before the first decision-model step; it drops the cached status, which depends on
# the repo.
forge_decide_env() {
  local repo="$1" run="$2" top
  top="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" || top=""
  [ -n "$top" ] || top="$(cd "$repo" 2>/dev/null && pwd)" || top="$repo"
  export FORGE_DECIDE_REPO="$top"
  export FORGE_DECIDE_RUN_DIR="$(cd "$run" 2>/dev/null && pwd || printf '%s' "$run")"
  FORGE_DECIDE_STATUS_FETCHED=0
  FORGE_DECIDE_STATUS_JSON=""
}

# forge_decide_plan_lines <repo> — the advisory lines `plan` prints after its table:
# the repo's privacy floor, then which gates are on and in shadow. Prints nothing when
# neither applies, and nothing at all when they cannot be computed.
forge_decide_plan_lines() {
  local repo="$1" status lines
  status="$(python3 "$SKILL_DIR/scripts/forge-decide.py" status --repo "$repo" --json 2>/dev/null)" || return 0
  [ -n "$status" ] || return 0
  lines="$(printf '%s' "$status" | python3 -c '
import json, sys
repo = sys.argv[1]
try:
    data = json.load(sys.stdin)
    lines = []
    privacy = data.get("privacy") or {}
    if privacy.get("floor") is True:
        lines.append("decide: %s is under a privacy floor (.forge/decide.json): "
                     "no decision-model request will be sent" % repo)
    gates = data.get("gates") or {}
    on = [name + (" (forced)" if gate.get("forced") else "")
          for name, gate in gates.items() if gate.get("mode") == "on"]
    shadow = [name for name, gate in gates.items() if gate.get("mode") == "shadow"]
    if on or shadow:
        lines.append("decide: on: %s; shadow: %s" % (", ".join(on) or "none",
                                                     ", ".join(shadow) or "none"))
except Exception:
    sys.exit(1)
for line in lines:
    print(line)
' "$repo" 2>/dev/null)" || return 0
  [ -z "$lines" ] || printf '%s\n' "$lines" | sed 's/^/forge: /' >&2
  return 0
}
