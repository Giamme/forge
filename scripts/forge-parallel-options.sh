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
# persisted: they apply to the invocation that asked for them.
#
#   --timeout S             run retry         $PLAN/timeout        dispatch timeout, seconds (0 = none)
#   --retry-failed N        run               (this run only)      automatic reviewer-driven retries
#   --infra-retries M       run               (this run only)      waits out quota/auth/network stops
#   --qa-threshold Pn|none  run retry         $PLAN/qa_threshold   least severe finding that still blocks
#   --verify-retries N      run retry integrate  $PLAN/verify_retries  reruns of a failed verify command
#   --final-review SPEC     integrate         $PLAN/final_qa       QA spec for the whole-run review
PP_SHIFT=2
PP_RETRY_FAILED=0
PP_INFRA_RETRIES=0
PP_FINAL_REVIEW=""

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
    --qa-threshold)
      case "$cmd" in run|retry|review) ;; *) return 1 ;; esac
      case "${2:-}" in
        P0|P1|P2|P3) printf '%s' "$2" > "$plan/qa_threshold" ;;
        none|off) rm -f "$plan/qa_threshold" ;;
        *) die "--qa-threshold needs P0, P1, P2, P3 or none" ;;
      esac ;;
    --verify-retries)
      case "$cmd" in run|retry|integrate) ;; *) return 1 ;; esac
      pp_nonneg --verify-retries "${2:-}"
      printf '%s' "$2" > "$plan/verify_retries" ;;
    --final-review)
      [ "$cmd" = integrate ] || return 1
      PP_FINAL_REVIEW="${2:?--final-review needs a QA spec}"
      printf '%s' "$PP_FINAL_REVIEW" > "$plan/final_qa" ;;
    *) return 1 ;;
  esac
  return 0
}
