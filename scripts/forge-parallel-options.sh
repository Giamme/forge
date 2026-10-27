#!/usr/bin/env bash
# Flags shared by several forge-parallel.sh subcommands, parsed in one place.
#
# Sourced at the top of forge-parallel.sh (an option parser sourced from inside a
# function, after that function already walked its arguments, cannot parse anything).
# Each subcommand's own `case` handles its private flags and falls through to this for
# the rest, before the Jev hook and its own "unknown option" error.
#
#   forge_pp_flag <subcommand> <plan-dir> <args...>
#     0  the flag was ours and valid; PP_SHIFT holds how many arguments it used
#     1  not ours, or not accepted by this subcommand
#   A malformed value exits through die (2).
#
# Persisted options become files in the plan dir, like --verify and --setup, so a later
# `retry` or the scheduler's task processes see them. Cost-bearing options that change
# what a run is allowed to spend (--retry-failed, --infra-retries) are deliberately NOT
# persisted: they apply to the invocation that asked for them. While the scheduler runs
# they are mirrored to $PLAN/retry_budget and $PLAN/infra_budget (plus scheduler.live) so
# `forge control` can change them live; the scheduler removes those files when it exits.
#
#   --timeout S             run retry         $PLAN/timeout        dispatch timeout, seconds (0 = none)
#   --retry-failed N        run               (this run only)      automatic reviewer-driven retries
#   --infra-retries M       run               (this run only)      waits out quota/auth/network stops
#   --infra-backoff SHAPE   run               $PLAN/infra_backoff  waits between those stops: exp:B[,C],
#                                                                  linear:S,T[,C] or list:a,b,... (default exp:60)
#   --infra-fallback        run               $PLAN/infra_fallback a dwarf's capacity stop moves it to the next healthy
#   --no-infra-fallback                                            model of its tier pool (off by default)
#   --qa-threshold Pn|none  run retry         $PLAN/qa_threshold   least severe finding that still blocks
#   --verify-retries N      run retry integrate  $PLAN/verify_retries  reruns of a failed verify command
#   --escalate-after N      run retry         $PLAN/escalate_after failed attempts on one model before the
#                                                                  next goes a tier up (default 3; 0 = off)
#   --escalate-effort       run retry         $PLAN/escalate_effort  on: before the stop, retry the same
#                                                                  model at the next effort its pairing accepts
#   --no-escalate-effort    run retry         (removes the file)   turn that step off (the default)
#   --final-review SPEC     integrate         $PLAN/final_qa       QA spec for the whole-run review
#   --verify-lane L=CMD     run retry         $PLAN/verify.d/<L>   a verification lane: name[options]=command,
#                                                                  options as in a lane file (group=g,first,...);
#                                                                  repeatable; replaces --verify and the lanes
#                                                                  of an earlier invocation
#   --verify-parallel N     run retry integrate  $PLAN/verify_parallel  lanes run at once (1-32, default 3)
PP_SHIFT=2
PP_RETRY_FAILED=0
PP_INFRA_RETRIES=0
PP_FINAL_REVIEW=""
PP_LANES_FRESH=0   # the first --verify-lane of an invocation starts a fresh verify.d/

# --verify-lane 'name[opt,opt]=command' -> $PLAN/verify.d/name: the options become the lane
# file's first line, `#forge: opt opt`, and the command follows, as in a lane file.
pp_lane() { # pp_lane <plan> <argument>
  local plan="$1" arg="$2" re='^([A-Za-z0-9][A-Za-z0-9_-]{0,63})(\[([^]]*)\])?=(.*)$'
  [[ "$arg" =~ $re ]] \
    || die "--verify-lane '$arg': expected name[options]=command (a name of letters, digits, _ and -, at most 64; options in closed brackets)"
  local name="${BASH_REMATCH[1]}" opts="${BASH_REMATCH[3]}" cmd="${BASH_REMATCH[4]}"
  [ -n "${cmd//[[:space:]]/}" ] || die "--verify-lane '$arg': the command is empty"
  if [ "$PP_LANES_FRESH" = 0 ]; then
    rm -rf "$plan/verify.d" && mkdir "$plan/verify.d" || die "could not create $plan/verify.d"
    PP_LANES_FRESH=1
  fi
  { [ -z "$opts" ] || printf '#forge: %s\n' "${opts//,/ }"; printf '%s\n' "$cmd"; } > "$plan/verify.d/$name"
  rm -f "$plan/verify_cmd"
}

pp_nonneg() { # pp_nonneg <flag> <value>
  case "$2" in ''|*[!0-9]*) die "$1 needs a non-negative integer" ;; esac
}

forge_pp_flag() {
  local cmd="$1" plan="$2"; shift 2
  PP_SHIFT=2
  case "${1:-}" in
    --timeout)
      case "$cmd" in run|retry) ;; *) return 1 ;; esac
      pp_nonneg --timeout "${2:-}"
      printf '%s' "$2" > "$plan/timeout" ;;
    --retry-failed)
      [ "$cmd" = run ] || return 1
      pp_nonneg --retry-failed "${2:-}"; PP_RETRY_FAILED="$2" ;;
    --infra-retries)
      [ "$cmd" = run ] || return 1
      pp_nonneg --infra-retries "${2:-}"; PP_INFRA_RETRIES="$2" ;;
    --infra-fallback)
      [ "$cmd" = run ] || return 1
      PP_SHIFT=1; printf 'on\n' > "$plan/infra_fallback" ;;
    --no-infra-fallback)
      [ "$cmd" = run ] || return 1
      PP_SHIFT=1; rm -f "$plan/infra_fallback" ;;
    --infra-backoff)
      [ "$cmd" = run ] || return 1
      PP_MSG="$(python3 "$SKILL_DIR/scripts/forge-schedule.py" backoff-check "${2:-}" 2>&1)" || die "${PP_MSG#forge: }"
      printf '%s\n' "$2" > "$plan/infra_backoff" ;;
    --qa-threshold)
      case "$cmd" in run|retry|review) ;; *) return 1 ;; esac
      case "${2:-}" in
        P0|P1|P2|P3) printf '%s' "$2" > "$plan/qa_threshold" ;;
        none|off) rm -f "$plan/qa_threshold" ;;
        *) die "--qa-threshold needs P0, P1, P2, P3 or none" ;;
      esac ;;
    --escalate-after)
      case "$cmd" in run|retry) ;; *) return 1 ;; esac
      pp_nonneg --escalate-after "${2:-}"
      printf '%s' "$2" > "$plan/escalate_after"
      # Off means off: the policy's own stop markers go too, so the scheduler retries again.
      if [ "$2" = 0 ]; then
        rm -f "$plan"/tasks/*/escalation.stop "$plan"/tasks/*/split.suggested "$plan"/tasks/*/escalation.next
      fi ;;
    --escalate-effort)
      case "$cmd" in run|retry) ;; *) return 1 ;; esac
      PP_SHIFT=1                                  # a switch, not --escalate-after's N
      printf 'on\n' > "$plan/escalate_effort" ;;
    --no-escalate-effort)
      case "$cmd" in run|retry) ;; *) return 1 ;; esac
      PP_SHIFT=1
      rm -f "$plan/escalate_effort" ;;
    --verify-retries)
      case "$cmd" in run|retry|integrate) ;; *) return 1 ;; esac
      pp_nonneg --verify-retries "${2:-}"
      printf '%s' "$2" > "$plan/verify_retries" ;;
    --verify-lane)
      case "$cmd" in run|retry) ;; *) return 1 ;; esac
      pp_lane "$plan" "${2-}" ;;
    --verify-parallel)
      case "$cmd" in run|retry|integrate) ;; *) return 1 ;; esac
      case "${2:-}" in ''|*[!0-9]*) die "--verify-parallel needs an integer from 1 to 32" ;; esac
      [ "$2" -ge 1 ] && [ "$2" -le 32 ] || die "--verify-parallel needs an integer from 1 to 32"
      printf '%s' "$2" > "$plan/verify_parallel" ;;
    --final-review)
      [ "$cmd" = integrate ] || return 1
      PP_FINAL_REVIEW="${2:?--final-review needs a QA spec}"
      printf '%s' "$PP_FINAL_REVIEW" > "$plan/final_qa" ;;
    *) return 1 ;;
  esac
  return 0
}
