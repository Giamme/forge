#!/usr/bin/env bash
# forge-parallel.sh — decomposed forge runs: many dwarves working concurrently in
# isolated git worktrees, each task reviewed by its own QA pass, only passing work
# merged. Bash 3.2 entrypoints keep the task and merge contracts; a Python 3
# standard-library coordinator schedules ready tasks without wave barriers.
#
# Usage:
#   forge-parallel.sh plan <plan-dir> --repo <dir> [routing flags] [--decompose-level low|medium|high] [--ui]
#                                     [--qa-threshold P0|P1|P2|P3|none|default] [--profile NAME | --no-profile]
#                                     [--collapse-chains]
#   forge-parallel.sh auto <plan-dir> --repo <dir> --goal-file <file> [--planner <spec>] [--decompose-level L]
#                                     [--plan auto|always|never] [--one-task-difficulty low|medium|high]
#                                     [--no-collapse] [--approved] [plan flags...] [-- run flags...]
#   forge-parallel.sh run  <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa]
#                                     [--verify <command>] [--setup <command>] [--dry-run]
#                                     [--timeout S] [--retry-failed N] [--infra-retries M]
#                                     [--qa-threshold P0|P1|P2|P3|none|default] [--verify-retries N] [--no-forecast]
#                                     [--sweep P0|P1|P2|P3 [--sweep-plausible] [--sweep-max N]]
#                                     [--sweep-ahead | --no-sweep-ahead] [--attribute | --no-attribute]
#   forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>] [--setup <command>]
#                                     [--answer <text> | --answer-file <file>]   (a NEEDS_INPUT task)
#                                     [--decide retry=N|threshold=Pn] [--dwarf <spec>] --reason "<why>" --approved
#                                                                              (a NEEDS_DECISION task)
#                                     [--no-dispatch]   (prepare it and start nothing: the next `run` dispatches it)
#                                     [--timeout S] [--qa-threshold Pn|none|default] [--verify-retries N]
#                                     [--retry-failed N] [--infra-retries M]
#   forge-parallel.sh integrate <plan-dir> --approved [--verify-retries N] [--final-review <qa-spec>]
#   forge-parallel.sh review <plan-dir> [--qa <spec>] [--yolo-qa] [--qa-threshold Pn|none|default]
#   forge-parallel.sh attribute <plan-dir> [--force]   (find a merge that breaks the failed verification)
#   forge-parallel.sh accept <plan-dir> <task-id> --reason "<why>" --approved
#   forge-parallel.sh adopt <plan-dir> <commit|a..b> | --head --reason "<why>" [--qa <spec>] [--approved]
#                                     (a commit made outside forge, reviewed alone, comes into the run)
#   forge-parallel.sh split <plan-dir> <new-plan> --tasks <id,id> [--dry-run]
#   forge-parallel.sh combine <new-plan> <plan-a> <plan-b> [<plan>...] [--verify <command>] [--dry-run]
#   forge-parallel.sh _task <plan-dir> <task-id>     (internal; coordinator target)
#   forge-parallel.sh _qa_only <plan-dir> <task-id>  (internal; exit 0 when the next entry only re-runs QA)
#   forge-parallel.sh _waves <plan-dir>             (internal; rewrite waves.tsv after a live `forge control add`)
#   forge-parallel.sh _sweep <plan-dir> --severity Pn [--plausible] [--max N]
#                                                   (internal; plan the finding sweep, print its task ids)
#
# Routing flags (plan):
#   --dwarf <spec>          fallback for any difficulty tier not named below
#   --dwarf-high <spec>     spec (or comma-list, pooled) for high-difficulty tasks
#   --dwarf-medium <spec>
#   --dwarf-low <spec>
#   --qa, --qa-high, --qa-medium, --qa-low    identical grammar
#   --decompose-level L     the --decompose-level the goal was split at, kept as a record
#   --shadow-qa <spec[,..]> opt-in: a second review of a sampled share of reviewed attempts,
#                           by a listed reviewer of another model family, recorded and
#                           never read (measures reviewer agreement). `none` turns it off
#   --shadow-rate R         that share, 0..1 (default 0.1)
#   --reviewer-session      opt-in experiment: a later review round continues the reviewer's
#                           earlier session (claude, openclaude, codex) when the QA spec is
#                           unchanged, and starts fresh whenever it cannot; each round is
#                           logged in tasks/<id>/review_sessions.tsv and tagged in the
#                           dataset. `--no-reviewer-session` turns it off (the default)
#   --implementer-session   opt-in: a retry continues the implementer's earlier session
#                           (claude, openclaude) when its spec is unchanged and its work is
#                           still what the review judged, and starts fresh whenever it cannot;
#                           each attempt is logged in tasks/<id>/implementer_sessions.tsv.
#                           `--no-implementer-session` turns it off (the default)
#   --retro | --no-retro   agents record what got in their way (retro, #132); plan file `retro_mode`
#                           (on or off; not `retro`, the notes directory). Without a flag the plan keeps its earlier choice, else takes the
#                           user `features` setting. The first plan with it on says what is recorded
#   --brief LEVEL           advisory, gated, strict or off (#135): one cheap read-only dispatch per
#                           task before it starts. Plan files `brief`, `brief_scope` (--brief-scope
#                           task|wave|plan, default task) and `brief_model` (--brief-model SPEC). On
#                           with no model is refused (exit 2); `off` removes the files
#   --summaries on|off      a generated summary per task (#133); plan files `summaries` and
#                           `summaries_model` (--summaries-model SPEC); on with no model is refused.
#                           A flag beats the user setting (forge config show), which is read once, at
#                           plan time; a re-plan without the flag keeps what the plan has
#   --fast ROLES            which of dwarf, qa, planner, review ask for the fast service tier
#                           (comma list, or all, or none). Kept in the plan; a re-plan without
#                           the flag keeps it, and none removes it. codex and claude only
#   --profile NAME          take the routing and policy values of a named profile (forge config
#                           profile list) instead of typing them: frozen into the plan as
#                           profile.json, shown in the table, and its decision-model gates and
#                           budget stay the plan's whatever decide.json says later. A flag of
#                           this call replaces just that one value. --no-profile uses none (and
#                           removes the plan's frozen copy); with neither, the plan keeps its
#                           copy, and a plan that has none gets the default profile, if one is set
#   --explore E             on by default at 0.2 (0 turns it off, and is kept): a row left
#                           to forge gets, with probability E/2, its neighbouring model
#                           (one tier away in the given pools) instead of its pool's turn;
#                           each decision and its odds go to tasks/<id>/route.jsonl
#                           (precedence: flag, profile, this plan's earlier choice, FORGE_ROUTE_EXPLORE)
#   --collapse-chains       this call only (not kept): after the task table is validated and
#                           before routing, merge a chain into one task named combined.
#                           A one-task or branching plan is left as it is, and collapsing
#                           a plan that is already collapsed is not an error
#
# The QA threshold (plan, run, retry, review; kept for the plan):
#   --qa-threshold Pn       the least severe CONFIRMED finding that still blocks (P0 worst ...
#                           P3 mildest; P2 when not set). forge decides the verdict from the
#                           findings: a PASS that lists a CONFIRMED finding at Pn or worse (or
#                           one with no severity) becomes FAIL; a FAIL made only of findings
#                           less severe than Pn is accepted as PASS-with-known-issues, and those
#                           are recorded in known_issues.md. `none` (or `off`) = P3, every
#                           CONFIRMED finding blocks; `default` goes back to P2
#
# Opt-in run behaviour (all off unless asked):
#   --retry-failed N        re-queue a task whose reviewer said FAIL up to N more times,
#                           inside the same run, while the other tasks keep going
#   --infra-retries M       wait out quota/auth/network stops up to M times instead of pausing
#   --sweep Pn              once every task has merged, before verification: the open findings
#                           at Pn or worse (tolerated, and PLAUSIBLE with --sweep-plausible) become
#                           at most --sweep-max N fix tasks (default 5), one per file, each with a
#                           narrow review; the sweep plan is printed and kept in sweep/. Once per
#                           plan; what it does not fix stays for `forge issues`
#   --sweep-ahead           a task is also told the open findings in files it owns, to fix only
#                           on its way (kept for the plan; --no-sweep-ahead turns it off)
#   --attribute             when the run's combined verification fails, search the merge order for
#                           a merge that breaks it (kept for the plan; --no-attribute turns it off)
#
# auto (#87, #88): from a goal to a finished run in one blocking command, with no orchestrating
# session in between. In a NEW plan dir (created when missing; one that already has tasks.tsv
# is refused, exit 2): the goal is copied verbatim to <plan-dir>/request.md and `auto` is
# written to <plan-dir>/orchestrator (the plan event in the dataset records it). Then
#   1. the gate (--plan): auto = forge-planner.py gate; always = plan; never = one task
#   2. one task: forge-planner.py one-task --difficulty (--one-task-difficulty, default medium);
#      no planner is dispatched
#   3. planning: the spec is --planner, else the routing.planner of the profile the plan flags
#      select (neither: exit 2). A preflight (forge-dispatch.sh doctor) runs before any model
#      call, the planner is dispatched into the plan dir (with the fast tier when planner is a
#      fast role), and its reply is parsed strictly by forge-planner.py parse
#   4. `plan` with every flag before `--` that is not auto's own, plus --collapse-chains unless
#      --no-collapse. When its gates send items back (plan.sendback.md, exit 4), the planner is
#      re-dispatched once with that file appended and the reply planned again
#   5. without --approved: stop, exit 0, after printing the exact `run` command; nothing is
#      dispatched after the planner. With --approved (the user's explicit pre-approval): `run`
#      with the flags after `--`, and auto exits with its code
# auto exit codes: 0 stopped at approval, or the run's 0 | 2 usage (no planner, a plan dir that
#   already has tasks.tsv, ...) | 4 the planner failed, its reply was refused, or the plan gates
#   sent items back a second time | 8 the planner stopped on an infrastructure failure (nothing
#   was dispatched after it) | otherwise the exit code of the preflight, of `plan` or of `run`
#
# Exit codes: 0 ok | 2 usage/validation | 3 precondition (not a git repo, etc.)
#             5 some task failed QA or verification failed | 6 integration conflict
#             8 paused on an infrastructure failure (quota, auth, network): no attempt was
#               spent, fix the cause and re-run `run`
set -uo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCH="$SKILL_DIR/scripts/forge-dispatch.sh"
MEMORY="$SKILL_DIR/scripts/forge-memory.sh"
source "$SKILL_DIR/scripts/forge-artifact.sh"
source "$SKILL_DIR/scripts/forge-metrics.sh"
source "$SKILL_DIR/scripts/forge-fractal-options.sh"
source "$SKILL_DIR/scripts/forge-ui-options.sh"
# Beside fractal's, and for the same reason: an option parser sourced from inside
# a function, after that function has already walked its arguments, cannot parse
# anything. Every --jev flag was rejected as "unknown option" until this line.
JEV_OPTS=0
[ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ] \
  && source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && JEV_OPTS=1
# Flags shared by several subcommands, then the helper libraries. Functions only, resolved
# at call time, so they may use die/note/field and friends defined below.
source "$SKILL_DIR/scripts/forge-parallel-options.sh"
for forge_lib in task gates verify attribute plans review adopt; do
  [ -f "$SKILL_DIR/scripts/forge-lib-$forge_lib.sh" ] && source "$SKILL_DIR/scripts/forge-lib-$forge_lib.sh"
done
OUTPUT=summary

die()  { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }
note() { printf 'forge: %s\n' "$*" >&2; }

# --- tsv helpers --------------------------------------------------------------
# awk rather than `read`, because tab is IFS-whitespace: `IFS=$'\t' read` collapses
# consecutive tabs and would silently shift every column after an empty field.
TASKS_COLS="id deps difficulty files dwarf qa title"
col_no() { # col_no <name> -> 1-based index
  local want="$1" i=1 c
  for c in $TASKS_COLS; do [ "$c" = "$want" ] && { echo "$i"; return; }; i=$((i+1)); done
  echo 0
}
field() { # field <tasks.tsv> <id> <colname>
  awk -F'\t' -v id="$2" -v n="$(col_no "$3")" '!/^#/ && NF && $1==id {print $n; exit}' "$1"
}
all_ids() { awk -F'\t' '!/^#/ && NF {print $1}' "$1"; }

list_has() { # list_has <needle> <comma-list>
  local n="$1" l="$2" w
  [ "$l" = "-" ] && return 1
  for w in $(printf '%s' "$l" | tr ',' ' '); do [ "$w" = "$n" ] && return 0; done
  return 1
}
lists_overlap() { # lists_overlap <comma-list-a> <space-list-b>
  local a="$1" b="$2" w v
  [ "$a" = "-" ] && return 1
  for w in $(printf '%s' "$a" | tr ',' ' '); do
    w="${w#./}"; w="${w%/}"
    for v in $b; do
      v="${v#./}"; v="${v%/}"
      case "$w/" in "$v/"*) return 0 ;; esac
      case "$v/" in "$w/"*) return 0 ;; esac
      [ "$w" = . ] || [ "$v" = . ] && return 0
    done
  done
  return 1
}

# --- plan ---------------------------------------------------------------------
# Resolves each task's difficulty into a concrete dwarf/qa spec and writes it back
# into tasks.tsv, so `run` never needs to know the routing rules. An explicit value
# already in the column always wins — that is how an approval-gate override for one
# row survives a re-plan.
pool_pick() { # pool_pick <comma-list> <index>
  local list="$1" idx="$2" n=0 w last=""
  for w in $(printf '%s' "$list" | tr ',' ' '); do n=$((n+1)); done
  [ "$n" -eq 0 ] && { echo ""; return; }
  idx=$((idx % n)); n=0
  for w in $(printf '%s' "$list" | tr ',' ' '); do
    [ "$n" -eq "$idx" ] && { echo "$w"; return; }
    n=$((n+1)); last="$w"
  done
  echo "$last"
}

# --- profiles (#48) -------------------------------------------------------------
# A named profile is chosen at `plan` (or by forge-solo.sh), frozen as <dir>/profile.json
# and applied as the values its flags would have set. forge_decide.profiles does the
# reading, checking and freezing; these functions only carry its answers to the shell.
forge_profile_py() { PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 -m forge_decide.profiles "$@"; }

# Gates and budget come from the frozen copy: FORGE_DECIDE_PROFILE_JSON is read by the
# decide resolution at its profile level, for this process and every child of it.
forge_profile_export() { # forge_profile_export <plan-dir>
  local text
  [ -f "${1:-}/profile.json" ] || return 0
  text="$(forge_profile_py decide "$1/profile.json" 2>/dev/null)" || return 0
  [ -z "$text" ] || export FORGE_DECIDE_PROFILE_JSON="$text"
  return 0
}

# Before anything is written: an unknown or invalid profile stops `plan` here (exit 2).
forge_profile_precheck() { # forge_profile_precheck <plan-dir> <plan's arguments...>
  local plan="$1" name="" none=0 repo="" prev="" arg; shift
  for arg in "$@"; do
    case "$prev" in
      --profile) name="$arg"; prev=""; continue ;;
      --repo) repo="$arg"; prev=""; continue ;;
    esac
    prev=""
    case "$arg" in --profile|--repo) prev="$arg" ;; --no-profile) none=1 ;; esac
  done
  [ -z "$name" ] || [ "$none" = 0 ] || die "--profile and --no-profile are mutually exclusive"
  [ -n "$repo" ] || repo="$(cat "$plan/repo" 2>/dev/null)"
  local flags=()
  [ -z "$name" ] || flags+=(--profile "$name")
  [ "$none" = 0 ] || flags+=(--no-profile)
  forge_profile_py freeze "$plan" ${repo:+--repo "$repo"} ${flags[@]+"${flags[@]}"} --check-only || exit $?
}

# Chooses, freezes and applies the plan's profile. Reads and sets do_plan's locals (dynamic
# scope, as forge_fast_parse does): a value a flag of this call gave is never replaced.
forge_profile_plan() {
  local args=() out key val repo
  [ -z "$D_ANY" ] || args+=(--given routing.dwarf.any)
  [ -z "$D_LO" ] || args+=(--given routing.dwarf.low)
  [ -z "$D_MD" ] || args+=(--given routing.dwarf.medium)
  [ -z "$D_HI" ] || args+=(--given routing.dwarf.high)
  [ -z "$Q_ANY" ] || args+=(--given routing.qa.any)
  [ -z "$Q_LO" ] || args+=(--given routing.qa.low)
  [ -z "$Q_MD" ] || args+=(--given routing.qa.medium)
  [ -z "$Q_HI" ] || args+=(--given routing.qa.high)
  [ -z "$PLANNER" ] || args+=(--given routing.planner)
  [ -z "$SHADOW_QA" ] || args+=(--given policy.shadow_qa)
  [ -z "$SHADOW_RATE" ] || args+=(--given policy.shadow_rate)
  [ -z "$EXPLORE" ] || args+=(--given policy.explore)
  [ "$FAST_GIVEN" != 1 ] || args+=(--given policy.fast)
  [ "$PF_QT" != 1 ] || args+=(--given policy.qa_threshold)
  [ -z "$PF_NAME" ] || args+=(--profile "$PF_NAME")
  [ "$PF_NONE" = 0 ] || args+=(--no-profile)
  repo="$REPO"; [ -n "$repo" ] || repo="$(cat "$PLAN/repo" 2>/dev/null)"
  out="$(forge_profile_py freeze "$PLAN" ${repo:+--repo "$repo"} ${args[@]+"${args[@]}"})" || exit $?
  while IFS="$(printf '\t')" read -r key val; do
    case "$key" in
      routing.dwarf.any) D_ANY="$val" ;;
      routing.dwarf.low) D_LO="$val" ;;
      routing.dwarf.medium) D_MD="$val" ;;
      routing.dwarf.high) D_HI="$val" ;;
      routing.qa.any) Q_ANY="$val" ;;
      routing.qa.low) Q_LO="$val" ;;
      routing.qa.medium) Q_MD="$val" ;;
      routing.qa.high) Q_HI="$val" ;;
      routing.planner) PLANNER="$val" ;;
      policy.shadow_qa) SHADOW_QA="$val" ;;
      policy.shadow_rate) SHADOW_RATE="$val" ;;
      policy.explore) EXPLORE="$val" ;;
      policy.fast) forge_fast_parse "$val" plan; FAST_GIVEN=1 ;;
    esac
  done <<EOF
$out
EOF
  forge_profile_export "$PLAN"
}

# What `run` reads from the frozen profile when its flags do not say: the retry budgets,
# and the Fractal planner (only with --fractal-auto-decompose, which --fractal-planner needs).
forge_profile_run_values() { # forge_profile_run_values <plan-dir>
  local key val arg auto=0 planner=0
  [ -f "$1/profile.json" ] || return 0
  for arg in ${FRACTAL_ARGS[@]+"${FRACTAL_ARGS[@]}"}; do
    case "$arg" in --fractal-auto-decompose) auto=1 ;; --fractal-planner) planner=1 ;; esac
  done
  while IFS="$(printf '\t')" read -r key val; do
    case "$key" in
      policy.retry_failed) [ "$PP_RETRY_FAILED_SET" = 1 ] || PP_RETRY_FAILED="$val" ;;
      policy.infra_retries) [ "$PP_INFRA_RETRIES_SET" = 1 ] || PP_INFRA_RETRIES="$val" ;;
      routing.fractal_planner) [ "$auto$planner" != 10 ] || FRACTAL_ARGS+=(--fractal-planner "$val") ;;
    esac
  done <<EOF
$(forge_profile_py value "$1/profile.json" policy.retry_failed policy.infra_retries routing.fractal_planner 2>/dev/null)
EOF
}

# --- retro, brief and summaries (#132, #135, #133): options frozen into the plan -------
# `plan` writes six plan files (retro_mode, brief, brief_scope, brief_model, summaries,
# summaries_model) that forge_side.settings(plan) reads at run time. The retro file is not
# called `retro`: <plan>/retro/ is where the notes are kept. For each, a flag of this call
# wins, then what the plan already has (a re-plan without the flag keeps it, as
# --reviewer-session is kept), then the user `features` setting, read here once. A file is
# written for a feature that is on, and as `off` for one switched off by a flag (or an earlier
# flag), so a later change to the user setting cannot turn it on in this plan. With nothing on
# and no explicit off, no file is written.
#
# forge_features_resolve sets FEAT_* and writes nothing; it returns 2 (FEAT_ERR says why) when
# a feature is on with no model, or a model spec does not resolve. forge_features_store writes
# the files and says what retro records the first time. An empty value means "no file".
forge_features_user() { # prints key=value lines: the stored user settings, defaults when unreadable
  PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY' 2>/dev/null || true
import forge_side
f = forge_side.stored_features()
for k in ('retro', 'brief', 'brief.scope', 'brief.model', 'summaries', 'summaries.model'):
    print('%s=%s' % (k, f.get(k) or ''))
PY
}

forge_feature_file() { # forge_feature_file <plan> <name> -> first line, trimmed; empty when absent
  local line=""
  if [ -f "$1/$2" ] && [ -r "$1/$2" ]; then IFS= read -r line < "$1/$2" || true; fi
  line="${line#"${line%%[![:space:]]*}"}"
  printf '%s' "${line%"${line##*[![:space:]]}"}"
}

forge_feature_spec_ok() { # forge_feature_spec_ok <flag name> <spec>: dispatch's own dry run, as a --qa spec resolves
  local tmp err rc=0
  tmp="$(mktemp -d)" || { FEAT_ERR="could not make a temporary directory"; return 2; }
  mkdir -p "$tmp/repo" "$tmp/run"; echo spec-check > "$tmp/prompt"
  bash "$SKILL_DIR/scripts/forge-dispatch.sh" side "$2" --repo "$tmp/repo" --run-dir "$tmp/run" \
    --prompt-file "$tmp/prompt" --dry-run >/dev/null 2>"$tmp/err" || rc=$?
  err="$(awk 'NF { line = $0 } END { print line }' "$tmp/err" 2>/dev/null)"
  rm -rf "${tmp:?}"
  [ "$rc" -eq 0 ] || { FEAT_ERR="$1: '$2' does not resolve to a model${err:+ ($err)}"; return 2; }
}

forge_features_resolve() { # forge_features_resolve <plan>
  local plan="$1" key val
  local u_retro=off u_brief=off u_scope=task u_bmodel="" u_summ=off u_smodel=""
  while IFS== read -r key val; do
    case "$key" in
      retro) [ "$val" = on ] && u_retro=on ;;
      brief) [ -n "$val" ] && u_brief="$val" ;;
      brief.scope) [ -n "$val" ] && u_scope="$val" ;;
      brief.model) u_bmodel="$val" ;;
      summaries) [ -n "$val" ] && u_summ="$val" ;;
      summaries.model) u_smodel="$val" ;;
    esac
  done <<FEATURES_EOF
$(forge_features_user)
FEATURES_EOF
  local f_retro f_brief f_scope f_bmodel f_summ f_smodel
  f_retro="$(forge_feature_file "$plan" retro_mode)"; f_brief="$(forge_feature_file "$plan" brief)"
  f_scope="$(forge_feature_file "$plan" brief_scope)"; f_bmodel="$(forge_feature_file "$plan" brief_model)"
  f_summ="$(forge_feature_file "$plan" summaries)"; f_smodel="$(forge_feature_file "$plan" summaries_model)"

  # retro: `on`; `off` when this call or the plan said so; no file otherwise
  local retro="${RETRO_FLAG:-${f_retro:-$u_retro}}"
  FEAT_RETRO=""
  case "$retro" in on) FEAT_RETRO=on ;; *) if [ "$RETRO_FLAG" = off ] || [ "$f_retro" = off ]; then FEAT_RETRO=off; fi ;; esac

  # brief: the level, its scope and its model
  local level="${BRIEF_FLAG:-${f_brief:-$u_brief}}"
  case "$level" in advisory|gated|strict|off) ;; *) level=off ;; esac
  FEAT_BRIEF=""; FEAT_BRIEF_SCOPE=""; FEAT_BRIEF_MODEL=""
  if [ "$level" = off ]; then
    if [ "$BRIEF_FLAG" = off ] || [ "$f_brief" = off ]; then FEAT_BRIEF=off; fi
  else
    local scope="${BRIEF_SCOPE_FLAG:-${f_scope:-$u_scope}}" bmodel="${BRIEF_MODEL_FLAG:-${f_bmodel:-$u_bmodel}}"
    case "$scope" in task|wave|plan) ;; *) scope=task ;; esac
    if [ -z "$bmodel" ]; then
      FEAT_ERR="brief is $level but has no model: pass --brief-model SPEC or run 'forge config set brief.model SPEC'"
      return 2
    fi
    FEAT_BRIEF="$level"; FEAT_BRIEF_SCOPE="$scope"; FEAT_BRIEF_MODEL="$bmodel"
  fi

  # summaries: the mode and its model
  local mode="${SUMM_FLAG:-${f_summ:-$u_summ}}"
  case "$mode" in on|off) ;; *) mode=off ;; esac
  FEAT_SUMM=""; FEAT_SUMM_MODEL=""
  if [ "$mode" = off ]; then
    if [ "$SUMM_FLAG" = off ] || [ "$f_summ" = off ]; then FEAT_SUMM=off; fi
  else
    local smodel="${SUMM_MODEL_FLAG:-${f_smodel:-$u_smodel}}"
    if [ -z "$smodel" ]; then
      FEAT_ERR="summaries is on but has no model: pass --summaries-model SPEC or run 'forge config set summaries.model SPEC'"
      return 2
    fi
    FEAT_SUMM=on; FEAT_SUMM_MODEL="$smodel"
  fi

  # Every model that will be dispatched resolves now, not at its first dispatch.
  if [ -n "$FEAT_BRIEF_MODEL" ]; then forge_feature_spec_ok --brief-model "$FEAT_BRIEF_MODEL" || return 2; fi
  if [ -n "$FEAT_SUMM_MODEL" ] && [ "$FEAT_SUMM_MODEL" != "$FEAT_BRIEF_MODEL" ]; then
    forge_feature_spec_ok --summaries-model "$FEAT_SUMM_MODEL" || return 2
  fi
  return 0
}

# The shared flags (--qa-threshold, ...) write into the plan dir as they are parsed, before the
# features above can be settled. A refusal puts the plan dir's top-level files back as they were.
forge_plan_snapshot() { # forge_plan_snapshot <plan>: prints the snapshot dir
  local snap f
  snap="$(mktemp -d)" || return 1
  for f in "$1"/*; do
    if [ -f "$f" ] && [ ! -L "$f" ]; then cp -p "$f" "$snap/" 2>/dev/null || true; fi
  done
  printf '%s' "$snap"
}

forge_plan_restore() { # forge_plan_restore <plan> <snapshot dir>
  local f
  for f in "$1"/*; do
    if [ -f "$f" ] && [ ! -e "$2/${f##*/}" ]; then rm -f "$f"; fi
  done
  for f in "$2"/*; do
    if [ -f "$f" ]; then cp -p "$f" "$1/" 2>/dev/null || true; fi
  done
}

forge_features_store() { # forge_features_store <plan>
  local plan="$1" pair name var val
  for pair in retro_mode:FEAT_RETRO brief:FEAT_BRIEF brief_scope:FEAT_BRIEF_SCOPE brief_model:FEAT_BRIEF_MODEL \
              summaries:FEAT_SUMM summaries_model:FEAT_SUMM_MODEL; do
    name="${pair%%:*}"; var="${pair#*:}"; val="${!var}"
    if [ -z "$val" ]; then
      rm -f "${plan:?}/${name:?}"
    else
      printf '%s\n' "$val" > "$plan/$name" || die "could not write $plan/$name" 3
    fi
  done
  if [ "$FEAT_RETRO" = on ]; then forge_retro_notice "$plan"; fi
  return 0
}

# Said once per user, to stderr, the first time a plan has retro on. The marker is in the state dir.
forge_retro_notice() { # forge_retro_notice <plan>
  local marker="${XDG_STATE_HOME:-$HOME/.local/state}/forge/retro.noticed"
  [ ! -e "$marker" ] || return 0
  note "retro is on. Each agent records what got in its way: a note with the command it ran and an excerpt of the error."
  note "  Kept in $1/retro/, in tasks/<id>/retro.json and in the local dataset. Nothing leaves this machine"
  note "  unless you approve it with 'forge retro issues'. This notice is shown once per user."
  { mkdir -p "${marker%/*}" && : > "$marker"; } 2>/dev/null || true
}

# One line per feature that is on, from the plan files (what the run will do).
forge_features_table() { # forge_features_table <plan>
  local plan="$1" level scope model
  if [ "$(forge_feature_file "$plan" retro_mode)" = on ]; then
    echo "retro: agents record what got in their way; report at retro.md (local only)"
  fi
  level="$(forge_feature_file "$plan" brief)"
  case "$level" in
    advisory|gated|strict)
      scope="$(forge_feature_file "$plan" brief_scope)"; model="$(forge_feature_file "$plan" brief_model)"
      echo "brief: $level, ${scope:-task} scope, ${model:-no model}; one cheap read-only dispatch per task before it starts" ;;
  esac
  if [ "$(forge_feature_file "$plan" summaries)" = on ]; then
    echo "summaries: on, $(forge_feature_file "$plan" summaries_model); one cheap dispatch per task when its attempts or status change"
  fi
  return 0
}

do_plan() {
  local PLAN="${1:?plan needs a plan dir}"; shift
  local PF_NAME="" PF_NONE=0 PF_QT=0
  forge_profile_precheck "$PLAN" "$@"
  local REPO="" D_ANY="" D_HI="" D_MD="" D_LO="" Q_ANY="" Q_HI="" Q_MD="" Q_LO=""
  PP_INFRA_FALLBACK=""
  local PLANNER="" NOMEM=0 jev_rc=0 DLEVEL="" SHADOW_QA="" SHADOW_RATE="" EXPLORE="" FAIL_FIRST="" REVIEWER_SESSION="" IMPLEMENTER_SESSION="" PRE_REVIEW="" PRE_REVIEW_PARALLEL=""
  local FAST_GIVEN=0 FAST_PARSED=""
  local RETRO_FLAG="" BRIEF_FLAG="" BRIEF_SCOPE_FLAG="" BRIEF_MODEL_FLAG="" SUMM_FLAG="" SUMM_MODEL_FLAG=""
  local COLLAPSE=0 COLLAPSED_NOW=0 FEAT_ERR="" FEAT_SNAP=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)         REPO="${2:?}"; shift 2 ;;
      --planner)      PLANNER="${2:?}"; shift 2 ;;
      --decompose-level)
        case "${2:-}" in low|medium|high) DLEVEL="$2"; shift 2 ;;
          *) die "--decompose-level must be low, medium or high" ;; esac ;;
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --no-memory)    NOMEM=1; shift ;;
      --profile)      PF_NAME="${2:?--profile needs a profile name}"; shift 2 ;;
      --no-profile)   PF_NONE=1; shift ;;
      --shadow-qa)
        [ -n "${2:-}" ] || die "--shadow-qa needs a QA spec, a comma list of specs, or none"
        SHADOW_QA="$2"; shift 2 ;;
      --shadow-rate)
        printf '%s' "${2:-}" | grep -Eq '^(0?\.[0-9]+|0|1|1\.0+)$' || die "--shadow-rate needs a share between 0 and 1"
        SHADOW_RATE="$2"; shift 2 ;;
      --explore)      EXPLORE="${2:?--explore needs a share between 0 and 0.5}"; shift 2 ;;
      --collapse-chains) COLLAPSE=1; shift ;;
      --dwarf)        D_ANY="${2:?}"; shift 2 ;;
      --dwarf-high)   D_HI="${2:?}"; shift 2 ;;
      --dwarf-medium) D_MD="${2:?}"; shift 2 ;;
      --dwarf-low)    D_LO="${2:?}"; shift 2 ;;
      --qa)           Q_ANY="${2:?}"; shift 2 ;;
      --qa-high)      Q_HI="${2:?}"; shift 2 ;;
      --qa-medium)    Q_MD="${2:?}"; shift 2 ;;
      --qa-low)       Q_LO="${2:?}"; shift 2 ;;
      --ui|--no-ui)   forge_ui_flag "$1"; shift ;;
      --fail-first)   FAIL_FIRST=on; shift ;;
      --no-fail-first) FAIL_FIRST=off; shift ;;
      --pre-review-tests)    PRE_REVIEW=on; shift ;;
      --no-pre-review-tests) PRE_REVIEW=off; shift ;;
      --pre-review-parallel)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,2}$' && [ "$2" -ge 1 ] && [ "$2" -le 64 ] \
          || die "--pre-review-parallel needs an integer from 1 to 64" 2
        PRE_REVIEW_PARALLEL="$((10#$2))"; shift 2 ;;
      --reviewer-session)    REVIEWER_SESSION=on; shift ;;
      --no-reviewer-session) REVIEWER_SESSION=off; shift ;;
      --implementer-session)    IMPLEMENTER_SESSION=on; shift ;;
      --no-implementer-session) IMPLEMENTER_SESSION=off; shift ;;
      --fast)
        [ -n "${2:-}" ] || die "--fast needs a comma list of roles (dwarf, qa, planner, review), all or none"
        forge_fast_parse "$2" plan
        FAST_GIVEN=1
        shift 2 ;;
      --retro)        RETRO_FLAG=on; shift ;;
      --no-retro)     RETRO_FLAG=off; shift ;;
      --brief)
        case "${2:-}" in advisory|gated|strict|off) BRIEF_FLAG="$2"; shift 2 ;;
          *) die "--brief must be advisory, gated, strict or off" ;; esac ;;
      --brief-scope)
        case "${2:-}" in task|wave|plan) BRIEF_SCOPE_FLAG="$2"; shift 2 ;;
          *) die "--brief-scope must be task, wave or plan" ;; esac ;;
      --brief-model)
        [ -n "${2:-}" ] || die "--brief-model needs a model spec"
        BRIEF_MODEL_FLAG="$2"; shift 2 ;;
      --summaries)
        case "${2:-}" in on|off) SUMM_FLAG="$2"; shift 2 ;;
          *) die "--summaries must be on or off" ;; esac ;;
      --summaries-model)
        [ -n "${2:-}" ] || die "--summaries-model needs a model spec"
        SUMM_MODEL_FLAG="$2"; shift 2 ;;
      *)
        # `case` is not a loop, so `continue` here belongs to the enclosing while.
        # The shared flags write into the plan dir (--qa-threshold), so it must exist first.
        [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
        [ -n "$FEAT_SNAP" ] || FEAT_SNAP="$(forge_plan_snapshot "$PLAN")" || FEAT_SNAP=""
        if forge_pp_flag plan "$PLAN" "$@"; then
          [ "$1" != --qa-threshold ] || PF_QT=1
          shift "$PP_SHIFT"; continue
        fi
        if [ "$JEV_OPTS" = 1 ]; then
          forge_jev_flag "$@"; jev_rc=$?
          case "$jev_rc" in
            0) shift "$JEV_SHIFT"; continue ;;
            2) exit 2 ;;
          esac
        fi
        die "plan: unknown option '$1'"
        ;;
    esac
  done
  [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
  # The retro, brief and summaries choices, settled before anything is written: a refusal
  # (no model, a spec that does not resolve) leaves the plan dir as it was.
  if ! forge_features_resolve "$PLAN"; then
    if [ -n "$FEAT_SNAP" ]; then forge_plan_restore "$PLAN" "$FEAT_SNAP"; rm -rf "${FEAT_SNAP:?}"; fi
    die "$FEAT_ERR" 2
  fi
  if [ -n "$FEAT_SNAP" ]; then rm -rf "${FEAT_SNAP:?}"; FEAT_SNAP=""; fi
  # The profile (#48) first: its gates and budget count for every decision-model step below,
  # and its values stand in for the flags this call did not give.
  forge_profile_plan
  [ "$JEV_OPTS" = 1 ] && forge_jev_export
  # Freeze the selection next to the task table, so run and retry restore it rather
  # than re-deciding. Written on every plan: a re-plan is a new decision.
  [ "$JEV_OPTS" = 1 ] && forge_jev_record "$PLAN"
  local T="$PLAN/tasks.tsv"
  [ -r "$T" ] || die "no tasks.tsv in '$PLAN' — decompose the goal first"
  # The table as the planner or orchestrator wrote it, before Jev or route resolution
  # rewrites any column: which tier a task was DECLARED is a label of its own.
  cp "$T" "$PLAN/tasks.declared.tsv"
  # A lateral move on an infrastructure stop is on for a new plan; one without the file stays off. A re-plan
  # keeps what the plan has, as it keeps its routing files: only --no-infra-fallback changes it here.
  if [ -n "$PP_INFRA_FALLBACK" ] || [ ! -f "$PLAN/infra_fallback" ]; then
    printf '%s\n' "${PP_INFRA_FALLBACK:-on}" > "$PLAN/infra_fallback" || die "could not write $PLAN/infra_fallback"
  fi
  [ -z "$DLEVEL" ] || printf '%s\n' "$DLEVEL" > "$PLAN/decompose_level"
  # Shadow reviews are part of the plan, like its routing: frozen here, shown in the table
  # and read by every task process. `none` turns them off again.
  case "$SHADOW_QA" in
    '') ;;
    none|off) rm -f "$PLAN/shadow_qa" "$PLAN/shadow_rate" "$PLAN/shadow_qa.ok" ;;
    *) printf '%s\n' "$SHADOW_QA" > "$PLAN/shadow_qa" ;;
  esac
  [ -z "$SHADOW_RATE" ] || [ "$SHADOW_QA" = none ] || [ "$SHADOW_QA" = off ] \
    || printf '%s\n' "$SHADOW_RATE" > "$PLAN/shadow_rate"
  # Fail-first evidence (opt-in, advisory) is part of the plan too: every task's review gets it.
  case "$FAIL_FIRST" in on) : > "$PLAN/fail_first" ;; off) rm -f "$PLAN/fail_first" ;; esac
  # Pre-review tests (opt-in, #85): each attempt's affected tests run before its review, and a
  # red result goes back to the implementer without one.
  case "$PRE_REVIEW" in on) : > "$PLAN/pre_review_tests" ;; off) rm -f "$PLAN/pre_review_tests" ;; esac
  # How many checks run at once across the plan's tasks (forge_pre_review's slots; default 2).
  [ -z "$PRE_REVIEW_PARALLEL" ] || printf '%s\n' "$PRE_REVIEW_PARALLEL" > "$PLAN/pre_review_parallel"
  # Reviewer-session continuation (opt-in experiment): the file holds `on`. Readers only
  # test that it exists; the dataset records its content.
  case "$REVIEWER_SESSION" in on) printf '%s\n' on > "$PLAN/reviewer_session" ;; off) rm -f "$PLAN/reviewer_session" ;; esac
  # Implementer-session continuation (opt-in, #90): the same `on` file, read the same way.
  case "$IMPLEMENTER_SESSION" in on) printf '%s\n' on > "$PLAN/implementer_session" ;; off) rm -f "$PLAN/implementer_session" ;; esac
  # Which roles ask for the fast tier. Omitted on a re-plan, so an earlier choice stays.
  forge_fast_store "$PLAN"
  # Retro, brief and summaries (#132, #133, #135), as forge_features_resolve settled them.
  forge_features_store "$PLAN"
  # Exploration is decided here and only here, so the table and preflight see what will run.
  # On by default: choosing models from measured pass rates needs some randomized picks, and
  # history had almost none (9 tasks ever ran on more than one spec). The flag wins, then
  # this plan's earlier choice, then FORGE_ROUTE_EXPLORE, then 0.2. A 0 is written down too,
  # so a later re-plan never quietly turns exploration back on.
  if [ -z "$EXPLORE" ]; then
    if [ -f "$PLAN/explore" ]; then EXPLORE="$(cat "$PLAN/explore")"
    elif [ -n "${FORGE_ROUTE_EXPLORE:-}" ]; then EXPLORE="$FORGE_ROUTE_EXPLORE"
    else EXPLORE=0.2; fi
  fi
  awk -v v="$EXPLORE" 'BEGIN { exit !(v ~ /^(0|0?\.[0-9]+)$/ && v + 0 <= 0.5) }' \
    || die "--explore (or FORGE_ROUTE_EXPLORE) needs a share between 0 and 0.5"
  printf '%s\n' "$EXPLORE" > "$PLAN/explore"
  if awk -v v="$EXPLORE" 'BEGIN { exit !(v + 0 == 0) }'; then EXPLORE=""; fi

  # repo: from flag on first plan, remembered afterwards
  if [ -n "$REPO" ]; then
    [ -d "$REPO" ] || die "--repo '$REPO' is not a directory"
    (cd "$REPO" && git rev-parse --git-dir >/dev/null 2>&1) \
      || die "'$REPO' is not a git repository. Decomposed runs need worktrees, and there is no honest fallback without them." 3
    (cd "$REPO" && pwd) > "$PLAN/repo"
  fi
  [ -s "$PLAN/repo" ] || die "plan needs --repo the first time"
  REPO="$(cat "$PLAN/repo")"
  # The repository the checkout belongs to, recorded once: a plan that has none gets one now.
  forge_repo_identity "$REPO" "$PLAN"
  # Before any decision-model step: the privacy floor and the run budget read these.
  [ "$JEV_OPTS" = 1 ] && forge_decide_env "$REPO" "$PLAN"
  [ -s "$PLAN/run_id" ] || basename "$PLAN" | sed 's/^forge-//' > "$PLAN/run_id"
  # Every plan, --ui or not: forge-ui's view of this repo's runs reads the registry.
  forge_ui_register "$PLAN"
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  # Persist plan choices for later run and retry processes.
  [ "$NOMEM" = 1 ] && : > "$PLAN/no_memory"
  [ -n "$PLANNER" ] && printf '%s\n' "$PLANNER" > "$PLAN/planner"
  python3 - "$PLAN/fractal-routing.json" "$D_ANY" "$D_LO" "$D_MD" "$D_HI" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text()) if p.exists() else {}
for key,value in zip(('any','low','medium','high'),sys.argv[2:]):
    if value: data[key]=[s.strip() for s in value.split(',') if s.strip()]
p.write_text(json.dumps(data))
PY
  # The reviewer pools, kept as the dwarf pools are: the round-robin below forgets them, and a review
  # stopped on infrastructure moves to a peer of its pool (forge-route.py qa-fallback).
  python3 - "$PLAN/qa-routing.json" "$Q_ANY" "$Q_LO" "$Q_MD" "$Q_HI" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text()) if p.exists() else {}
for key,value in zip(('any','low','medium','high'),sys.argv[2:]):
    if value: data[key]=[s.strip() for s in value.split(',') if s.strip()]
p.write_text(json.dumps(data))
PY

  # Uncommitted work is invisible to every dwarf, because worktrees branch from a
  # commit. That is the likeliest way to get a confusingly wrong result, so it is
  # stated loudly rather than left for the user to deduce from an odd diff.
  if [ -n "$(cd "$REPO" && git status --porcelain 2>/dev/null)" ]; then
    note "WARNING: '$REPO' has uncommitted changes. Task worktrees branch from HEAD,"
    note "         so those edits will NOT be visible to any dwarf. Commit or stash first"
    note "         if the tasks depend on them."
  fi

  # --- jev: plan-time gates ------------------------------------------------------
  # Prompt adequacy and drift: warnings about the task text and its declared files. Drift
  # `on` adds the files it predicts to tasks.tsv (inside score-plan); `shadow` only records.
  local jev_loaded=0
  if [ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ]; then
    source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && jev_loaded=1
  fi
  if [ "$jev_loaded" = 1 ] && { forge_decide_active prompt || forge_decide_active drift; }; then
    local jev_scores jev_rc
    jev_scores="$(python3 "$SKILL_DIR/scripts/forge-jev.py" score-plan \
      --plan "$PLAN" --repo "$REPO" 2>/dev/null)"
    jev_rc=$?
    # A gate in shadow is asked and recorded by score-plan, which leaves it out of
    # these rows: what is printed here is only what an `on` gate has to say.
    # --jev-shadow before migration keeps its gates `on` and prints a count for the
    # warnings, as it always did; what a gate ADDED is still said, whatever the flag.
    if [ "$jev_rc" -eq 0 ] && [ -n "$jev_scores" ]; then
      local jid jwarn jdrift
      # The columns score-plan prints: id, warnings, drift. Read in lockstep with
      # cmd_score_plan.
      while IFS="$(printf '\t')" read -r jid jwarn jdrift; do
        [ -n "$jid" ] || continue
        if [ "${FORGE_JEV_SHADOW:-}" != on ]; then
          case "$jwarn" in
            *prompt_adequacy*) note "jev: $jid — the prompt may be too vague for QA to judge correctness against" ;;
          esac
        fi
        if [ -n "$jdrift" ] && [ "$jdrift" != "-" ]; then
          # The drift field only names files score-plan ADDED (drift `on`): a shadowed gate
          # prints '-', so there is no "may edit undeclared files" case left to hint at.
          note "drift added to $jid: $jdrift"
        fi
      done <<EOF
$jev_scores
EOF
      if [ "${FORGE_JEV_SHADOW:-}" = on ]; then
        note "jev: scored $(printf '%s\n' "$jev_scores" | grep -c .) task(s) in shadow mode (no change)"
      fi
    fi
  fi

  # Coverage: does some task's acceptance criteria deliver each requirement of request.md?
  # `on` marks the ones no task covers pending in plan-gates.json; the send-back file below
  # says which, so nothing is printed here.
  if [ "$jev_loaded" = 1 ] && forge_decide_active coverage; then
    python3 "$SKILL_DIR/scripts/forge-jev.py" coverage --plan "$PLAN" --repo "$REPO" >/dev/null 2>&1 || true
  fi

  # The one send-back step of this call, after every pre-resolve gate: prompt and coverage
  # mark what to rewrite, and exit 4 hands it back before validation, waves and the table.
  # An item is sent back once; the re-plan lists what is still wrong under the table.
  if [ "$jev_loaded" = 1 ] && { forge_decide_active prompt || forge_decide_active coverage; }; then
    local sendback_out sendback_rc=0 sendback_n
    sendback_out="$(python3 "$SKILL_DIR/scripts/forge-plan-check.py" sendback "$PLAN" 2>/dev/null)" || sendback_rc=$?
    if [ "$sendback_rc" -eq 4 ]; then
      printf '%s\n' "$sendback_out"
      sendback_n="$(printf '%s\n' "$sendback_out" | grep -c '^- ')"
      note "plan gates sent $sendback_n item(s) back: rewrite them as $PLAN/plan.sendback.md says, then run plan again (once)"
      return 4
    fi
  fi

  # --- collapse a chain, then resolve routing ---
  # --collapse-chains acts on this call only. The gates have already run (drift may have
  # added files). Validate, then collapse, then routing fills combined's `-` columns, and
  # the declared copy is taken again so the declared tier is the combined task's. A second
  # collapse (tasks.precollapse.tsv or collapsed.json already there) is not an error.
  if [ "$COLLAPSE" = 1 ]; then
    local vline vid vdeps vdiff vd
    while IFS= read -r vline; do
      case "$vline" in ''|'#'*) continue ;; esac
      vid="$(printf '%s' "$vline" | awk -F'\t' '{print $1}')"
      vdeps="$(printf '%s' "$vline" | awk -F'\t' '{print $2}')"
      vdiff="$(printf '%s' "$vline" | awk -F'\t' '{print $3}')"
      [ -n "$vid" ] || die "tasks.tsv has a row with no id"
      case "$vdiff" in low|medium|high) ;; *) die "task '$vid' has difficulty '$vdiff' (expected low, medium or high)" ;; esac
      if [ "$vdeps" != "-" ]; then
        for vd in $(printf '%s' "$vdeps" | tr ',' ' '); do
          all_ids "$T" | grep -qx "$vd" || die "task '$vid' depends on unknown task '$vd'"
        done
      fi
    done < "$T"
    local collapse_out collapse_err collapse_rc=0 collapse_msg
    collapse_err="$PLAN/.collapse.err"
    collapse_out="$(python3 "$SKILL_DIR/scripts/forge-plan-check.py" collapse "$PLAN" 2>"$collapse_err")" || collapse_rc=$?
    collapse_msg="$(cat "$collapse_err" 2>/dev/null || true)"
    rm -f "$collapse_err"
    if [ "$collapse_rc" -ne 0 ]; then
      case "$collapse_msg" in
        *already\ exists*) ;;
        *) die "could not collapse the plan${collapse_msg:+: $collapse_msg}" ;;
      esac
    else
      case "$collapse_out" in
        collapsed\ *)
          cp "$T" "$PLAN/tasks.declared.tsv" || die "could not rewrite $PLAN/tasks.declared.tsv"
          COLLAPSED_NOW=1
          ;;
      esac
    fi
  fi

  local tmp="$PLAN/.tasks.tsv.new"; : > "$tmp"
  local hi_i=0 md_i=0 lo_i=0 an_i=0 qhi_i=0 qmd_i=0 qlo_i=0 qan_i=0 unassigned=0
  printf '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n' > "$tmp"
  local id deps diff files dw qa title line
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    id="$(printf '%s' "$line" | awk -F'\t' '{print $1}')"
    deps="$(printf '%s' "$line" | awk -F'\t' '{print $2}')"
    diff="$(printf '%s' "$line" | awk -F'\t' '{print $3}')"
    files="$(printf '%s' "$line" | awk -F'\t' '{print $4}')"
    dw="$(printf '%s' "$line" | awk -F'\t' '{print $5}')"
    qa="$(printf '%s' "$line" | awk -F'\t' '{print $6}')"
    title="$(printf '%s' "$line" | awk -F'\t' '{print $7}')"
    [ -n "$id" ] || die "tasks.tsv has a row with no id"
    case "$diff" in low|medium|high) ;; *) die "task '$id' has difficulty '$diff' (expected low, medium or high)" ;; esac
    # deps must exist
    if [ "$deps" != "-" ]; then
      local d
      for d in $(printf '%s' "$deps" | tr ',' ' '); do
        all_ids "$T" | grep -qx "$d" || die "task '$id' depends on unknown task '$d'"
      done
    fi
    # resolve dwarf. UNASSIGNED is a marker from a previous plan, not a user
    # choice, so it must be re-resolved rather than preserved like a real override.
    [ "$dw" = "UNASSIGNED" ] && dw="-"
    if [ "$dw" = "-" ] || [ -z "$dw" ]; then
      case "$diff" in
        high)   if [ -n "$D_HI" ]; then dw="$(pool_pick "$D_HI" "$hi_i")"; hi_i=$((hi_i+1)); fi ;;
        medium) if [ -n "$D_MD" ]; then dw="$(pool_pick "$D_MD" "$md_i")"; md_i=$((md_i+1)); fi ;;
        low)    if [ -n "$D_LO" ]; then dw="$(pool_pick "$D_LO" "$lo_i")"; lo_i=$((lo_i+1)); fi ;;
      esac
      if [ "$dw" = "-" ] || [ -z "$dw" ]; then
        if [ -n "$D_ANY" ]; then dw="$(pool_pick "$D_ANY" "$an_i")"; an_i=$((an_i+1));
        else dw="UNASSIGNED"; unassigned=$((unassigned+1)); fi
      fi
      # Exploration (--explore): now and then the neighbouring tier's model instead, with the
      # odds recorded, so `forge dataset routing` can compare two models on tasks neither was
      # routed to by design. The pool's turn above was still taken, so other rows are unchanged.
      # Explored or not, the pick's tier is recorded (running.tier), so an escalation or pin that
      # this re-plan replaces no longer says which tier a lateral move stays in (#106).
      if [ "$dw" != UNASSIGNED ]; then
        local assigned
        assigned="$(python3 "$SKILL_DIR/scripts/forge-route.py" assign --plan "$PLAN" --task "$id" \
          --tier "$diff" --epsilon "${EXPLORE:-0}" --default "$dw" --pools "$PLAN/fractal-routing.json" 2>/dev/null)"
        [ -z "$assigned" ] || dw="$assigned"
      fi
    fi
    # resolve qa: the same grammar as the dwarf, a comma-list round-robins within its tier.
    # Written verbatim, a list reached dispatch as one spec ('opus,sol' is not a known
    # alias) and every review of the run failed before a model was called.
    if [ "$qa" = "-" ] || [ -z "$qa" ]; then
      case "$diff" in
        high)   if [ -n "$Q_HI" ]; then qa="$(pool_pick "$Q_HI" "$qhi_i")"; qhi_i=$((qhi_i+1)); fi ;;
        medium) if [ -n "$Q_MD" ]; then qa="$(pool_pick "$Q_MD" "$qmd_i")"; qmd_i=$((qmd_i+1)); fi ;;
        low)    if [ -n "$Q_LO" ]; then qa="$(pool_pick "$Q_LO" "$qlo_i")"; qlo_i=$((qlo_i+1)); fi ;;
      esac
      if [ "$qa" = "-" ] || [ -z "$qa" ]; then
        if [ -n "$Q_ANY" ]; then qa="$(pool_pick "$Q_ANY" "$qan_i")"; qan_i=$((qan_i+1)); else qa="opus"; fi
      fi
    fi
    # A column runs one spec; a list there would only fail later, at its first dispatch.
    case "$dw" in *,*) die "task '$id': the dwarf column names one spec, not a list ('$dw'); pool models with a --dwarf flag" ;; esac
    case "$qa" in *,*) die "task '$id': the qa column names one spec, not a list ('$qa'); pool reviewers with a --qa flag" ;; esac
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$deps" "$diff" "$files" "$dw" "$qa" "$title" >> "$tmp"
  done < "$T"
  mv "$tmp" "$T"
  # Which cells routing filled in, so a re-plan under another profile replaces those and no others.
  # Kept current through a --no-profile re-plan too, once a profile has recorded any.
  if [ -f "$PLAN/profile.json" ] || [ -f "$PLAN/routed.json" ]; then
    forge_profile_py routed "$PLAN" 2>/dev/null || true
  fi

  compute_waves "$PLAN"

  # --- jev: forward dependency ----------------------------------------------------
  # Wave order is structural, but it cannot see an earlier-wave task written against
  # an interface a later-wave task builds. Forward `on` adds that dep to tasks.tsv (inside
  # `coupling`, refusing a cycle), so the waves are computed again below when it did;
  # otherwise the gate only advises. It runs after compute_waves and before the table.
  if [ "$jev_loaded" = 1 ] && forge_decide_active forward; then
    local jev_coupling jev_coupling_rc
    jev_coupling="$(python3 "$SKILL_DIR/scripts/forge-jev.py" coupling \
      --plan "$PLAN" --repo "$REPO" 2>/dev/null)"
    jev_coupling_rc=$?
    # A shadowed forward gate is recorded in jev-coupling.json and prints no row; --jev-shadow
    # before migration counts the advisory rows, as it always did. Rows an `on` gate acted
    # on (`added`, `cycle`) are said and acted on whatever the flag.
    if [ "$jev_coupling_rc" -eq 0 ] && [ -n "$jev_coupling" ]; then
      local ca cb cp ckind dep_added=0 advisory=0
      while IFS="$(printf '\t')" read -r ca cb cp ckind; do
        [ -n "$ca" ] || continue
        # An earlier task written against work that has not been built yet, which its own
        # review cannot catch. The last column says what `on` did about it.
        case "$ckind" in
          added)
            dep_added=1
            note "forward: task '$ca' now waits for '$cb' ($cp) — it may depend on what '$cb' builds" ;;
          cycle)
            note "forward: '$ca' may depend on '$cb' ($cp), but that dependency would form a cycle — not added" ;;
          *)
            advisory=$((advisory+1))
            [ "${FORGE_JEV_SHADOW:-}" = on ] ||
              note "jev: task '$ca' runs before '$cb' and may depend on what it builds ($cp) — consider a dep" ;;
        esac
      done <<EOF
$jev_coupling
EOF
      if [ "$advisory" -gt 0 ] && [ "${FORGE_JEV_SHADOW:-}" = on ]; then
        note "jev: found $advisory coupled pair(s) in shadow mode (no change)"
      fi
      if [ "$dep_added" = 1 ]; then compute_waves "$PLAN"; fi
    fi
  fi

  # After the last wave computation (a forward dep may have just changed it). Advisory:
  # a failure is a note, and the table is still printed.
  local shape_err="" shape_rc=0
  shape_err="$(python3 "$SKILL_DIR/scripts/forge-plan-check.py" shape "$PLAN" --write 2>&1 >/dev/null)" || shape_rc=$?
  if [ "$shape_rc" -ne 0 ]; then
    note "plan shape could not be recorded (advisory)${shape_err:+: $shape_err}"
  fi

  render_table "$PLAN"
  # What the gates did, from plan-gates.json: files and deps they added, prompts and
  # requirements still flagged. Prints nothing when no gate recorded anything.
  python3 "$SKILL_DIR/scripts/forge-plan-check.py" gates "$PLAN" 2>/dev/null || true
  # Advisory, under the table: a spec measurably slower than a lower effort of the same model,
  # once per distinct spec (doctor --spec says the same). Read from the dataset for the
  # operator's screen only, so not by forge-plan-check.py, which builds a prompt.
  python3 "$SKILL_DIR/scripts/forge-dataset.py" slow-notes "$PLAN" 2>/dev/null || true
  # Advisory, under the table: a privacy floor that silences every gate, and which
  # gates this plan's runs will ask. Prints nothing when it cannot tell.
  [ "$jev_loaded" = 1 ] && forge_decide_plan_lines "${FORGE_DECIDE_REPO:-$REPO}"
  # Before the UNASSIGNED return, so the URL prints under the table either way.
  forge_ui_launch "$PLAN"

  if [ "$unassigned" -gt 0 ]; then
    note ""
    note "$unassigned task(s) are UNASSIGNED: their difficulty tier has no --dwarf-<tier>"
    note "rule and there is no plain --dwarf fallback. Assign them before running —"
    note "forge will not pick a model that spends your quota on your behalf."
    return 2
  fi
  return 0
}

# --- waves --------------------------------------------------------------------
# A wave is the largest set of tasks whose deps are already satisfied AND whose
# file sets are pairwise disjoint. Disjointness is what actually bounds
# parallelism: two dwarves editing one file produce a conflict no reviewer can
# untangle, so they get separate waves no matter how the goal was decomposed.
compute_waves() {
  # Written to temp files and renamed only on success: a cycle leaves the previous files.
  local PLAN="$1" T="$1/tasks.tsv" W="$1/.waves.tsv.$$" D="$1/.deferrals.txt.$$"
  : > "$W"; : > "$D"
  local remaining done_ids wave n id deps files wave_files ok d progressed
  remaining="$(all_ids "$T" | tr '\n' ' ')"
  done_ids=" "; wave=0
  while [ -n "${remaining// /}" ]; do
    wave=$((wave+1)); wave_files=""; progressed=0
    local next_remaining=""
    for id in $remaining; do
      deps="$(field "$T" "$id" deps)"
      files="$(field "$T" "$id" files)"
      ok=1
      if [ "$deps" != "-" ]; then
        for d in $(printf '%s' "$deps" | tr ',' ' '); do
          case "$done_ids" in *" $d "*) ;; *) ok=0 ;; esac
        done
      fi
      if [ "$ok" = 1 ] && lists_overlap "$files" "$wave_files"; then
        ok=0
        printf 'wave %s: %s deferred (file overlap with a task already in this wave)\n' \
          "$wave" "$id" >> "$D"
      fi
      if [ "$ok" = 1 ]; then
        printf '%s\t%s\n' "$wave" "$id" >> "$W"
        wave_files="$wave_files $(printf '%s' "$files" | tr ',' ' ')"
        progressed=1
      else
        next_remaining="$next_remaining $id"
      fi
    done
    [ "$progressed" = 1 ] || { rm -f "$W" "$D"; die "dependency cycle or unsatisfiable deps among:$next_remaining"; }
    for id in $(awk -F'\t' -v w="$wave" '$1==w {print $2}' "$W"); do done_ids="$done_ids$id "; done
    remaining="$next_remaining"
  done
  mv -f "$D" "$PLAN/deferrals.txt" && mv -f "$W" "$PLAN/waves.tsv"
}

render_table() {
  local PLAN="$1" T="$1/tasks.tsv" W="$1/waves.tsv"
  local run_id; run_id="$(cat "$PLAN/run_id")"
  local n; n="$(all_ids "$T" | wc -l | tr -d ' ')"
  local waves; waves="$(awk -F'\t' '{print $1}' "$W" | sort -u | wc -l | tr -d ' ')"
  echo "run forge/$run_id   repo $(cat "$PLAN/repo")"
  [ -s "$PLAN/goal.txt" ] && echo "goal: $(head -1 "$PLAN/goal.txt")"
  local est=$((n*2)) extra=""
  if [ -s "$PLAN/planner" ]; then
    est=$((est+1)); extra=" + 1 planner ($(cat "$PLAN/planner"))"
  fi
  echo "tasks: $n   waves: $waves   estimated dispatches: $est ($n dwarf + $n qa$extra)"
  if [ -s "$PLAN/shadow_qa" ]; then
    local rate; rate="$(cat "$PLAN/shadow_rate" 2>/dev/null || echo 0.1)"
    echo "shadow review: $(cat "$PLAN/shadow_qa") on a sampled $rate of reviewed attempts" \
      "(about $(awk -v n="$n" -v r="$rate" 'BEGIN { x = n * r; printf "%d", (x == int(x)) ? x : int(x) + 1 }') extra reviews; recorded, never changes a result)"
  fi
  if [ -f "$PLAN/fail_first" ]; then
    if [ -s "$(cat "$PLAN/repo")/.forge/verify-subset" ]; then
      echo "fail-first: each task's new tests also run without its change (advice for the reviewer; never changes a result)"
    else
      echo "fail-first: on, but $(cat "$PLAN/repo")/.forge/verify-subset (a test command with {files}) is missing, so nothing will run"
    fi
  fi
  if [ -f "$PLAN/pre_review_tests" ]; then
    if [ -s "$(cat "$PLAN/repo")/.forge/verify-subset" ]; then
      echo "pre-review tests: on — each attempt's affected tests run before its review; a red attempt goes back to its implementer unreviewed"
    else
      echo "pre-review tests: on, but $(cat "$PLAN/repo")/.forge/verify-subset (a test command with {files} or {changed}) is missing, so nothing will run"
    fi
  fi
  if [ -f "$PLAN/reviewer_session" ]; then
    echo "reviewer-session: a later review round continues the reviewer's earlier session when its QA spec is unchanged (experiment; falls back to a fresh session)"
  fi
  if [ -f "$PLAN/implementer_session" ]; then
    echo "implementer-session: a retry continues the implementer's earlier session when its spec is unchanged and its work is as the review left it (claude, openclaude; falls back to a fresh session)"
  fi
  if [ -s "$PLAN/fast" ]; then
    local fast_roles
    fast_roles="$(awk 'NF { printf "%s%s", (n++ ? ", " : ""), $0 }' "$PLAN/fast")"
    echo "fast tier: $fast_roles — billed at a higher rate (codex service_tier=fast; claude fast mode needs extra usage enabled)"
  fi
  forge_features_table "$PLAN"
  if [ -f "$PLAN/profile.json" ]; then
    local profile_line; profile_line="$(forge_profile_py line "$PLAN/profile.json" 2>/dev/null)"
    [ -z "$profile_line" ] || echo "$profile_line"
  fi
  # The plan-level threshold: at plan time no per-task file exists yet.
  local qa_level qa_src
  read -r qa_level qa_src <<EOF
$(qa_threshold_walk "$PLAN" "")
EOF
  case "$qa_level" in
    P3) echo "QA threshold: P3 ($qa_src) — every CONFIRMED finding blocks" ;;
    P0) echo "QA threshold: P0 ($qa_src) — P0 blocks; a CONFIRMED P1–P3 is recorded as a known issue" ;;
    P1) echo "QA threshold: P1 ($qa_src) — P0–P1 block; a CONFIRMED P2–P3 is recorded as a known issue" ;;
    *)  echo "QA threshold: P2 ($qa_src) — P0–P2 block; a CONFIRMED P3 is recorded as a known issue" ;;
  esac
  # A reviewer spec that names no effort reviews at forge-dispatch.sh's QA default (#89).
  if awk -F'\t' '$0 !~ /^#/ && NF >= 6 { split($6, s, ":"); if (s[2] == "") found = 1 } END { exit !found }' "$T"; then
    echo "QA effort: high where a reviewer spec names none (forge-dispatch.sh's default; planning stays at xhigh)"
  fi
  echo
  local w id dws starred=0
  for w in $(awk -F'\t' '{print $1}' "$W" | sort -un); do
    local cnt; cnt="$(awk -F'\t' -v w="$w" '$1==w' "$W" | wc -l | tr -d ' ')"
    echo "wave $w ($cnt parallel)"
    printf '  %-14s %-7s %-24s %-14s %s\n' id diff dwarf qa files
    for id in $(awk -F'\t' -v w="$w" '$1==w {print $2}' "$W"); do
      dws="$(field "$T" "$id" dwarf)"
      if [ -s "$PLAN/tasks/$id/route.explored" ] && [ "$(cat "$PLAN/tasks/$id/route.explored")" = "$dws" ]; then
        dws="$dws*"; starred=1
      fi
      printf '  %-14s %-7s %-24s %-14s %s\n' \
        "$id" "$(field "$T" "$id" difficulty)" "$dws" \
        "$(field "$T" "$id" qa)" "$(field "$T" "$id" files)"
      # The approach is shown here because this gate is the only moment a human can
      # change the plan for free. After dispatch, changing it costs a whole run.
      if [ -s "$PLAN/tasks/$id/approach.md" ]; then
        sed 's/^/                 | /' "$PLAN/tasks/$id/approach.md"
      fi
    done
    echo
  done
  # A chain collapsed on this call, or a chain left as a chain. one and branches say nothing.
  # COLLAPSED_NOW is do_plan's: collapsed.json from an earlier plan is not announced again.
  local shape_note=""
  if [ "${COLLAPSED_NOW:-0}" = 1 ] && [ -f "$PLAN/collapsed.json" ]; then
    shape_note="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("collapsed %d" % len(d.get("from") or []))' "$PLAN/collapsed.json" 2>/dev/null)" || shape_note=""
  elif [ -f "$PLAN/plan-shape.json" ]; then
    shape_note="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("%s %s" % (d.get("shape"), d.get("tasks")))' "$PLAN/plan-shape.json" 2>/dev/null)" || shape_note=""
  fi
  case "$shape_note" in
    collapsed\ *)
      echo "collapsed a sequential plan of ${shape_note#collapsed } tasks into one (tasks.precollapse.tsv keeps the original)"
      echo ;;
    chain\ *)
      echo "this plan runs sequentially: ${shape_note#chain } tasks, at most 1 in parallel; each pays its own implementation and review. One task would run once: re-plan with --collapse-chains"
      echo ;;
  esac
  if [ "$starred" = 1 ]; then
    echo "* picked by exploratory routing (--explore $(cat "$PLAN/explore" 2>/dev/null)) instead of the pool's turn;"
    echo "  to pin another model, put its spec in the dwarf column of tasks.tsv and re-plan"
    echo
  fi
  local explore drawn=0 decisions; explore="$(cat "$PLAN/explore" 2>/dev/null)"
  # Said only when a row was actually drawn (route.jsonl): with every row named, or no tier
  # with a neighbour, there is no spend to show. LC_ALL=C: a comma-decimal locale prints "0,1".
  for decisions in "$PLAN"/tasks/*/route.jsonl; do [ -e "$decisions" ] && { drawn=1; break; }; done
  if [ -n "$explore" ] && [ "$drawn" = 1 ] && LC_ALL=C awk -v v="$explore" 'BEGIN { exit !(v + 0 > 0) }'; then
    echo "exploration on (--explore $explore): a row left to forge gets its neighbouring tier's model with"
    echo "  probability $(LC_ALL=C awk -v v="$explore" 'BEGIN { print v / 2 }'), odds logged for \`forge dataset routing\`; --explore 0 turns it off for this plan"
    echo
  fi
  report_size "$PLAN"
  if [ -s "$PLAN/deferrals.txt" ]; then
    echo "deferrals (serialized to protect the merge):"
    sed 's/^/  /' "$PLAN/deferrals.txt"
    echo
  fi
}

# --- capsule ------------------------------------------------------------------
# Every dispatch is a clean slate, which keeps one bad turn from poisoning another
# task but leaves each dwarf blind to the wider run. The capsule is the fix, and it
# is regenerated per dispatch because status changes as waves land. Two failures it
# exists to prevent: reimplementing something an earlier task already merged, and
# editing a file another dwarf currently owns.
task_status() { # task_status <plan> <id>
  local PLAN="$1" id="$2"
  [ -f "$PLAN/tasks/$id/merged" ] && { echo MERGED; return; }
  [ -f "$PLAN/tasks/$id/status" ] && { cat "$PLAN/tasks/$id/status"; return; }
  echo PENDING
}

dependencies_ready() {
  local plan="$1" id="$2" dep
  for dep in $(field "$plan/tasks.tsv" "$id" deps | tr ',' ' '); do
    [ "$dep" = - ] && continue
    if [ "$(task_status "$plan" "$dep")" != MERGED ]; then
      echo BLOCKED > "$plan/tasks/$id/status"
      note "$id: BLOCKED by $dep ($(task_status "$plan" "$dep"))"
      return 1
    fi
  done
}

preflight_plan() {
  local plan="$1" id role flags ft spec key seen=";"
  export FORGE_CAPABILITY_CACHE="$plan/capabilities"
  for id in $(all_ids "$plan/tasks.tsv"); do
    for role in dwarf qa; do
      flags=""; [ -f "$plan/yolo_$role" ] && flags=--yolo
      ft="$(forge_fast_tier "$plan" "$role")"
      spec="$(field "$plan/tasks.tsv" "$id" "$role")"
      # Once per spec, role and options: the same arguments get the same answer, and a note doctor
      # adds (a slow effort) is said once, not once per task.
      key="$role|$spec|$flags|$ft"
      case "$seen" in *";$key;"*) continue ;; esac
      seen="$seen$key;"
      /bin/bash "$DISPATCH" doctor --spec "$spec" --role "$role" $flags $ft || return $?
    done
  done
  if [ -s "$plan/planner" ]; then
    ft="$(forge_fast_tier "$plan" planner)"
    /bin/bash "$DISPATCH" doctor --spec "$(cat "$plan/planner")" --role planner $ft || return $?
  fi
  # A shadow reviewer that cannot run here is dropped with a note, never a failed run.
  # A fast-tier refusal is the exception: dispatch already refused it, and skipping the
  # shadow would let the rest of the run be billed. That exit stops the run.
  if [ -s "$plan/shadow_qa" ]; then
    python3 "$SKILL_DIR/scripts/forge-shadow.py" preflight --plan "$plan" || return $?
  fi
}

# The implementer a sweep task gets when the plan has a low tier (or a plain --dwarf pool): checked
# with the rest, before any model is paid for, since no task may have used it yet.
sweep_preflight() { # sweep_preflight <plan>
  local spec flags="" ft
  spec="$(python3 -c 'import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
for tier in ("low", "any"):
    specs = data.get(tier) if isinstance(data, dict) else None
    if isinstance(specs, list) and specs and isinstance(specs[0], str) and specs[0]:
        print(specs[0]); break' "$1/fractal-routing.json" 2>/dev/null)"
  [ -n "$spec" ] || return 0
  [ -f "$1/yolo_dwarf" ] && flags=--yolo
  ft="$(forge_fast_tier "$1" dwarf)"
  /bin/bash "$DISPATCH" doctor --spec "$spec" --role dwarf $flags $ft
}

sweep_plan() { # sweep_plan <plan> --severity Pn [--plausible] [--max N]: the scheduler's _sweep
  python3 "$SKILL_DIR/scripts/forge-sweep.py" plan "$@"
}

write_capsule() { # write_capsule <plan> <id> <role>
  local PLAN="$1" me="$2" role="$3" T="$1/tasks.tsv"
  local run_id; run_id="$(cat "$PLAN/run_id")"
  local n; n="$(all_ids "$T" | wc -l | tr -d ' ')"
  # Per task, NOT one shared file. Tasks in a wave run concurrently, so a single
  # $PLAN/capsule.md is a race: each task overwrites it and then reads back
  # whichever sibling wrote last. That silently hands a dwarf someone else's task
  # description — observed in testing before this was split per task.
  local out="$PLAN/tasks/$me/capsule.md" id st
  {
    echo "# forge run $run_id"
    [ -s "$PLAN/goal.txt" ] && { echo; echo "Overall goal: $(cat "$PLAN/goal.txt")"; }
    echo
    echo "This run is split into $n tasks worked by different models. You are handling ONE of"
    echo "them. Do not do the other tasks' work."
    echo
    if [ "$role" = qa ]; then
      echo "## Under review"
    else
      echo "## Your task"
    fi
    echo "$me — $(field "$T" "$me" title)   (difficulty: $(field "$T" "$me" difficulty))"
    echo "Files this task owns: $(field "$T" "$me" files)"
    echo
    echo "## Run status"
    printf '%-14s %-7s %-9s %s\n' id diff status files
    for id in $(all_ids "$T"); do
      if [ "$id" = "$me" ]; then st="THIS ONE"
      elif [ -s "$PLAN/tasks/$id/reviewed.commit" ] && git -C "$(cat "$PLAN/repo")" merge-base --is-ancestor "$(cat "$PLAN/tasks/$id/reviewed.commit")" "$(cat "$PLAN/tasks/$me/base_ref")"; then st=MERGED
      else st="NOT IN BASE"; fi
      printf '%-14s %-7s %-9s %s\n' "$id" "$(field "$T" "$id" difficulty)" "$st" "$(field "$T" "$id" files)"
    done
    echo
    echo "## Ground rules"
    echo "- Your branch already contains every MERGED task's work. Do not reimplement it."
    echo "- MERGED means present in this task's pinned baseline; later merges are not included."
    echo "- The review diff is only this task's work against that baseline."
    echo "- Implementation must respect other tasks' ownership; report necessary scope changes."
    if [ -s "$PLAN/tasks/$me/submodules.txt" ]; then
      local sm_st sm_path
      while IFS="$(printf '\t')" read -r sm_st sm_path _; do
        if [ "$sm_st" = missing ]; then
          echo "- Submodule \`$sm_path\` is not available in this worktree (its directory is empty)."
        else
          echo "- \`$sm_path\` is a submodule: its files are here to read and build with. Changes there are not part of your change and are never committed or reviewed."
        fi
      done < "$PLAN/tasks/$me/submodules.txt"
    fi

  } > "$out"
  echo "$out"
}

# --- one task -----------------------------------------------------------------
# Idempotent by construction: it may be entered on a fresh task, on a resumed run
# whose worktree already exists, or on a retry of a task that already failed once.
# The original version created the worktree unconditionally, which made a
# decomposed run a one-shot — a failed task's preserved branch had nothing that
# could act on it, and an interrupted run could not be continued.
# A fresh worktree is a bare checkout: no node_modules, no venv, no build output,
# nothing a package manager put there. Every dwarf in the run then rediscovers that
# the same way — by running the test command, watching it fail, and working out the
# install step — and pays it again. In one observed 6-task run that tax showed up as
# four separate "this worktree has no node_modules" entries in project memory and
# minutes per dispatch.
#
# Forge deliberately does NOT guess the fix. The obvious guess, symlinking the source
# repo's node_modules into the worktree, is actively wrong in a workspace monorepo:
# the workspace links inside it point back at the source checkout, so the worktree
# silently builds against the source repo's packages instead of its own, and the
# resulting type errors name files the task never touched. Copying is correct but can
# be gigabytes per task.
#
# So the repo says what its worktrees need, and forge runs it verbatim: `.forge/setup`
# (executable, or any file whose contents are a shell command), or `--setup <command>`
# to override. It runs once per worktree, from the worktree root, only on creation —
# a resumed run or a retry reuses the existing worktree and does not pay it again.
# Always succeeds: "no setup configured" is the normal case, not a failure. Returning
# the status of the last test instead would make the common path report an error, and
# under a caller running `set -e` that aborts the task before the worktree is usable.
worktree_setup_command() { # worktree_setup_command <plan> <repo>
  local PLAN="$1" REPO="$2"
  if [ -s "$PLAN/setup_cmd" ]; then cat "$PLAN/setup_cmd"
  elif [ -f "$REPO/.forge/setup" ]; then cat "$REPO/.forge/setup"
  fi
  return 0
}

# Exports each submodule's files into a fresh worktree (offline, no `.git`, so they stay
# invisible to every diff and fingerprint). Never fatal: a failure leaves the directory empty.
populate_submodules() { # populate_submodules <repo> <wt> <tdir> <id>
  local repo="$1" wt="$2" tdir="$3" id="$4" st path _sha detail err
  rm -f "$tdir/submodules.txt"
  err="$(python3 "$SKILL_DIR/scripts/forge-submodules.py" populate --source "$repo" --worktree "$wt" \
    --out "$tdir/submodules.txt" 2>&1 >/dev/null)" \
    || note "$id: submodule population failed (continuing): $(printf '%s' "$err" | tail -1)"
  [ -s "$tdir/submodules.txt" ] || return 0
  while IFS="$(printf '\t')" read -r st path _sha detail; do
    [ "$st" = missing ] && note "$id: submodule $path is not available in this worktree ($detail)"
  done < "$tdir/submodules.txt"
  return 0
}

# Non-fatal by design. A setup step that fails leaves a worktree the dwarf can still
# work in — it just has to install things itself, which is the status quo this exists
# to improve on. Failing the task instead would turn a slow run into a dead one.
run_worktree_setup() { # run_worktree_setup <plan> <repo> <wt> <tdir> <id>
  local PLAN="$1" REPO="$2" wt="$3" tdir="$4" id="$5" cmd
  # Every worktree forge makes reaches here once, right after it exists, so the submodules'
  # files are in place before `.forge/setup` runs. The outcome lands in <tdir>/submodules.txt.
  populate_submodules "$REPO" "$wt" "$tdir" "$id"
  cmd="$(worktree_setup_command "$PLAN" "$REPO")"
  [ -n "$cmd" ] || return 0
  if (cd "$wt" && eval "$cmd") >"$tdir/setup.out" 2>&1; then
    note "$id: worktree setup ok"
  else
    note "$id: worktree setup failed (continuing) — see $tdir/setup.out"
  fi
}

ensure_worktree() { # ensure_worktree <repo> <wt> <br> <base> <tdir> <plan> <id>
  local REPO="$1" wt="$2" br="$3" base="$4" tdir="$5" PLAN="$6" id="$7"
  # A worktree directory deleted by hand leaves its registration behind, and that
  # registration then blocks `worktree add` with "already exists". Prune first.
  (cd "$REPO" && git worktree prune >/dev/null 2>&1)
  if [ -e "$wt/.git" ]; then
    return 0
  elif (cd "$REPO" && git rev-parse --verify --quiet "$br" >/dev/null 2>&1); then
    (cd "$REPO" && git worktree add "$wt" "$br") >"$tdir/worktree.out" 2>&1
  else
    (cd "$REPO" && git worktree add -b "$br" "$wt" "$base") >"$tdir/worktree.out" 2>&1
  fi || return 1
  forge_worktree_event created "$PLAN" "$REPO" "$wt" "$br" task "$id"
  run_worktree_setup "$PLAN" "$REPO" "$wt" "$tdir" "$id"
}

dispatch_duration() { # dispatch_duration <tdir> <role>
  sed -n 's/^duration_s=//p' "$1/$2.resolved" 2>/dev/null | tail -1
}

# `files` is documented as a promise, not a prediction: it decides which tasks may
# run concurrently and what the capsule declares off limits. Nothing used to check
# it, so an understated set surfaced much later as a merge CONFLICT with no stated
# cause. The answer is free right after the diff capture, so take it there.
#
# A warning, never a failure: a dwarf that genuinely had to touch one more file did
# the right thing, and failing the task for it would be worse than reporting it.
check_drift() { # check_drift <declared-comma-list> <tdir> <wt> <base>
  local declared="$1" tdir="$2" wt="$3" base="$4" touched f d covered
  : > "$tdir/drift.txt"
  touched="$(cd "$wt" && git diff --cached --name-only "$base" -- . ":(exclude).forge" 2>/dev/null)"
  [ -n "$touched" ] || return 0
  for f in $touched; do
    covered=0
    if [ "$declared" != "-" ] && [ -n "$declared" ]; then
      for d in $(printf '%s' "$declared" | tr ',' ' '); do
        # Exact match, or a declared directory prefix: declaring src/routes covers
        # src/routes/api.py, which is how the column is normally written.
        case "$f" in
          "$d"|"$d"/*) covered=1 ;;
        esac
      done
    fi
    [ "$covered" = 0 ] && printf '%s\n' "$f" >> "$tdir/drift.txt"
  done
  [ -s "$tdir/drift.txt" ] || rm -f "$tdir/drift.txt"
  return 0
}

# The attempt trail (#133): one line per attempt that starts with carried findings, saying why it
# happened, as `<attempt>\t<source>\t<epoch>`. The source is read the way forge-prompt.py reads it
# (a missing or unknown one is a review). A line for the same attempt number is replaced rather than
# repeated: an infrastructure stop refunds the attempt, so its restart runs the same number again.
# An existing trail that cannot be read is left as it is (non-zero return): rewriting it from this
# one line would drop the history.
record_retry_source() { # record_retry_source <tdir> <attempt>
  local tdir="$1" n="$2" src trail tmp
  trail="$tdir/retry_sources.tsv"; tmp="$tdir/.retry_sources.tsv.tmp"
  [ -s "$tdir/retry_findings.md" ] || return 0
  src="$(cat "$tdir/retry_findings.source" 2>/dev/null)"
  case "$src" in review|prereview|bounce|message|answer) ;; *) src=review ;; esac
  if [ -e "$trail" ]; then
    [ -f "$trail" ] && [ -r "$trail" ] || return 1
    awk -F'\t' -v n="$n" '$1 != n' "$trail" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  else
    : > "$tmp" || return 1
  fi
  printf '%s\t%s\t%s\n' "$n" "$src" "$(date +%s)" >> "$tmp" && mv "$tmp" "$trail" || { rm -f "$tmp"; return 1; }
}

do_task_body() (
  local PLAN="${1:?}" id="${2:?}"
  local T="$PLAN/tasks.tsv" REPO run_id base wt br tdir attempt resume=""
  local reviewed review source_fp review_fp
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  tdir="$PLAN/tasks/$id"; mkdir -p "$tdir"
  if [ -s "$PLAN/fractal-selection.json" ]; then
    forge_fractal_select "$PLAN" "$REPO" parallel 0 || return $?
  fi
  forge_fast_fractal_note "$PLAN"
  forge_metric_begin "$tdir" task
  dependencies_ready "$PLAN" "$id" || return 1
  echo RUNNING > "$tdir/status"
  rm -f "$tdir/noretry"
  [ -f "$PLAN/no_memory" ] && export FORGE_MEMORY=off
  # --timeout given to run/retry is kept in the plan and wins over the environment.
  [ -s "$PLAN/timeout" ] && export FORGE_TIMEOUT="$(cat "$PLAN/timeout")"
  wt="$(cat "$PLAN/wt_root")/$id"
  br="forge/$run_id/$id"
  # Every attempt gets a distinct number and commit message. A QA-only resume (the
  # reviewer hit an infrastructure failure, or never reached a verdict) re-reviews work
  # that is already committed, so it neither runs the dwarf nor spends an attempt.
  attempt="$(cat "$tdir/attempt" 2>/dev/null || echo 0)"
  if [ -f "$tdir/resume" ]; then
    if resume_valid "$tdir" "$wt"; then
      resume=qa
    else
      rm -f "$tdir/resume"
      note "$id: the saved review state no longer matches the worktree — running the full task"
    fi
  fi
  if [ -z "$resume" ]; then
    attempt=$(( attempt + 1 ))
    echo "$attempt" > "$tdir/attempt"
  fi
  # The gate's decision belongs to the review that made it. A review that ends before the gate
  # (invalidated, timed out, no changes) must not leave an earlier attempt's decision for the
  # next retry note or `accept` to read; prepare_retry has already carried it forward.
  rm -f "$tdir/qa.gate" "$tdir/known_issues.md" "$tdir/invalidated.reason" "$tdir/prereview.failed" \
        "$tdir/bounced" "$tdir/message.failed"

  # The base is pinned per task on first creation and reused by every retry. The
  # integration branch moves as later waves land, so re-reading the run-level
  # base_ref on a retry would pull other tasks' merged code into this task's diff
  # and put it in front of its reviewer as if this dwarf had written it.
  if [ -s "$tdir/base_ref" ]; then
    base="$(cat "$tdir/base_ref")"
  else
    base="$(git -C "$(cat "$PLAN/wt_root")/_integration" rev-parse HEAD)"; printf '%s\n' "$base" > "$tdir/base_ref"
  fi

  local dw qa yd yq rc ft_dwarf ft_qa
  dw="$(field "$T" "$id" dwarf)"; qa="$(field "$T" "$id" qa)"
  yd=""; yq=""
  [ -f "$PLAN/yolo_dwarf" ] && yd="--yolo"
  [ -f "$PLAN/yolo_qa" ] && yq="--yolo"
  # Empty when the role is not listed, so the recorded command line is unchanged.
  ft_dwarf="$(forge_fast_tier "$PLAN" dwarf)"
  ft_qa="$(forge_fast_tier "$PLAN" qa)"

  if ! ensure_worktree "$REPO" "$wt" "$br" "$base" "$tdir" "$PLAN" "$id"; then
    echo ERROR > "$tdir/status"
    note "$id: could not create worktree — $(tail -1 "$tdir/worktree.out")"
    return 1
  fi

  if [ -z "$resume" ]; then
    # Persist each source separately; QA receives complete context once.
    write_capsule "$PLAN" "$id" dwarf >/dev/null
    cp "$tdir/capsule.md" "$tdir/baseline.capsule"
    # --task lets Jev narrow the injected slice to facts that bear on THIS task. Without
    # it, or with Jev off, the full role slice is injected exactly as before.
    /bin/bash "$MEMORY" inject "$REPO" dwarf --task "$tdir/prompt.md" \
        --run-dir "$tdir" > "$tdir/dwarf.memory"
    {
      python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" --retry-source "$(cat "$tdir/retry_findings.source" 2>/dev/null || echo review)" --memory "$tdir/dwarf.memory"
      [ ! -f "$PLAN/sweep_ahead" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" ahead "$PLAN" "$id" --role dwarf 2>/dev/null
      echo "Implement this task in the repository. Follow the intended approach unless it is wrong; explain deviations. Run the task's requested verification and report results."
      echo "On retry, fix the findings in the existing work; preserve parts not criticised. Explain any finding you reject."
      # With repository verification configured, the whole suite runs after the merge, never per attempt.
      echo; python3 "$SKILL_DIR/scripts/forge-contract.py" dwarf $([ ! -f "$PLAN/pre_review_tests" ] || echo --pre-review) \
        $([ -z "$(verify_source_for "$PLAN" "$REPO")" ] || echo --verify-after-merge)
      echo; /bin/bash "$MEMORY" note dwarf
    } > "$tdir/dwarf.input"
    # This attempt starts with the findings just written to dwarf.input (#133).
    record_retry_source "$tdir" "$attempt" || note "$id: retry_sources.tsv not updated; the attempt trail stays as it was"
    guard_before_dwarf "$wt" "$tdir"
    # Whether this attempt continues the implementer's earlier session (plan
    # --implementer-session, #90): only a retry of judged work that is still exactly what was
    # judged, on the harness, model and effort that made it. Every other answer is a fresh
    # dispatch, and nothing here can abort the attempt: a doctor that fails means fresh.
    local is_mode=fresh is_reason="" is_id="" is_fp="" is_cont=()
    if [ -f "$PLAN/implementer_session" ]; then
      if [ -n "${FORGE_FRACTAL_RUN:-}" ] || forge_plan_is_fractal "$PLAN"; then
        is_reason=fractal
      elif [ "$attempt" = 1 ]; then
        is_reason=first-attempt
      elif [ ! -s "$tdir/retry_findings.md" ]; then
        is_reason=no-findings
      elif ! implementer_work_unchanged "$tdir" "$wt"; then
        is_reason=work-changed
      else
        FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$PLAN/capabilities}" \
          /bin/bash "$DISPATCH" doctor --spec "$dw" --role dwarf $yd $ft_dwarf > "$tdir/dwarf.doctor" 2>&1 || : > "$tdir/dwarf.doctor"
        IFS=$'\t' read -r is_mode is_reason is_id < <(review_session_decide \
          "$tdir/retry_findings.dwarf.session" "$tdir/retry_findings.dwarf.resolved" "$tdir/dwarf.doctor" "claude openclaude")
        if [ "$is_mode" = continued ]; then
          is_cont=(--continue-session "$is_id")
          is_fp="$(forge_fingerprint "$wt")" || is_fp=""
        fi
      fi
    fi
    forge_metric_phase dispatch
    local dwarf_cmd=(/bin/bash "$DISPATCH" dwarf "$dw" --repo "$wt" --run-dir "$tdir" \
          --ripwire-query-file "$tdir/prompt.md" --prompt-file "$tdir/dwarf.input" ${yd:+"$yd"} $ft_dwarf --output "${FORGE_OUTPUT:-summary}")
    "${dwarf_cmd[@]}" ${is_cont[@]+"${is_cont[@]}"} >"$tdir/dwarf.out" 2>&1
    rc=$?
    if [ "$rc" = 9 ] && [ "$is_mode" = continued ]; then
      # The session was not continued (the harness cannot, or it failed). Why comes from the
      # continued dispatch's record, which a fresh one clears.
      if [ "$(sed -n 's/^reason=//p' "$tdir/dwarf.continue" 2>/dev/null | head -1)" = unsupported ]; then
        is_reason=continue-unsupported
      else
        is_reason=continue-failed
      fi
      mv "$tdir/dwarf.out" "$tdir/dwarf.continue.out" 2>/dev/null || true
      if [ -n "$is_fp" ] && [ "$(forge_fingerprint "$wt")" = "$is_fp" ]; then
        # It changed nothing, so the same input goes to a fresh session, which decides the attempt.
        is_mode=fresh
        "${dwarf_cmd[@]}" >"$tdir/dwarf.out" 2>&1
        rc=$?
      else
        # It worked on the task before it failed: that is a failed implementer, as when a fresh
        # one fails, and running a fresh one on top would pay for the attempt twice.
        is_mode=failed
        cp "$tdir/dwarf.continue.out" "$tdir/dwarf.out" 2>/dev/null || true
        rc=4
        note "$id: the continued implementer session failed after changing files — not running a fresh one on top"
      fi
    fi
    if [ -f "$PLAN/implementer_session" ]; then
      # One row per attempt, describing the dispatch whose result counts.
      local is_row=1
      [ ! -f "$tdir/implementer_sessions.tsv" ] || is_row=$(( $(wc -l < "$tdir/implementer_sessions.tsv") + 1 ))
      printf '%s\t%s\t%s\t%s\t%s\n' "$is_row" "$attempt" "$is_mode" "$is_reason" "$is_id" >> "$tdir/implementer_sessions.tsv"
    fi
    if [ "${FORGE_OUTPUT:-summary}" = full ]; then cat "$tdir/dwarf.out"; fi
    if [ "$rc" -ne 0 ]; then
      case "$rc" in
        7) echo TIMEOUT > "$tdir/status"; note "$id: dwarf hit the dispatch timeout — see $tdir/dwarf.out" ;;
        8) infra_stop "$tdir" "$id" dwarf "$attempt"; return 8 ;;
        *) echo ERROR > "$tdir/status"; note "$id: dwarf dispatch failed — see $tdir/dwarf.out" ;;
      esac
      return 1
    fi

    /bin/bash "$MEMORY" record "$REPO" --last "$tdir/dwarf.last" --role dwarf \
        --run-id "$run_id" --task "$id" --model "$dw" --run-dir "$tdir" \
        --duration "$(dispatch_duration "$tdir" dwarf)" >/dev/null 2>&1

    guard_after_dwarf "$wt" "$tdir" "$id"
    # Before the snapshot, so a dwarf that only asked a question (and changed nothing) still reaches it.
    forge_post_dwarf_gates "$PLAN" "$id" "$tdir" "$wt" "$attempt" || return 0
    forge_metric_phase snapshot
    # Capture the real diff, including new files, then commit on the task branch.
    # Diffed against the task's pinned base rather than the branch head, so a retry
    # hands QA the task's CUMULATIVE work. Reviewing only the fix would let the
    # first attempt's code through unread.
    # .forge/ is forge's own memory, rewritten in the user's tree after every run.
    # Left in, it would reach QA as if a dwarf had written it, and any task whose
    # files touched it would collide with every other task in the wave planner.
    if [ -z "${FORGE_FRACTAL_RUN:-}" ] || [ "${FORGE_FRACTAL_RETRY:-}" = 1 ]; then
      [ -s "$tdir/changes.diff" ] && mv "$tdir/changes.diff" "$tdir/changes.prev.diff"
    fi
    (cd "$wt" && git add -A -- . ":(exclude).forge" \
        && git diff --cached --binary "$base" -- . ":(exclude).forge") > "$tdir/changes.diff" 2>/dev/null || { echo ERROR > "$tdir/status"; return 1; }
    if [ ! -s "$tdir/changes.diff" ]; then
      # A harness that exited 0 with an empty final message and changed nothing did not
      # decline the task; it never really ran (login, quota, a dropped connection).
      if grep -qx 'class=empty' "$tdir/dwarf.infra" 2>/dev/null; then
        infra_stop "$tdir" "$id" dwarf "$attempt"; return 8
      fi
      echo FAIL > "$tdir/status"
      : > "$tdir/noretry"
      echo "The dwarf produced no changes at all." > "$tdir/qa.last"
      note "$id: dwarf produced an empty diff"; return 1
    fi
    # A bounced attempt (retry_findings.source `bounce`) was never reviewed, so a retry that keeps
    # its code is reviewed rather than refused as identical: the refusal is for a diff a review
    # already judged.
    if [ -s "$tdir/changes.prev.diff" ] && cmp -s "$tdir/changes.diff" "$tdir/changes.prev.diff" \
        && [ "$(cat "$tdir/retry_findings.source" 2>/dev/null)" != bounce ]; then
      echo FAIL > "$tdir/status"
      : > "$tdir/noretry"
      echo "The retry changed nothing: the diff is byte-identical to the previous attempt." \
        > "$tdir/qa.last"
      note "$id: retry produced no new changes — not re-reviewing identical code"; return 1
    fi

    if ! git -C "$wt" diff --cached --quiet "$base" -- .forge; then
      echo FAIL > "$tdir/status"; note "$id: changed excluded Forge memory; cannot accept an unreviewed change"; return 1
    fi
    check_drift "$(field "$T" "$id" files)" "$tdir" "$wt" "$base"
    if [ -s "$tdir/drift.txt" ]; then
      note "$id: touched undeclared file(s): $(tr '\n' ' ' < "$tdir/drift.txt")"
    fi

    (cd "$wt" && git -c user.name=forge -c user.email=forge@local \
        commit --allow-empty -q -m "forge($id) attempt $attempt: $(field "$T" "$id" title)") >/dev/null 2>&1 || {
      echo ERROR > "$tdir/status"; return 1;
    }
    reviewed="$(git -C "$wt" rev-parse HEAD)" || return 1
    git -C "$wt" diff --binary "$base" "$reviewed" -- . ':(exclude).forge' > "$tdir/committed.diff" || return 1
    if ! cmp -s "$tdir/changes.diff" "$tdir/committed.diff" ||
       [ "$(forge_tree "$wt")" != "$(git -C "$wt" rev-parse "$reviewed^{tree}")" ]; then
      echo INVALIDATED > "$tdir/status"; return 1
    fi
    echo "$reviewed" > "$tdir/reviewed.commit"
    # The implementer session that made this commit, and what it ran as: the one a retry of
    # these findings may continue (plan --implementer-session; carried with the findings).
    rm -f "$tdir/reviewed.dwarf.session" "$tdir/reviewed.dwarf.resolved"
    [ ! -s "$tdir/dwarf.session" ] || cp "$tdir/dwarf.session" "$tdir/reviewed.dwarf.session"
    [ ! -s "$tdir/dwarf.resolved" ] || cp "$tdir/dwarf.resolved" "$tdir/reviewed.dwarf.resolved"
    forge_pre_qa_gates "$PLAN" "$id" "$tdir" "$wt" "$attempt" "$reviewed" || return 0
  else
    reviewed="$(cat "$tdir/reviewed.commit")"
  fi
  review="$tdir/review-$attempt-$$"
  forge_review_snapshot "$wt" "$base" "$reviewed" "$review" || { echo ERROR > "$tdir/status"; return 1; }
  forge_review_deps "$wt" "$review" "$tdir/review.deps"
  source_fp="$(forge_fingerprint "$wt")" || return 1
  # The review copy is a commit: an untracked file the reviewer leaves there (a test run's
  # bytecode) changes nothing that merges, so only tracked content counts (#95).
  review_fp="$(forge_fingerprint "$review" --tracked)" || return 1
  echo "$source_fp" > "$tdir/source.fingerprint"
  echo "$review_fp" > "$tdir/review.fingerprint"

  forge_metric_phase preparation
  local qa_thr rs_on=0
  qa_thr="$(qa_threshold_for "$PLAN" "$id")"
  # Read once per round, like the threshold: continuing the earlier reviewer's session is an
  # opt-in experiment (plan --reviewer-session), and a round never changes its mind midway.
  [ -f "$PLAN/reviewer_session" ] && rs_on=1

  # Opt-in (plan --fail-first): do this attempt's new tests fail without its change? Read-only
  # on the review snapshot, so the task worktree is untouched; the reviewer gets the evidence.
  rm -f "$tdir/failfirst.json" "$tdir/failfirst.txt"
  [ ! -f "$PLAN/fail_first" ] || forge_fail_first "$review" "$tdir" "$REPO"
  # Opt-in (plan --pre-review-tests, #85): the tests this attempt affects, before paying for a
  # review. Red: the attempt fails unreviewed and its findings are the test output. A QA-only
  # resume re-reviews work that already passed this check, and keeps its evidence.
  [ -f "$PLAN/pre_review_tests" ] || rm -f "$tdir/prereview.json" "$tdir/prereview.txt" "$tdir/prereview.md"
  if [ -f "$PLAN/pre_review_tests" ] && [ -z "$resume" ] && ! forge_pre_review "$review" "$tdir" "$REPO" "$PLAN"; then
    : > "$tdir/prereview.failed"
    rm -f "$tdir/qa.last"                  # an earlier attempt's review describes other code
    echo FAIL > "$tdir/status"
    note "$id: the tests this attempt affects fail — sent back to the implementer without a review (see $tdir/prereview.md)"
    return 0
  fi
  # What changed since the review whose findings this reviewer is handed. Fractal keeps
  # today's prompt unless it retries, as changes.prev.diff does. dwarf.last is the reply of
  # the attempt under review: a dwarf run just rewrote it, or $resume (the validated QA-only
  # path, never the raw marker) guarantees the reviewed commit is the one it describes.
  local prompt_extra=()
  if [ -z "${FORGE_FRACTAL_RUN:-}" ] || [ "${FORGE_FRACTAL_RETRY:-}" = 1 ]; then
    prompt_extra+=(--role qa)
    attempt_delta "$tdir" "$wt" "$reviewed"
    if [ -s "$tdir/changes.since.diff" ]; then
      prompt_extra+=(--retry-diff "$tdir/changes.since.diff" --retry-stat "$tdir/changes.since.stat"
        --retry-range "$(awk '{ print substr($1, 1, 7) ".." substr($2, 1, 7) }' "$tdir/changes.since.range")")
    fi
    if [ -s "$tdir/retry_findings.md" ] && [ -s "$tdir/dwarf.last" ]; then
      prompt_extra+=(--reply "$tdir/dwarf.last")
    fi
  else
    rm -f "$tdir/changes.since.diff" "$tdir/changes.since.range" "$tdir/changes.since.stat"
  fi
  # A finding sweep's fix task gets the narrow review: one FIXED / NOT FIXED line per finding.
  local qa_kind=task
  [ ! -f "$tdir/sweep.json" ] || qa_kind=sweep
  # QA uses the dispatch-time ownership and baseline, never later merge state.
  /bin/bash "$MEMORY" inject "$REPO" qa --task "$tdir/prompt.md" \
      --run-dir "$tdir" > "$tdir/qa.memory"
  {
    python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" --retry-source "$(cat "$tdir/retry_findings.source" 2>/dev/null || echo review)" ${prompt_extra[@]+"${prompt_extra[@]}"} --memory "$tdir/dwarf.memory" --memory "$tdir/qa.memory"
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part head --kind "$qa_kind"
    echo
    [ ! -f "$PLAN/sweep_ahead" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" ahead "$PLAN" "$id" --role qa 2>/dev/null
    cat "$tdir/review.deps"
    [ -s "$tdir/failfirst.txt" ] || [ -s "$tdir/prereview.txt" ] || echo "Forge ran no tests of its own on this attempt, so nothing here stands in for yours."
    echo
    if [ -s "$tdir/failfirst.txt" ]; then cat "$tdir/failfirst.txt"; echo; fi
    if [ -s "$tdir/prereview.txt" ]; then cat "$tdir/prereview.txt"; echo; fi
    if [ -s "$tdir/guard.txt" ]; then
      # Facts forge observed about how the implementer worked. On a first attempt the reviewer
      # never sees the implementer's own report (a retry's reviewer gets the reply, to check
      # against the code), so this is how an unfinished measurement or a stray process
      # reaches the review.
      echo "Implementer guard notes (observed by forge, not claimed by the implementer):"
      sed 's/^/  - /' "$tdir/guard.txt"
      echo "A promise of later work means a check may never have run; judge the diff on its own merits."
      echo
    fi
    if [ -s "$tdir/drift.txt" ]; then
      # The reviewer is already asked about scope and had no way to know the answer.
      echo "Scope note: this task declared it would touch $(field "$T" "$id" files), and the"
      echo "diff also changes files it did not declare:"
      sed 's/^/  - /' "$tdir/drift.txt"
      echo "That is not automatically wrong — judge whether each was actually necessary. It"
      echo "matters because another task may own those files."
      echo
    fi
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part tail --kind "$qa_kind" --threshold "$qa_thr"
    echo
    # --irreversible-delete: a deleted file shows as a header, not every line it had (one deleted
    # data directory once made a 7.1M-character prompt that the reviewer's CLI refused).
    echo '```diff'; git -C "$wt" diff --binary --irreversible-delete "$base" "$reviewed" -- . ':(exclude).forge'; echo '```'
    echo; /bin/bash "$MEMORY" note qa
    echo "Place any learning notes before the final FORGE_VERDICT: PASS or FORGE_VERDICT: FAIL line."
  } > "$tdir/qa.input"
  # Opt-in (plan --shadow-qa): a second, independent review of this same diff, to measure
  # how often two reviewers agree. It reviews its own snapshot from its own dir under
  # $PLAN/shadow/, and nothing in this script ever reads its result.
  local shadow_pid=""
  if [ -s "$PLAN/shadow_qa" ] && [ -z "${FORGE_FRACTAL_RUN:-}" ]; then
    python3 "$SKILL_DIR/scripts/forge-shadow.py" run --plan "$PLAN" --task "$id" --worktree "$wt" \
        --base "$base" --reviewed "$reviewed" --attempt "$attempt" --qa "$qa" --tdir "$tdir" ${yq:+--yolo} \
        </dev/null >/dev/null 2>&1 &
    shadow_pid=$!
  fi
  forge_metric_phase dispatch
  # Whether this round continues the reviewer's session. Every answer but "continued" is a
  # fresh dispatch, and nothing here can abort the round: a doctor that fails means fresh.
  local rs_mode=fresh rs_reason="" rs_id="" rs_row=1 rs_cont=()
  if [ "$rs_on" = 1 ]; then
    if [ -n "${FORGE_FRACTAL_RUN:-}" ] || forge_plan_is_fractal "$PLAN"; then
      rs_reason=fractal
    elif [ ! -s "$tdir/retry_findings.md" ]; then
      rs_reason=first-round
    else
      FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$PLAN/capabilities}" \
        /bin/bash "$DISPATCH" doctor --spec "$qa" --role qa $yq $ft_qa > "$tdir/qa.doctor" 2>&1 || : > "$tdir/qa.doctor"
      IFS=$'\t' read -r rs_mode rs_reason rs_id < <(review_session_decide \
        "$tdir/retry_findings.session" "$tdir/retry_findings.resolved" "$tdir/qa.doctor")
      [ "$rs_mode" = continued ] && rs_cont=(--continue-session "$rs_id")
    fi
  fi
  local qa_cmd=(/bin/bash "$DISPATCH" qa "$qa" --repo "$review" --run-dir "$tdir" \
        --review-base "$(cat "$tdir/review.base")" --ripwire-query-file "$tdir/prompt.md" --prompt-file "$tdir/qa.input" ${yq:+"$yq"} $ft_qa --output "${FORGE_OUTPUT:-summary}")
  "${qa_cmd[@]}" ${rs_cont[@]+"${rs_cont[@]}"} >"$tdir/qa.out" 2>&1
  rc=$?
  if [ "$rc" = 9 ] && [ "$rs_mode" = continued ]; then
    # The session was not continued (the harness cannot, or it failed). Same input, fresh: the
    # fresh dispatch decides the task. Why comes from the first dispatch's record, which the
    # second one clears.
    if [ "$(sed -n 's/^reason=//p' "$tdir/qa.continue" 2>/dev/null | head -1)" = unsupported ]; then
      rs_reason=continue-unsupported
    else
      rs_reason=continue-failed
    fi
    rs_mode=fresh
    mv "$tdir/qa.out" "$tdir/qa.continue.out" 2>/dev/null || true
    "${qa_cmd[@]}" >"$tdir/qa.out" 2>&1
    rc=$?
  fi
  if [ "$rs_on" = 1 ]; then
    # One row per round, describing the dispatch whose result counts.
    [ ! -f "$tdir/review_sessions.tsv" ] || rs_row=$(( $(wc -l < "$tdir/review_sessions.tsv") + 1 ))
    printf '%s\t%s\t%s\t%s\t%s\n' "$rs_row" "$attempt" "$rs_mode" "$rs_reason" "$rs_id" >> "$tdir/review_sessions.tsv"
  fi
  if [ -n "$shadow_pid" ]; then
    # On every path below, before any status is written: the shadow learns the primary is
    # done (which bounds how much longer it may take), and no shadow outlives its task, so
    # a retry can never start while one is still reviewing. It can delay a task, never change it.
    kill -USR1 "$shadow_pid" 2>/dev/null || true
    wait "$shadow_pid" 2>/dev/null || true
  fi
  if [ "${FORGE_OUTPUT:-summary}" = full ]; then cat "$tdir/qa.out"; fi
  if [ "$rc" -ne 0 ]; then
    case "$rc" in
      7) echo TIMEOUT > "$tdir/status"; note "$id: qa hit the dispatch timeout — see $tdir/qa.out" ;;
      8) infra_stop "$tdir" "$id" qa "$attempt"; return 8 ;;
      *) echo ERROR > "$tdir/status"; note "$id: qa dispatch failed — see $tdir/qa.out" ;;
    esac
    return 1
  fi
  rm -f "$tdir/resume"

  forge_metric_phase verification
  if [ "$(forge_fingerprint "$wt")" != "$source_fp" ] || [ "$(forge_fingerprint "$review" --tracked)" != "$review_fp" ]; then
    # The committed work is untouched, so --retry-failed may review it again (reason `review`);
    # the merge-time invalidations below leave no reason and wait for a human.
    echo review > "$tdir/invalidated.reason"
    echo INVALIDATED > "$tdir/status"; note "$id: artifact changed during review"; return 1
  fi
  local verdict
  verdict="$(forge_verdict "$tdir/qa.last")"
  /bin/bash "$MEMORY" record "$REPO" --last "$tdir/qa.last" --role qa \
      --run-id "$run_id" --task "$id" --model "$qa" --verdict "${verdict:-UNKNOWN}" \
      --run-dir "$tdir" \
      --duration "$(dispatch_duration "$tdir" qa)" >/dev/null 2>&1

  case "$verdict" in
    PASS) echo PASS > "$tdir/status" ;;
    FAIL) echo FAIL > "$tdir/status" ;;
    # No verdict means the reviewer never reached a conclusion. Treating that as a
    # pass would merge unreviewed code, so it is fail-safe by construction.
    *)    echo UNKNOWN > "$tdir/status" ;;
  esac
  # The gate at the effective threshold: a FAIL made only of tolerated findings becomes an
  # accepted PASS-with-known-issues, a PASS that lists a blocking CONFIRMED finding becomes
  # FAIL. qa_thr is the value the review prompt was built with, so the prompt and the gate of
  # one attempt agree. Before record_attempt, so attempts.tsv shows the gated status.
  qa_gate "$PLAN" "$id" "$tdir" "$verdict" "$qa_thr"
  return 0
)

# Entry point for both the scheduler's `_task` and `retry`: runs the pipeline, then logs
# the attempt (including infrastructure stops, which spend nothing) for later audit.
do_task() {
  local rc crc=0 stop_before
  do_task_body "$@"; rc=$?
  record_attempt "$1" "$2"
  # The retry policy's state (escalation.next or escalation.stop), decided now rather than
  # at the next retry so a spent retry budget still reports it and the scheduler sees it.
  stop_before="$(cat "$1/tasks/$2/escalation.stop" 2>/dev/null)"
  python3 "$SKILL_DIR/scripts/forge-route.py" escalation --plan "$1" --task "$2" --check \
    --qa-only-next "$(qa_only_next_flag "$1" "$2")" >/dev/null 2>&1 || crc=$?
  # A FAIL that does not converge, or that the policy could not move to a stronger model, waits
  # for a person (NEEDS_DECISION). attempts.tsv was written above and keeps FAIL.
  decision_after_attempt "$1" "$2" "$stop_before" "$crc"
  return $rc
}


# Merge one passing task onto the integration branch. Shared by `run` and `retry`
# so a retried task lands exactly the way a first-attempt task does.
merge_task() { # merge_task <plan> <id> -> 0 merged, 1 conflicted
  local PLAN="$1" id="$2" REPO run_id int_wt
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  int_wt="$(cat "$PLAN/wt_root")/_integration"
  if [ -s "$PLAN/accepted.integration" ] && [ "$(git -C "$int_wt" rev-parse HEAD)" != "$(cat "$PLAN/accepted.integration")" ]; then
    echo INVALIDATED > "$PLAN/tasks/$id/status"
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=INVALIDATED --set reason=integration-moved
    note "$id: the integration branch moved outside this run. To bring those commits into the run: forge-parallel.sh adopt $PLAN --head --reason \"...\""
    return 1
  fi
  [ -z "$(git -C "$int_wt" status --porcelain -- . ':(exclude).forge')" ] || {
    echo INVALIDATED > "$PLAN/tasks/$id/status"
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=INVALIDATED --set reason=integration-dirty
    return 1;
  }
  local tdir="$PLAN/tasks/$id" reviewed wt
  dependencies_ready "$PLAN" "$id" || return 1
  reviewed="$(cat "$tdir/reviewed.commit")" || return 1
  wt="$(cat "$PLAN/wt_root")/$id"
  if [ "$(forge_fingerprint "$wt")" != "$(cat "$tdir/source.fingerprint")" ] ||
     [ "$(git -C "$REPO" rev-parse "forge/$run_id/$id")" != "$reviewed" ]; then
    echo INVALIDATED > "$tdir/status"
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=INVALIDATED --set reason=task-moved
    return 1
  fi
  if (cd "$int_wt" && git merge --no-ff -m "forge: merge $id" "$reviewed" >/dev/null 2>&1); then
    git -C "$int_wt" rev-parse HEAD > "$PLAN/accepted.integration"
    touch "$PLAN/tasks/$id/merged"
    rm -f "$tdir/conflict.files"
    # Which of a sweep task's findings its reviewer marked fixed: what `forge issues` leaves out.
    [ ! -f "$tdir/sweep.json" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" settle "$PLAN" "$id" >/dev/null 2>&1 || true
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=MERGED --set commit="$reviewed" \
      --set integration="$(cat "$PLAN/accepted.integration")"
    return 0
  fi
  # Which files collided is the label for whether two tasks were coupled, and the abort
  # below erases it, so it is kept first.
  (cd "$int_wt" && git diff --name-only --diff-filter=U) > "$tdir/conflict.files" 2>/dev/null
  (cd "$int_wt" && git merge --abort >/dev/null 2>&1)
  echo CONFLICT > "$PLAN/tasks/$id/status"
  forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=CONFLICT --set commit="$reviewed" \
    --set conflicts="$(tr '\n' ' ' < "$tdir/conflict.files" 2>/dev/null)"
  return 1
}

# --- retry --------------------------------------------------------------------
# The answer arguments of `retry`, found by walking the option list as do_retry's own loop does,
# so the value of another option (--setup '--answer') is never mistaken for one. Run it in a
# subshell with a scratch directory as the plan: the shared option parsers write into it, not
# into the real plan. Leaves n (how many answer flags), flag, val and novalue in the scratch dir.
_retry_answer_scan() { # _retry_answer_scan <scratch-dir> <retry args...>
  local sd="$1"; shift
  local n=0 flag="" val="" novalue=0 rc=0 dwarf="" decide="" reason="" approved=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-ripwire|--yolo-dwarf|--yolo-qa|--no-dispatch) shift ;;
      --output|--verify|--setup) [ $# -ge 2 ] || break; shift 2 ;;
      --dwarf) [ $# -ge 2 ] || break; dwarf="$2"; shift 2 ;;
      --decide) [ $# -ge 2 ] || break; decide="$2"; shift 2 ;;
      --reason) [ $# -ge 2 ] || break; reason="$2"; shift 2 ;;
      --approved) approved=1; shift ;;
      --answer|--answer-file)
        n=$((n + 1)); flag="$1"
        if [ $# -ge 2 ]; then val="$2"; shift 2; else novalue=1; shift; fi ;;
      *)
        if forge_pp_flag retry "$sd" "$@"; then shift "$PP_SHIFT"; continue; fi
        if [ "${JEV_OPTS:-0}" = 1 ]; then
          forge_jev_flag "$@"; rc=$?
          case "$rc" in
            0) shift "$JEV_SHIFT"; continue ;;
            2) exit 2 ;;
          esac
        fi
        break ;;
    esac
  done
  printf '%s' "$n" > "$sd/n"; printf '%s' "$flag" > "$sd/flag"; printf '%s' "$val" > "$sd/val"
  printf '%s' "$dwarf" > "$sd/dwarf"; printf '%s' "$decide" > "$sd/decide"
  printf '%s' "$reason" > "$sd/reason"; printf '%s' "$approved" > "$sd/approved"
  [ "$novalue" = 0 ] || : > "$sd/novalue"
}

# A failed task keeps its branch and worktree "so there is something to inspect and
# retry from" — this is the retry. It is a command a human types, not an automatic
# repair loop, which is what keeps forge's one-dwarf-then-QA rule intact.
# The wait before `retry --infra-retries` dispatches again after an infrastructure stop: the provider's
# retry-after when it gave one, else the plan's infra_backoff (the scheduler's own rule, its functions).
retry_infra_wait() { # retry_infra_wait <plan> <task-dir> <cycle>
  local secs
  secs="$(python3 - "$SKILL_DIR/scripts/forge-schedule.py" "$1" "$2" "$3" <<'PY' 2>/dev/null
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('forge_schedule', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules['forge_schedule'] = module
spec.loader.exec_module(module)
backoff = None
try:
    text = (Path(sys.argv[2]) / 'infra_backoff').read_text().strip()
    backoff = (module.parse_backoff(text), text) if text else None
except (OSError, ValueError):
    pass
print(module.infra_wait(module.infra_info(Path(sys.argv[3])), int(sys.argv[4]), backoff)[0])
PY
)" || secs=""
  case "$secs" in ''|*[!0-9.]*) secs=60 ;; esac
  note "waiting ${secs}s for the infrastructure stop to clear"
  sleep "$secs"
}

report_untested() { # report_untested <plan> <id>...: verdicts that rest on reading alone (#117)
  local plan="$1" id tests_note untested=0; shift
  for id in "$@"; do
    tests_note="$(forge_tests_note "$plan/tasks/$id/qa.last")"
    if [ -n "$tests_note" ]; then
      [ "$untested" = 0 ] && echo "reviews whose tests could not run:"
      untested=1
      printf '  %-14s %s\n' "$id" "$tests_note"
    fi
  done
  [ "$untested" = 0 ] || echo
}

do_retry() {
  local PLAN="${1:?retry needs a plan dir}" id="${2:?retry needs a task id}"; shift 2
  FORGE_RUN_STARTED="$(forge_now)"
  local NEWDWARF="" jev_rc=0 ANSWER="" ANSWER_FILE="" HAVE_ANSWER=0 DECIDE="" REASON="" APPROVED=0 NO_DISPATCH=0
  # An answer is validated before any option below writes into the plan: a rejected answer
  # changes nothing, --setup and --verify included. Only a NEEDS_INPUT task takes one. The
  # arguments are walked the way the loop below walks them (a value is never taken for a flag),
  # in a subshell against a scratch plan dir, so nothing real is written by the walk.
  local pre_dir="$PLAN/.answer-scan.$$" pre_n=0 pre_have="" pre_val="" pre_novalue=0 pre_ok=0
  local pre_dwarf="" pre_decide="" pre_reason="" pre_approved=0
  rm -rf "$pre_dir"
  if mkdir "$pre_dir" 2>/dev/null; then
    if ( _retry_answer_scan "$pre_dir" "$@" ) >/dev/null 2>&1; then
      pre_ok=1
      pre_n="$(<"$pre_dir/n")"; pre_have="$(<"$pre_dir/flag")"; pre_val="$(<"$pre_dir/val")"
      [ ! -f "$pre_dir/novalue" ] || pre_novalue=1
      pre_dwarf="$(<"$pre_dir/dwarf")"; pre_decide="$(<"$pre_dir/decide")"
      pre_reason="$(<"$pre_dir/reason")"; pre_approved="$(<"$pre_dir/approved")"
      # `$(<f)` drops trailing newlines, so an answer of only newlines needs the size check.
      [ -s "$pre_dir/val" ] || pre_val=""
    fi
    rm -rf "$pre_dir"
  else
    die "retry: cannot write to the plan dir $PLAN"
  fi
  if [ "$pre_ok" = 1 ] && [ "$pre_n" != 0 ]; then
    [ "$pre_n" = 1 ] || die "retry: give --answer or --answer-file once"
    [ "$pre_novalue" = 0 ] || die "retry: $pre_have needs a value"
    [ -s "$PLAN/tasks.tsv" ] && all_ids "$PLAN/tasks.tsv" | grep -qx "$id" || die "no task '$id' in this plan"
    local pre_st; pre_st="$(task_status "$PLAN" "$id")"
    [ "$pre_st" = NEEDS_INPUT ] || die "retry: '$id' is $pre_st, not waiting for an answer; --answer and --answer-file only answer a NEEDS_INPUT task"
    if [ "$pre_have" = --answer-file ]; then
      [ -f "$pre_val" ] && [ -r "$pre_val" ] || die "retry: --answer-file '$pre_val' is not a readable file"
      [ -s "$pre_val" ] || die "retry: --answer-file '$pre_val' is empty"
    else
      [ -n "$pre_val" ] || die "retry: the answer is empty"
    fi
  fi
  # A decision is judged the same way, before any option writes: a NEEDS_DECISION task is resumed
  # only by --decide or --dwarf with --reason and --approved, and --decide is for no other task.
  if [ "$pre_ok" = 1 ] && [ -s "$PLAN/tasks.tsv" ] && all_ids "$PLAN/tasks.tsv" | grep -qx "$id"; then
    retry_decision_gate "$PLAN" "$id" "$(task_status "$PLAN" "$id")" "$pre_decide" "$pre_dwarf" "$pre_reason" "$pre_approved"
  fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --output) OUTPUT="${2:?}"; shift 2 ;;
      --dwarf) NEWDWARF="${2:?--dwarf needs a spec}"; shift 2 ;;
      --decide) DECIDE="${2:?--decide needs retry=N or threshold=Pn}"; shift 2 ;;
      --reason) REASON="${2?--reason needs a text}"; shift 2 ;;
      --approved) APPROVED=1; shift ;;
      --no-dispatch) NO_DISPATCH=1; shift ;;
      # The check above refuses an empty answer first; these hold when its walk could not run.
      --answer) [ "$HAVE_ANSWER" = 0 ] || die "retry: give --answer or --answer-file once"
                ANSWER="${2?--answer needs the answer text}"; HAVE_ANSWER=1; shift 2
                [ -n "$ANSWER" ] || die "retry: the answer is empty" ;;
      --answer-file) [ "$HAVE_ANSWER" = 0 ] || die "retry: give --answer or --answer-file once"
                ANSWER_FILE="${2:?--answer-file needs a file}"; HAVE_ANSWER=2; shift 2
                [ -s "$ANSWER_FILE" ] || die "retry: --answer-file '$ANSWER_FILE' is empty or missing" ;;
      --yolo-dwarf) touch "$PLAN/yolo_dwarf"; shift ;;
      --yolo-qa)    touch "$PLAN/yolo_qa"; shift ;;
      --verify)     printf '%s' "${2:?--verify needs a command}" > "$PLAN/verify_cmd"; rm -rf "$PLAN/verify.d"; PP_LANES_FRESH=0; shift 2 ;;
      --setup)      printf '%s' "${2:?--setup needs a command}" > "$PLAN/setup_cmd"; shift 2 ;;
      *)
        # `case` is not a loop, so `continue` here belongs to the enclosing while.
        if forge_pp_flag retry "$PLAN" "$@"; then shift "$PP_SHIFT"; continue; fi
        if [ "$JEV_OPTS" = 1 ]; then
          forge_jev_flag "$@"; jev_rc=$?
          case "$jev_rc" in
            0) shift "$JEV_SHIFT"; continue ;;
            2) exit 2 ;;
          esac
        fi
        die "retry: unknown option '$1'"
        ;;
    esac
  done
  forge_profile_run_values "$PLAN"
  [ "$JEV_OPTS" = 1 ] && forge_jev_settle "$PLAN"
  # Before any decision-model step: the privacy floor and the run budget read these.
  [ "$JEV_OPTS" = 1 ] && [ -s "$PLAN/repo" ] && forge_decide_env "$(cat "$PLAN/repo")" "$PLAN"
  case "$OUTPUT" in summary|full) ;; *) die "--output must be summary or full" ;; esac
  export FORGE_OUTPUT="$OUTPUT"
  local T="$PLAN/tasks.tsv" tdir="$PLAN/tasks/$id"
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'"
  all_ids "$T" | grep -qx "$id" || die "no task '$id' in this plan"
  [ -s "$PLAN/wt_root" ] || die "this plan has never been run — use 'run', not 'retry'"
  local st; st="$(task_status "$PLAN" "$id")"
  [ "$st" = "MERGED" ] && die "task '$id' already merged — nothing to retry"
  # A decision's grant is exactly what it says, and the profile's default budget of further attempts
  # does not stretch it: a task waiting for a decision (resumed by --decide or --dwarf), or an
  # infrastructure stop resumed while a decision's count is kept. Only a --retry-failed typed on this
  # line adds to a decision.
  if [ "$PP_RETRY_FAILED_SET" != 1 ] && { [ -n "$DECIDE" ] || [ "$st" = NEEDS_DECISION ] \
       || { [ "$st" = INFRA ] && [ -n "$(decided_left "$tdir")" ]; }; }; then
    PP_RETRY_FAILED=0
  fi
  [ "$HAVE_ANSWER" = 0 ] || [ "$st" = NEEDS_INPUT ] || die "retry: '$id' is $st, not waiting for an answer; --answer and --answer-file only answer a NEEDS_INPUT task"
  # The budgets are this call's own, and --no-dispatch ends the call before any attempt spends them.
  if [ "$NO_DISPATCH" = 1 ] && { [ "$PP_RETRY_FAILED_SET" = 1 ] || [ "$PP_INFRA_RETRIES_SET" = 1 ]; }; then
    die "retry: --retry-failed and --infra-retries apply to the attempts this call runs, and --no-dispatch runs none; give them to the run: forge-parallel.sh run $PLAN --retry-failed N" 2
  fi
  # A recorded decision is already what the next run does: a second one would stack onto its count.
  if [ -n "$DECIDE" ] && [ -f "$tdir/retry.prepared" ]; then
    die "retry: '$id' already has a decision recorded and waits for the next run; forge-parallel.sh run $PLAN starts it" 2
  fi
  retry_decision_gate "$PLAN" "$id" "$st" "$DECIDE" "$NEWDWARF" "$REASON" "$APPROVED"
  # A task at its attempt cap (#69) is refused with the decision commands unless --decide retry=N reaches past it.
  if ! attempt_cap_gate "$PLAN" "$id" "$st" "$DECIDE"; then write_results "$PLAN"; return 2; fi
  # The user's answer to a NEEDS_INPUT task's question (validated above): written before
  # anything else is prepared, atomically, and kept when the preparation then refuses (the
  # same answer can be retried).
  if [ "$HAVE_ANSWER" != 0 ]; then
    if [ "$HAVE_ANSWER" = 2 ]; then
      cp "$ANSWER_FILE" "$tdir/.answer.md.tmp" || { rm -f "$tdir/.answer.md.tmp"; die "retry: could not write the answer" 1; }
    else
      printf '%s\n' "$ANSWER" > "$tdir/.answer.md.tmp" || { rm -f "$tdir/.answer.md.tmp"; die "retry: could not write the answer" 1; }
    fi
    mv "$tdir/.answer.md.tmp" "$tdir/answer.md" || die "retry: could not write the answer" 1
  fi

  # Findings carry-forward, the optional --dwarf escalation and the QA-only resume
  # decision are shared with the scheduler's automatic retries.
  # A refusal (the retry policy found no stronger model, or the escalation could not be
  # written) must stop here: past this point the status is cleared and a dwarf dispatched.
  local prc=0
  if [ "$st" = NEEDS_DECISION ]; then
    retry_decided "$PLAN" "$id" "$DECIDE" "$NEWDWARF" "$REASON" || prc=$?
  else
    prepare_retry "$PLAN" "$id" "$NEWDWARF" || prc=$?
    # A person's plain retry ends what an earlier decision allowed; resuming after an
    # infrastructure stop is not a new choice, so the allowance carries over.
    # Nor is changing a prepared task's model: the count its decision granted still stands.
    [ "$prc" != 0 ] || [ "$st" = INFRA ] || [ -f "$tdir/retry.prepared" ] || rm -f "${tdir:?}/decided.left"
  fi
  if [ "$prc" != 0 ]; then
    write_results "$PLAN"
    [ "$prc" = 3 ] && return 2
    return "$prc"
  fi
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  # --no-dispatch leaves the dependencies to the `run` that dispatches it: one can merge in that same run.
  [ "$NO_DISPATCH" = 1 ] || dependencies_ready "$PLAN" "$id" || { write_results "$PLAN"; return 5; }
  local vrepo vcheckout; vrepo="$(cat "$PLAN/repo" 2>/dev/null)"; vcheckout="$(cat "$PLAN/wt_root")/_integration"
  [ -d "$vcheckout" ] || vcheckout="$vrepo"
  verify_lanes_check "$PLAN" "$vrepo" "$vcheckout"
  [ ! -f "$PLAN/no_ripwire" ] || export FORGE_RIPWIRE=off
  preflight_plan "$PLAN" || return $?
  /bin/bash "$SKILL_DIR/scripts/forge-install-ripwire.sh" || true
  rm -f "$tdir/status"
  # The pauses an earlier run ended with describe that run: this retry's summary must not report them.
  rm -f "$PLAN/infra.last"
  # #118: `retry` holds the plan lock until its attempts end, so several tasks cannot resume side by
  # side. This stops once the retry is prepared, and the marker tells the next `run` that dispatch is
  # a person's retry. The decision's count is already in decided.left, which that run honours.
  if [ "$NO_DISPATCH" = 1 ]; then
    : > "$tdir/retry.prepared" || die "retry: could not record the prepared retry" 1
    write_results "$PLAN"
    note "$id is ready and nothing was dispatched; forge-parallel.sh run $PLAN starts it"
    return 0
  fi
  rm -f "$tdir/retry.prepared"

  note "retrying $id with dwarf $(field "$T" "$id" dwarf)"
  forge_fast_fractal_note "$PLAN"
  export FORGE_FRACTAL_RETRY=1
  export FORGE_DATASET_TRIGGER=human
  do_task "$PLAN" "$id"
  st="$(task_status "$PLAN" "$id")"
  # A decision of retry=N is N attempts in all: this one, then N-1 more while the reviews fail.
  # Any other decision is this one attempt. The same stops apply as to any retry: a task that
  # stopped for a decision again, an empty diff, or a policy that finds no stronger model.
  # The count is decided.left, so it survives a run that ended before the attempts were used.
  local more; more="$(decided_left "$tdir")"; more="${more:-0}"
  # --retry-failed N (or the frozen profile's) adds N further attempts for this call alone, spent after the
  # decided ones; --infra-retries M waits out that many infrastructure stops before giving up (#48).
  local rf_left="$PP_RETRY_FAILED" inf_left="$PP_INFRA_RETRIES" inf_cycle=0 from_decided
  while :; do
    if [ "$st" = INFRA ]; then
      # No attempt was spent and nothing was judged: wait the stop out as the scheduler does, and dispatch again.
      [ "$inf_left" -gt 0 ] || break
      inf_left=$(( inf_left - 1 ))
      retry_infra_wait "$PLAN" "$tdir" "$inf_cycle"; inf_cycle=$(( inf_cycle + 1 ))
      prepare_retry "$PLAN" "$id" || break
      rm -f "$tdir/status" "$PLAN/infra.last"
      note "retrying $id after an infrastructure stop ($inf_left more wait(s) allowed)"
      do_task "$PLAN" "$id"
      st="$(task_status "$PLAN" "$id")"
      continue
    fi
    { [ "$st" = FAIL ] || [ "$st" = UNKNOWN ]; } && [ ! -e "$tdir/noretry" ] || break
    from_decided=0
    if [ "$more" -gt 0 ]; then from_decided=1; elif [ "$rf_left" -le 0 ]; then break; fi
    # Each further attempt is spent against the cap (#69) like the scheduler's retries: an UNKNOWN review
    # does not stop the task by itself, so the cap is checked here, and a task at it stops for a decision.
    if ! attempt_cap_gate "$PLAN" "$id" "$st" ""; then st="$(task_status "$PLAN" "$id")"; break; fi
    if [ "$from_decided" = 1 ]; then more=$(( more - 1 )); else rf_left=$(( rf_left - 1 )); fi
    prepare_retry "$PLAN" "$id" || break
    if [ "$from_decided" = 1 ]; then decided_left_set "$tdir" "$more" || break; fi
    rm -f "$tdir/status"
    if [ "$from_decided" = 1 ]; then note "retrying $id (decided retry, $more more after this)"
    else note "retrying $id (--retry-failed, $rf_left more after this)"; fi
    do_task "$PLAN" "$id"
    st="$(task_status "$PLAN" "$id")"
  done
  if [ "$st" = "INFRA" ]; then
    # Nothing was judged and no attempt was spent: skip verification, say how to resume.
    write_results "$PLAN"
    echo
    report_infra "$PLAN"
    return 8
  fi
  if [ "$st" = "PASS" ]; then
    if merge_task "$PLAN" "$id"; then
      note "$id PASS -> merged"
    else
      note "$id PASS but conflicted on merge — branch preserved"
    fi
  fi
  report_untested "$PLAN" "$id"
  # Tasks still to run (#112): the run that dispatches them verifies the integration branch at its
  # end, and integrate verifies its candidate, so verifying now would only be repeated.
  local left="" lid
  for lid in $(all_ids "$T"); do
    case "$(task_status "$PLAN" "$lid")" in PENDING|BLOCKED) left="${left:+$left }$lid" ;; esac
  done
  if [ -n "$left" ]; then
    note "verification deferred: tasks still to run ($left); the next run, or integrate, verifies the integration branch"
  else
    verify_result "$PLAN" "$(cat "$PLAN/wt_root")/_integration" "$PLAN" || { write_results "$PLAN"; run_report_extras "$PLAN"; return 5; }
  fi
  write_results "$PLAN"
  echo
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  run_report_extras "$PLAN"
  report_decisions "$PLAN"
  [ "$(task_status "$PLAN" "$id")" = "MERGED" ] || return 5
  return 0
}

# One row per execution of the verify command, with its own copy of the log: the
# deterministic rerun and Jev's retry both write verification.retry.log, so without this
# only the last of them would survive.
verify_run_note() { # verify_run_note <out> <kind> <rc> <before> <after> <log> <t0>
  local out="$1" n=1
  [ -f "$out/verification.runs.tsv" ] && n=$(( $(wc -l < "$out/verification.runs.tsv") + 1 ))
  cp "$out/$6" "$out/verification.run-$n.log" 2>/dev/null || :
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$2" "${3:-}" "$([ "$4" = "$5" ] && echo 0 || echo 1)" \
    "$(( $(date +%s) - $7 ))" "verification.run-$n.log" >> "$out/verification.runs.tsv"
}

# --- stoppable verification --------------------------------------------------
# `run` sets FORGE_RUN_STOPPABLE for the verification it supervises. Then the verify command
# runs as its own process group, and a TERM to the verification subshell stops that whole
# group: TERM at once, KILL after a grace period. `retry` and `integrate` leave it unset and
# keep the plain foreground command.
verify_group_stop() { # TERM the verify command's group; KILL it after FORGE_VERIFY_GRACE seconds
  [ -n "$FORGE_VERIFY_GROUP" ] && [ -z "$FORGE_VERIFY_TIMER" ] || return 0
  local group="$FORGE_VERIFY_GROUP" termed=1
  # Until the guard's setsid there is no group: the TERM goes to the guard itself, which dies
  # of it before it starts the command. A group that forms all the same gets its TERM from
  # the timer.
  kill -TERM -- "-$group" 2>/dev/null || { termed=0; kill -TERM "$group" 2>/dev/null; }
  # The timer polls in short naps rather than sleeping the whole grace in one: it ends by
  # itself within a nap of the group's last member, and a long sleep left behind would hold
  # the plan lock it inherited.
  ( trap - TERM INT
    ticks=$((FORGE_VERIFY_GRACE * 10))
    while kill -0 -- "-$group" 2>/dev/null || { [ "$termed" = 0 ] && kill -0 "$group" 2>/dev/null; }; do
      if [ "$termed" = 0 ] && kill -TERM -- "-$group" 2>/dev/null; then termed=1; fi
      if [ "$ticks" -le 0 ]; then
        if [ "$termed" = 1 ]; then kill -KILL -- "-$group" 2>/dev/null; else kill -KILL "$group" 2>/dev/null; fi
        break
      fi
      sleep 0.1
      ticks=$((ticks - 1))
    done ) &
  FORGE_VERIFY_TIMER=$!
}

verify_signal() { # trap handler of a stoppable verification
  FORGE_VERIFY_STOP=1
  verify_group_stop
}

verify_exec() { # verify_exec <checkout> <command> <log> [grace seconds, default 5] -> the command's exit code, 130 when stopped
  local wt="$1" cmd="$2" log="$3" rc=0 group deadline
  if [ "${FORGE_RUN_STOPPABLE:-0}" != 1 ]; then
    (cd "$wt" && /bin/bash -c "$cmd") > "$log" 2>&1
    return $?
  fi
  FORGE_VERIFY_GRACE="${4:-5}"
  [ "$FORGE_VERIFY_STOP" = 1 ] && return 130
  # A background job ignores SIGINT and SIGQUIT, and the command would inherit that; the
  # wrapper restores both before the guard puts the command in a session of its own.
  (cd "$wt" && exec python3 -c 'import os, signal, sys
for name in (signal.SIGINT, signal.SIGQUIT):
    signal.signal(name, signal.SIG_DFL)
os.execv(sys.executable, [sys.executable] + sys.argv[1:])' \
    "$SKILL_DIR/scripts/forge-guard.py" session -- /bin/bash -c "$cmd") > "$log" 2>&1 <&0 &
  group=$!
  FORGE_VERIFY_GROUP="$group"
  [ "$FORGE_VERIFY_STOP" = 1 ] && verify_group_stop
  # `wait` returns at once when a trapped signal arrives, so wait again until the job is gone.
  wait "$group" || rc=$?
  while kill -0 "$group" 2>/dev/null; do rc=0; wait "$group" || rc=$?; done
  if [ "$FORGE_VERIFY_STOP" = 1 ]; then
    # Members that outlived the leader hold the plan lock; the KILL timer ends them.
    deadline=$((SECONDS + FORGE_VERIFY_GRACE + 5))
    while kill -0 -- "-$group" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do sleep 0.1; done
  fi
  FORGE_VERIFY_GROUP=""
  if [ -n "$FORGE_VERIFY_TIMER" ]; then
    wait "$FORGE_VERIFY_TIMER" 2>/dev/null
    FORGE_VERIFY_TIMER=""
  fi
  [ "$FORGE_VERIFY_STOP" = 1 ] && return 130
  return "$rc"
}

verify_mark_interrupted() { # verify_mark_interrupted <out>
  echo INTERRUPTED > "$1/verification.status"
  echo 130 > "$1/verification.exit"
  note "verification interrupted; the verify command was stopped and is not rerun"
}

# True, once the verification is marked INTERRUPTED, when a stop has arrived. Checked before
# each run of the command (a stop that came first runs nothing and records no run) and after.
verify_stopped() { # verify_stopped <out>
  [ "$FORGE_VERIFY_STOP" = 1 ] || return 1
  verify_mark_interrupted "$1"
}

verify_result() ( verify_result_body "$@" ) # plan, checkout, artifact directory[, fixed source]

# The body is a function of its own so `run` can start it as `( verify_result_body ... ) &`:
# that one subshell is the background job, the home of the signal handler above and of
# forge_metric_begin's EXIT trap.
# An optional fourth argument fixes the verification: "command:<file>" or "lanes:<dir>" (a
# finished verification's verification.command or verification.lanes.d/). It replaces
# verify_source_for and verify-discover, so a re-run judges by the predicate that verification
# used, whatever the plan, the repository or discovery would say now.
verify_result_body() { # plan, checkout, artifact directory[, fixed source]
  local plan="$1" wt="$2" out="$3" cmd="" before after rc=0 repo jev_loaded=0 vretries
  FORGE_VERIFY_STOP=0 FORGE_VERIFY_GROUP="" FORGE_VERIFY_TIMER="" FORGE_VERIFY_GRACE=5
  [ "${FORGE_RUN_STOPPABLE:-0}" = 1 ] && trap verify_signal TERM INT
  mkdir -p "$out"
  forge_metric_begin "$out" verification
  forge_metric_phase verification
  repo="$(cat "$plan/repo")"
  rm -f "$out/verification.flaky" "$out/verification.retry.log" "$out/verification.failing.log" "$out/verification.coverage.txt" \
    "$out/verification.runs.tsv" "$out"/verification.run-*.log "$out/verification.commit" \
    "$out/verification.lanes.json" "$out"/verification.lane.*.log
  # The commit being verified, named before any early return, so every outcome (FAIL and
  # UNVERIFIED included) can be joined to the code it judged.
  git -C "$wt" rev-parse HEAD > "$out/verification.commit" 2>/dev/null || rm -f "$out/verification.commit"
  # A failed command is rerun this many times before it counts, so one flaky lane does not
  # cost a whole run. The source fingerprint still has to be unchanged, and a rerun that
  # passes is recorded loudly (verification.flaky) rather than passed off as clean.
  vretries="$(cat "$plan/verify_retries" 2>/dev/null)"
  case "$vretries" in ''|*[!0-9]*) vretries=1 ;; esac
  # Sourced unconditionally (cheap: local function/var definitions, no network) so
  # forge_decide_active is available below. verify_result is itself a subshell, so this
  # never leaks state into the caller. A missing or broken options file just leaves
  # jev_loaded=0 and every Jev branch below falls through to today's behaviour.
  if [ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ]; then
    source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && jev_loaded=1
  fi
  local vsource fixed="${4:-}"
  if [ -n "$fixed" ]; then
    vsource="$(verify_fixed_source "$fixed")" || { echo UNVERIFIED > "$out/verification.status"
      note "combined result UNVERIFIED: unusable fixed verification source: $fixed"; return 0; }
    # A fixed source that is missing or empty is not a reason to look elsewhere.
    case "$vsource" in
      lanes:*) verify_lanes_present "${vsource#*$'\t'}" || vsource="none" ;;
      *) [ -s "${vsource#*$'\t'}" ] || vsource="none" ;;
    esac
    if [ "$vsource" = none ]; then
      echo UNVERIFIED > "$out/verification.status"
      note "combined result UNVERIFIED: the fixed verification source $fixed is missing or empty"; return 0
    fi
  else
    vsource="$(verify_source_for "$plan" "$repo")"
  fi
  case "$vsource" in command:*) cmd="$(cat "${vsource#*$'\t'}")" ;; esac
  # A lanes snapshot belongs to the verification that wrote it: a run on a command, or on
  # other lanes, leaves none behind (verify_lanes_run makes the new one).
  case "$vsource" in lanes:*) ;; *) rm -rf "$out/verification.lanes.d" ;; esac
  if [ "$(forge_tree "$wt")" != "$(git -C "$wt" rev-parse HEAD^{tree})" ]; then
    echo FAIL > "$out/verification.status"
    note "verification checkout has source changes outside its commit"; return 1
  fi
  case "$vsource" in
    lanes:*)
      verify_lanes_run "$plan" "$wt" "$out" "${vsource#*$'\t'}" "$vretries"
      return $? ;;
  esac
  # Only when nothing is configured: ask Jev to name a command that actually exists
  # in the repo. Its exit code, not its stdout text, decides whether a judgment was
  # produced — any non-zero exit (no key, disabled, nothing suitable, a traceback)
  # falls straight through to the exact UNVERIFIED behaviour below. A Jev failure
  # must never change the outcome of a run.
  if [ -z "$cmd" ] && [ -z "$fixed" ] && [ "$jev_loaded" = 1 ] && forge_decide_active verify-discover; then
    local jev_json jev_rc jev_cmd jev_note
    # --json rather than the bare form: the record exists so a human can audit why a
    # command ran, and "confidence: null" answers nothing. The exit code still decides.
    jev_json="$(python3 "$SKILL_DIR/scripts/forge-jev.py" verify-discover --repo "$repo" --json --run-dir "$out" 2>/dev/null)"
    jev_rc=$?
    jev_cmd=""
    if [ "$jev_rc" -eq 0 ] && [ -n "$jev_json" ]; then
      jev_cmd="$(printf '%s' "$jev_json" | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin) or {}).get("command") or "")
except ValueError:
    print("")
' 2>/dev/null)"
    fi
    if [ -n "$jev_cmd" ]; then
      cmd="$jev_cmd"
      # Coverage (#42): the pick's evidence names fewer languages than the repo has. The
      # pick stands and its run still decides; only the announcement says so.
      jev_note="$(printf '%s' "$jev_json" | python3 -c '
import json, sys
try:
    coverage = (json.load(sys.stdin) or {}).get("coverage") or {}
except ValueError:
    coverage = {}
covered = coverage.get("covered") or []
missing = [language for language in coverage.get("present") or [] if language not in covered]
if covered and missing:
    print(" — it may cover only part of the repo (covers %s; the repo also has %s)" % (", ".join(covered), ", ".join(missing)))
' 2>/dev/null)"
      # Loud on purpose: an unreviewed command is about to run on the user's behalf,
      # and they must never discover that later from a log instead of right here.
      note "jev chose a verification command (no --verify or .forge/verify configured): $cmd$jev_note"
      printf '%s' "$jev_json" | python3 -c '
import json, sys
out = sys.argv[1]
try:
    found = json.load(sys.stdin) or {}
except ValueError:
    found = {}
with open(out + "/verification.jev.json", "w") as fh:
    json.dump({"discovered": found}, fh, indent=2, sort_keys=True)
    fh.write("\n")
' "$out" 2>/dev/null || true
    fi
  fi
  verify_coverage_report "$wt" "$cmd" "$out/verification.coverage.txt"
  if [ -z "$cmd" ]; then
    echo UNVERIFIED > "$out/verification.status"
    note "combined result UNVERIFIED: no --verify command or .forge/verify configured"
    return 0
  fi
  printf '%s\n' "$cmd" > "$out/verification.command"
  before="$(forge_fingerprint "$wt")" || return 1
  local vt0; vt0="$(date +%s)"
  verify_stopped "$out" && return 130
  verify_exec "$wt" "$cmd" "$out/verification.log" || rc=$?
  [ "$FORGE_VERIFY_STOP" = 1 ] || echo "$rc" > "$out/verification.exit"
  after="$(forge_fingerprint "$wt")" || { verify_stopped "$out" && return 130; return 1; }
  verify_run_note "$out" initial "$rc" "$before" "$after" verification.log "$vt0"
  verify_stopped "$out" && return 130
  echo "$before" > "$out/verification.fingerprint"
  if [ "$rc" != 0 ] || [ "$before" != "$after" ]; then
    local flaked=0 n_retry=0 rc3 b3 a3 fcmd
    # The rerun itself is the evidence: it needs no model and no recorded trap. A tree
    # change is never retried.
    if [ "$rc" != 0 ] && [ "$before" = "$after" ] && [ "$vretries" -gt 0 ]; then
      # First the failing tests alone, through .forge/verify-subset (#112): failing again on their own
      # confirms a regression without a second full suite. Their pass says nothing about the rest of
      # the command, so the full rerun below still decides a flaky result.
      if [ -s "$repo/.forge/verify-subset" ] && fcmd="$(python3 "$SKILL_DIR/scripts/forge-verify-failed.py" command \
           --checkout "$wt" --log "$out/verification.log" --subset "$repo/.forge/verify-subset" 2>/dev/null)"; then
        rc3=0
        note "verification failed (exit $rc); rerunning only the failing tests first: $fcmd"
        b3="$(forge_fingerprint "$wt")" || return 1
        vt0="$(date +%s)"
        verify_stopped "$out" && return 130
        verify_exec "$wt" "$fcmd" "$out/verification.failing.log" || rc3=$?
        a3="$(forge_fingerprint "$wt")" || { verify_stopped "$out" && return 130; return 1; }
        verify_run_note "$out" failing "$rc3" "$b3" "$a3" verification.failing.log "$vt0"
        verify_stopped "$out" && return 130
        # 126 and 127: the subset command itself could not run, which confirms nothing.
        if [ "$rc3" != 0 ] && [ "$rc3" != 126 ] && [ "$rc3" != 127 ] && [ "$b3" = "$a3" ]; then
          echo FAIL > "$out/verification.status"
          note "the failing tests fail again on their own (exit $rc3): a regression, not a flaky test, so the full command is not rerun; see $out/verification.log"
          return 1
        fi
      fi
      while [ "$n_retry" -lt "$vretries" ] && [ "$flaked" = 0 ]; do
        n_retry=$((n_retry+1)); rc3=0
        note "verification failed (exit $rc); rerunning to tell a flaky lane from a regression ($n_retry/$vretries)"
        b3="$(forge_fingerprint "$wt")" || return 1
        vt0="$(date +%s)"
        verify_stopped "$out" && return 130
        verify_exec "$wt" "$cmd" "$out/verification.retry.log" || rc3=$?
        a3="$(forge_fingerprint "$wt")" || { verify_stopped "$out" && return 130; return 1; }
        verify_run_note "$out" rerun "$rc3" "$b3" "$a3" verification.retry.log "$vt0"
        verify_stopped "$out" && return 130
        if [ "$rc3" = 0 ] && [ "$b3" = "$a3" ]; then
          flaked=1
          {
            echo "first_exit=$rc"; echo "retry_exit=0"; echo "retries_used=$n_retry"
            echo "first_log=$out/verification.log"; echo "retry_log=$out/verification.retry.log"
          } > "$out/verification.flaky"
          echo 0 > "$out/verification.exit"
          note "FLAKY: verification failed once (exit $rc) and passed on rerun — the first failure is in $out/verification.log; this is not a clean pass"
        fi
      done
    fi
    if [ "$flaked" = 1 ]; then
      echo PASS > "$out/verification.status"
      return 0
    fi
    echo FAIL > "$out/verification.status"
    note "combined verification failed or changed source; see $out/verification.log"
    return 1
  fi
  echo PASS > "$out/verification.status"
}

write_results() { # write_results <plan>
  local PLAN="$1" T="$1/tasks.tsv" run_id id
  run_id="$(cat "$PLAN/run_id")"
  : > "$PLAN/results.tsv"
  for id in $(all_ids "$T"); do
    printf '%s\t%s\tforge/%s/%s\n' "$id" "$(task_status "$PLAN" "$id")" "$run_id" "$id" >> "$PLAN/results.tsv"
  done
}

# forecast_report PLAN: `forge dataset forecast --run`, as note lines. Whatever it does
# (no dataset, a broken store, a slow read), the run goes on and its exit code is its own.
forecast_report() {
  local line
  python3 "$SKILL_DIR/scripts/forge-dataset.py" forecast "$1" --run 2>/dev/null \
    | while IFS= read -r line; do note "$line"; done
  return 0
}

# --- run ----------------------------------------------------------------------
# Waits for a background job and, when a trapped signal ends the wait early, waits again
# until the job is gone; its exit code is left in run_rc.
run_wait() { # run_wait <pid>
  run_rc=0
  wait "$1" || run_rc=$?
  while kill -0 "$1" 2>/dev/null; do run_rc=0; wait "$1" || run_rc=$?; done
}

# A standalone retry runs its dispatches in the foreground, and bash runs a trap only between
# commands, so a TERM to the retry's pid alone ended it and left the dispatch, its harness and
# the tools they started running in the task's worktree (#129). retry_stop sends TERM to
# everything below the retry job (forge-guard.py stop-tree, which reads ps itself), and waits,
# bounded, while each dispatch's EXIT trap stops its harness and reaps what it started. What
# still runs then is killed, and the guard's backstop runs on any harness record a dispatch
# left behind, as the scheduler's does.
RETRY_STOP_WAIT=20     # seconds; a dispatch's EXIT trap needs about 1 s plus its ~6 s reap
retry_stop() { # retry_stop <job pid> <plan> <task>
  local record
  python3 "$SKILL_DIR/scripts/forge-guard.py" stop-tree "$1" --wait "$RETRY_STOP_WAIT" >/dev/null 2>&1 \
    || kill -TERM "$1" 2>/dev/null
  [ -n "$2" ] && [ -n "$3" ] || return 0
  for record in "$2/tasks/$3/dwarf.harness" "$2/tasks/$3/qa.harness"; do
    [ ! -e "$record" ] || python3 "$SKILL_DIR/scripts/forge-guard.py" stop "$record" >/dev/null 2>&1
  done
}

# The handler of do_run's subshell: a signal reaches the job in progress (the scheduler,
# then the verification) as TERM. Before any job exists there is nothing to clean up.
run_signal() {
  run_stop=1
  if [ -n "$run_child" ]; then kill -TERM "$run_child" 2>/dev/null
  elif [ "$run_phase" = pre ]; then exit 130; fi
}

# Not a ( ) function: main runs it as `( do_run ... ) &`, one subshell that is both the
# background job and the home of its signal handler. A function called with `&` would be a
# second subshell level, and an async function does not run its EXIT trap.
do_run() {
  local run_child="" run_stop=0 run_phase=pre run_rc=0 interrupted=0 vstopped=0
  FORGE_RUN_STOPPABLE=1
  trap run_signal TERM INT
  local PLAN="${1:?run needs a plan dir}"; shift
  FORGE_RUN_STARTED="$(forge_now)"   # the summary's overridden verdicts are this run's
  local fractal_pipeline_args=("$@")
  local MAXP=3 DRY=0 jev_rc=0 FORECAST=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-forecast) FORECAST=0; shift ;;
      --fractal) forge_fractal_flag on || exit $?; shift ;;
      --fractal-auto-decompose) forge_fractal_flag on || exit $?; FRACTAL_ARGS+=("$1"); shift ;;
      --no-fractal) forge_fractal_flag off || exit $?; shift ;;
      --fractal-*) FRACTAL_ARGS+=("$1" "${2:?}"); shift 2 ;;
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --output) OUTPUT="${2:?}"; shift 2 ;;
      --max-parallel) MAXP="${2:?}"; shift 2 ;;
      --yolo-dwarf)   touch "$PLAN/yolo_dwarf"; shift ;;
      --yolo-qa)      touch "$PLAN/yolo_qa"; shift ;;
      --verify)     printf '%s' "${2:?--verify needs a command}" > "$PLAN/verify_cmd"; rm -rf "$PLAN/verify.d"; PP_LANES_FRESH=0; shift 2 ;;
      --setup)        printf '%s' "${2:?--setup needs a command}" > "$PLAN/setup_cmd"; shift 2 ;;
      --dry-run)      DRY=1; shift ;;
      *)
        # `case` is not a loop, so `continue` here belongs to the enclosing while.
        if forge_pp_flag run "$PLAN" "$@"; then shift "$PP_SHIFT"; continue; fi
        if [ "$JEV_OPTS" = 1 ]; then
          forge_jev_flag "$@"; jev_rc=$?
          case "$jev_rc" in
            0) shift "$JEV_SHIFT"; continue ;;
            2) exit 2 ;;
          esac
        fi
        die "run: unknown option '$1'"
        ;;
    esac
  done
  forge_profile_run_values "$PLAN"
  [ "$JEV_OPTS" = 1 ] && forge_jev_settle "$PLAN"
  [ -n "$PP_SWEEP" ] || [ "$PP_SWEEP_PLAUSIBLE$PP_SWEEP_MAX_SET" = 00 ] || die "--sweep-plausible and --sweep-max go with --sweep"
  case "$OUTPUT" in summary|full) ;; *) die "--output must be summary or full" ;; esac
  export FORGE_OUTPUT="$OUTPUT"
  case "$MAXP" in ''|*[!0-9]*) die "--max-parallel must be positive" ;; esac
  [ "$MAXP" -gt 0 ] || die "--max-parallel must be positive"
  local T="$PLAN/tasks.tsv" W="$PLAN/waves.tsv"
  [ -s "$W" ] || die "no waves.tsv — run the plan subcommand first"
  grep -q 'UNASSIGNED' "$T" && die "some tasks are UNASSIGNED — assign a dwarf before running"

  compute_waves "$PLAN"
  local REPO run_id wt_root int_wt int_br
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  # Before any decision-model step: the privacy floor and the run budget read these.
  [ "$JEV_OPTS" = 1 ] && forge_decide_env "$REPO" "$PLAN"
  if [ "$DRY" != 1 ]; then
    # The concurrency limit this run was given, for forge-ui to draw; a later run rewrites it.
    printf '%s\n' "$MAXP" > "$PLAN/max_parallel"
    # Plan registers too; this also picks up plans made before it did.
    forge_ui_register "$PLAN"
  fi
  forge_fractal_parallel_run "$PLAN" "$REPO" "$DRY" ${fractal_pipeline_args[@]+"${fractal_pipeline_args[@]}"} || return $?
  forge_fast_fractal_note "$PLAN"
  # Durable, beside the repo — never TMPDIR. A temp worktree root has destroyed
  # real work here before: isolated worktrees are never pushed, so when the root
  # goes, the only copy of that work goes with it.
  wt_root="$(cd "$REPO/.." && pwd)/.forge-worktrees/$run_id"
  echo "$wt_root" > "$PLAN/wt_root"
  # Sibling, not parent: git refs are files, so a branch `forge/<run>` makes
  # `forge/<run>/<task>` impossible ("cannot lock ref ... exists; cannot create").
  int_br="forge/$run_id-integration"; int_wt="$wt_root/_integration"

  if [ "$DRY" = 1 ]; then
    echo "worktree root: $wt_root"
    echo "integration:   $int_br"
    local id
    for id in $(awk -F'\t' '{print $2}' "$W"); do
      echo "  $id -> $wt_root/$id  (branch forge/$run_id/$id)"
      echo "      dwarf: $DISPATCH dwarf $(field "$T" "$id" dwarf) --repo $wt_root/$id"
      echo "      qa   : $DISPATCH qa    $(field "$T" "$id" qa) --repo $wt_root/$id"
    done
    echo "(dry run — nothing created or dispatched)"
    return 0
  fi

  forge_metric_begin "$PLAN" parallel
  forge_metric_phase preflight
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  [ ! -f "$PLAN/no_ripwire" ] || export FORGE_RIPWIRE=off
  preflight_plan "$PLAN" || return $?
  [ -z "$PP_SWEEP" ] || sweep_preflight "$PLAN" || return $?
  /bin/bash "$SKILL_DIR/scripts/forge-install-ripwire.sh" || true
  forge_metric_phase preparation
  mkdir -p "$wt_root"
  # A branch named exactly forge/<run-id> would occupy refs/heads/forge/<run-id> as a
  # file and make every task branch below it impossible to create. Catch it here with
  # an actionable message rather than letting all N tasks fail identically at dispatch.
  if (cd "$REPO" && git rev-parse --verify --quiet "forge/$run_id" >/dev/null 2>&1); then
    die "branch 'forge/$run_id' blocks task branches 'forge/$run_id/<task>' (git refs are files, so a ref cannot also be a directory). Delete or rename it: git branch -D forge/$run_id" 3
  fi
  if ! (cd "$REPO" && git rev-parse --verify "$int_br" >/dev/null 2>&1); then
    (cd "$REPO" && git branch "$int_br" HEAD) || die "could not create $int_br" 3
    # Where the integration branch started, so a whole-run review can diff exactly what
    # this run added. Plans created before this file existed fall back to a merge-base.
    git -C "$REPO" rev-parse HEAD > "$PLAN/integration.base"
  fi
  if [ ! -d "$int_wt" ]; then
    (cd "$REPO" && git worktree add "$int_wt" "$int_br" >/dev/null 2>&1) \
      || die "could not create integration worktree" 3
    forge_worktree_event created "$PLAN" "$REPO" "$int_wt" "$int_br" integration
    # The integration worktree is where a merged result gets validated, so it needs
    # the same setup the task worktrees get. Without it, the one checkout holding
    # every task's merged work is the one checkout that cannot build or test it.
    mkdir -p "$PLAN/tasks/_integration"
    run_worktree_setup "$PLAN" "$REPO" "$int_wt" "$PLAN/tasks/_integration" _integration
  fi

  if [ -s "$PLAN/accepted.integration" ] && [ "$(git -C "$int_wt" rev-parse HEAD)" != "$(cat "$PLAN/accepted.integration")" ]; then
    die "integration branch changed outside this run. To bring those commits into the run: forge-parallel.sh adopt $PLAN --head --reason \"...\"" 3
  fi
  git -C "$int_wt" rev-parse HEAD > "$PLAN/accepted.integration"
  local id failed=0
  # Also before any model is paid for: what this plan is likely to cost, and which tasks
  # history says may bounce. Advisory, budgeted and silent without history; it never stops a run.
  [ "$FORECAST" = 1 ] && forecast_report "$PLAN"
  # Invalid verification lanes stop the run here, before any model is paid for.
  verify_lanes_check "$PLAN" "$REPO" "$int_wt"
  # Say now, before any model is paid for, which suites the verify command will not run.
  verify_coverage_report "$REPO" "$(verify_command_for "$PLAN" "$REPO")" "$PLAN/verification.coverage.txt"
  local sched_rc=0 paused=0 sched_args=()
  [ "$PP_RETRY_FAILED" -gt 0 ] && sched_args+=(--retry-failed "$PP_RETRY_FAILED")
  [ "$PP_INFRA_RETRIES" -gt 0 ] && sched_args+=(--infra-retries "$PP_INFRA_RETRIES")
  [ -n "$PP_SWEEP" ] && sched_args+=(--sweep "$PP_SWEEP" --sweep-max "$PP_SWEEP_MAX")
  [ "$PP_SWEEP_PLAUSIBLE" = 1 ] && sched_args+=(--sweep-plausible)
  forge_metric_phase dispatch
  # In the background so a signal to this shell ends the wait; the scheduler gets it as TERM,
  # stops its tasks and returns 130.
  run_phase=scheduler
  python3 "$SKILL_DIR/scripts/forge-schedule.py" "$SELF" "$PLAN" "$MAXP" ${sched_args[@]+"${sched_args[@]}"} <&0 &
  run_child=$!
  [ "$run_stop" = 1 ] && kill -TERM "$run_child" 2>/dev/null
  run_wait "$run_child"; sched_rc=$run_rc
  run_child=""
  [ "$sched_rc" != 0 ] && failed=1
  [ "$sched_rc" = 8 ] && paused=1

  forge_metric_phase verification
  # Counted after that phase call: a signal that arrived while it ran is handled only once it
  # returned, and must still keep verification from starting.
  [ "$sched_rc" = 130 ] || [ "$run_stop" = 1 ] && interrupted=1
  # An infrastructure pause leaves the run unfinished, so there is nothing yet worth
  # verifying; the next `run` resumes and verifies at its end. An interrupted run has
  # merged only part of its work: a verification of that is not worth its minutes, and it
  # would run again as the rerun if it were killed.
  if [ "$paused" = 0 ] && [ "$interrupted" = 0 ]; then
    run_phase=verification
    ( verify_result_body "$PLAN" "$int_wt" "$PLAN" ) <&0 &
    run_child=$!
    [ "$run_stop" = 1 ] && kill -TERM "$run_child" 2>/dev/null
    run_wait "$run_child"
    [ "$run_rc" != 0 ] && failed=1
    [ "$run_rc" = 130 ] && vstopped=1
    run_child=""
    # A stop that killed the subshell before its handler was set leaves no record: it counts too.
    [ "$vstopped" = 1 ] || [ "$run_stop" = 1 ] && interrupted=1
  fi
  run_phase=results
  write_results "$PLAN"
  # Only prune worktrees for work that is safely merged, and only after the record
  # of it has been written and read back. A failed task keeps its worktree and
  # branch so there is something to inspect and retry from.
  if [ -s "$PLAN/results.tsv" ]; then
    for id in $(awk -F'\t' '$2=="MERGED" {print $1}' "$PLAN/results.tsv"); do
      [ -e "$wt_root/$id" ] || continue
      (cd "$REPO" && git worktree remove --force "$wt_root/$id" >/dev/null 2>&1) \
        && forge_worktree_event removed "$PLAN" "$REPO" "$wt_root/$id" "forge/$run_id/$id" task "$id"
    done
  fi

  echo
  echo "results (integration branch: $int_br)"
  echo "artifacts: $PLAN/tasks/<id>/{dwarf,qa}.{last,log}; results: $PLAN/results.tsv"
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  echo
  run_report_extras "$PLAN"
  # Pre-review tests (#85): every check of the plan, and the reviews the red ones saved.
  local pre_review
  pre_review="$(cat "$PLAN"/tasks/*/prereview.log 2>/dev/null | awk -F'\t' 'NF >= 3 { n++; if ($2 == "failed") f++; s += $3 }
    END { if (n) printf "pre-review tests: %d check(s), %d attempt(s) sent back without a review, %ds of test time\n", n, f, s }')"
  [ -z "$pre_review" ] || { echo "$pre_review"; echo; }

  # Undeclared files are reported here rather than buried in a task log, because
  # this is the list that explains any CONFLICT above.
  local drift=0
  for id in $(all_ids "$T"); do
    if [ -s "$PLAN/tasks/$id/drift.txt" ]; then
      [ "$drift" = 0 ] && echo "scope drift (touched files the task did not declare):"
      drift=1
      printf '  %-14s %s\n' "$id" "$(tr '\n' ' ' < "$PLAN/tasks/$id/drift.txt")"
    fi
  done
  [ "$drift" = 1 ] && echo

  # shellcheck disable=SC2046  # ids are single words
  report_untested "$PLAN" $(all_ids "$T")

  # From this run's own flags, never from a verification.status an earlier run left.
  if [ "$interrupted" = 1 ]; then
    if [ "$vstopped" = 1 ]; then echo "interrupted during verification, which was stopped and not rerun."
    else echo "interrupted; verification was skipped."; fi
  fi
  local stopped_ids; stopped_ids="$(awk -F'\t' '$2=="INTERRUPTED" {print $1}' "$PLAN/results.tsv")"
  if [ -n "$stopped_ids" ]; then
    echo "A later run does not dispatch an interrupted task; continue each one with:"
    for id in $stopped_ids; do echo "  forge-parallel.sh retry $PLAN $id"; done
  fi
  [ "$interrupted" = 1 ] || [ -n "$stopped_ids" ] && echo
  # Tasks stopped for the user's answer: the question as it was asked, and the command that
  # answers it. Not failures, so the retry hint below does not count them.
  local waiting; waiting="$(awk -F'\t' '$2=="NEEDS_INPUT" {print $1}' "$PLAN/results.tsv")"
  if [ -n "$waiting" ]; then
    echo "waiting for your answer:"
    for id in $waiting; do
      printf '  %s: %s\n' "$id" "$(awk 'NF { print substr($0, 1, 120); exit }' "$PLAN/tasks/$id/needs_input.md" 2>/dev/null)"
      echo "    answer with: forge-parallel.sh retry $PLAN $id --answer \"<text>\""
    done
    echo
  fi
  # Tasks stopped for a decision (#69): why, and the choices. Not failures to retry either.
  report_decisions "$PLAN"
  # The retry hint is for tasks that actually failed; a run that only failed verification,
  # or only paused on infrastructure, has nothing to retry task by task. An interrupted task
  # has its own hint above, a task waiting for an answer or a decision its own block.
  if [ "$(awk -F'\t' '$2!="MERGED" && $2!="INFRA" && $2!="PENDING" && $2!="BLOCKED" && $2!="INTERRUPTED" && $2!="NEEDS_INPUT" && $2!="NEEDS_DECISION" {n++} END {print n+0}' "$PLAN/results.tsv")" -gt 0 ]; then
    echo "retry a failed task with its reviewer's findings:"
    echo "  forge-parallel.sh retry $PLAN <task-id> [--dwarf <spec>]"
    echo "or let a run retry them itself:  forge-parallel.sh run $PLAN --retry-failed 1"
    echo
  fi
  if [ "$failed" = 1 ] && [ "$paused" = 0 ] && [ "$interrupted" = 0 ] && [ "$(cat "$PLAN/verification.status" 2>/dev/null)" = FAIL ]; then
    echo "combined verification failed: see $PLAN/verification.log"
    echo
    if [ -f "$PLAN/attribute_on" ] && declare -F attribute_search >/dev/null; then
      # This run holds the plan lock: the search function, never the subcommand.
      run_phase=attribute
      ( attribute_search "$PLAN" 0 ) <&0 &
      run_child=$!
      [ "$run_stop" = 1 ] && kill -TERM "$run_child" 2>/dev/null
      run_wait "$run_child"
      run_child=""
      [ "$run_stop" = 1 ] && interrupted=1
      echo
    else
      echo "find a merge that broke it: forge-parallel.sh attribute $PLAN"
      echo
    fi
  fi

  if [ ! -f "$PLAN/no_memory" ]; then
    /bin/bash "$MEMORY" spend "$REPO" --run "$run_id" 2>/dev/null | sed 's/^/  /'
    echo
  fi
  if [ ! -f "$PLAN/no_memory" ] && [ -s "$REPO/.forge/memory.md" ]; then
    echo "project memory: $(grep -c '^- ' "$REPO/.forge/memory.md") fact(s) in .forge/ —"
    echo "  uncommitted, and the only thing forge wrote outside its own branches."
    echo
    echo "Your branch and working tree were not touched, apart from .forge/ above."
  else
    echo "Your branch and working tree were not touched."
  fi
  [ "$interrupted" = 1 ] && return 130
  [ "$paused" = 1 ] && return 8
  [ "$failed" = 1 ] && return 5
  return 0
}

# --- integrate ----------------------------------------------------------------
do_integrate() {
  local PLAN="${1:?}"; shift
  local APPROVED=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --approved) APPROVED=1; shift ;;
      *) if forge_pp_flag integrate "$PLAN" "$@"; then shift "$PP_SHIFT"; else die "integrate: unknown option '$1'"; fi ;;
    esac
  done
  [ "$APPROVED" = 1 ] || die "integrate needs --approved: it merges forge's work into YOUR current branch"
  report_unmerged "$PLAN"
  local REPO run_id int_br cur
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"; int_br="forge/$run_id-integration"
  cur="$(cd "$REPO" && git rev-parse --abbrev-ref HEAD)"
  [ "$cur" = HEAD ] && die "integration requires a checked-out user branch" 3
  [ "$cur" = "$int_br" ] && die "you are already on $int_br"
  [ -n "$(cd "$REPO" && git status --porcelain)" ] && die "working tree is dirty — commit or stash before integrating"
  # Verify the actual combined candidate, including changes on the user's branch.
  local parent target candidate candidate_sha out
  parent="$(git -C "$REPO" rev-parse HEAD)" || return 3
  target="$(git -C "$REPO" rev-parse "$int_br")" || return 3
  [ -s "$PLAN/accepted.integration" ] && [ "$target" = "$(cat "$PLAN/accepted.integration")" ] || die "integration revision does not match the accepted run" 3
  out="$PLAN/integrate-$(date +%s)-$$"; mkdir -p "$out"
  candidate="$(cat "$PLAN/wt_root")/_candidate-$$"
  git -C "$REPO" worktree add --detach "$candidate" "$parent" > "$out/setup.log" 2>&1 || return 3
  forge_worktree_event created "$PLAN" "$REPO" "$candidate" - candidate
  if ! git -C "$candidate" merge --no-ff -m "forge: integrate run $run_id" "$target" > "$out/merge.log" 2>&1; then
    forge_dataset_event integrate_conflict --dir "$PLAN" --set branch="$cur" --set parent="$parent" \
      --set target="$target" --set conflicts="$(git -C "$candidate" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')" \
      --file merge_log="$out/merge.log"
    note "candidate merge conflicted; inspect $candidate and $out/merge.log"; return 6
  fi
  run_worktree_setup "$PLAN" "$REPO" "$candidate" "$out" integration-candidate
  verify_result "$PLAN" "$candidate" "$out" || { report_verify_notes "$out"; return 5; }
  candidate_sha="$(git -C "$candidate" rev-parse HEAD)" || return 3
  report_verify_notes "$out"
  report_known_issues "$PLAN"
  # Opt-in whole-run review of the combined candidate, after verification and before the
  # user's branch moves. Per-task review only ever saw one slice at a time.
  # A profile that names a final reviewer (#48) asks for this review: --final-review wins, then the spec
  # `review` or an earlier --final-review left in final_qa, then the profile's own.
  if [ -z "$PP_FINAL_REVIEW" ] && [ -f "$PLAN/profile.json" ]; then
    local pf_final; pf_final="$(forge_profile_py value "$PLAN/profile.json" routing.final_reviewer 2>/dev/null)"
    pf_final="${pf_final#*$'\t'}"
    if [ -n "$pf_final" ]; then
      PP_FINAL_REVIEW="$(head -1 "$PLAN/final_qa" 2>/dev/null | tr -d ' \r')"
      [ -n "$PP_FINAL_REVIEW" ] || PP_FINAL_REVIEW="$pf_final"
      note "final review: $PP_FINAL_REVIEW, from the plan's profile"
    fi
  fi
  if [ -n "$PP_FINAL_REVIEW" ]; then
    declare -F final_review_for_integrate >/dev/null || die "the final review is not available in this build" 3
    final_review_for_integrate "$PLAN" "$parent" "$candidate_sha" "$candidate" "$out" "$PP_FINAL_REVIEW" || return $?
  fi
  [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" = "$cur" ] &&
    [ "$(git -C "$REPO" rev-parse HEAD)" = "$parent" ] &&
    [ "$(git -C "$REPO" rev-parse "$int_br")" = "$target" ] &&
    [ -z "$(git -C "$REPO" status --porcelain)" ] || die "branch or working tree changed during verification" 3
  git -C "$REPO" merge --ff-only "$candidate_sha" || return 6
  # The user's branch has moved: the run shipped. Recorded now, because the cleanup below
  # can still fail and make the command's exit code say otherwise.
  forge_dataset_event integrate_advanced --dir "$PLAN" --set branch="$cur" --set parent="$parent" \
    --set candidate="$candidate_sha" --set target="$target" --set verification="$(cat "$out/verification.status" 2>/dev/null)"
  git -C "$REPO" worktree remove "$candidate" || return 3
  forge_worktree_event removed "$PLAN" "$REPO" "$candidate" - candidate
  echo "merged candidate of $int_br into $cur (verification: $(cat "$out/verification.status"))"
}

# --- auto (#87, #88) -----------------------------------------------------------------
# The profile `plan` will use for these flags, read without writing anything: the planner
# spec and the fast-tier roles it names, as `key<TAB>value` lines. The choice is plan's own
# (profiles._choose): --no-profile means none; with no --profile the plan's frozen
# profile.json stays; otherwise the named profile, else the default. A frozen profile adds no
# fast roles: plan stored those when it froze it.
forge_auto_profile() { # forge_auto_profile <plan-dir> <repo> <profile name or empty> <no-profile 0|1>
  PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - "$@" <<'PY'
import sys
from pathlib import Path
from forge_decide import profiles
plan, repo, name, none = Path(sys.argv[1]), sys.argv[2] or None, sys.argv[3] or None, sys.argv[4] == '1'
try:
    action, chosen = profiles._choose(repo, name, none, plan / profiles.FROZEN, False, False)
except profiles.ProfileError as exc:
    print('forge: %s' % exc, file=sys.stderr)
    sys.exit(2)
body = chosen.get('body') if isinstance(chosen, dict) else None
values = profiles.flat(body) if body else {}
planner = values.get('routing.planner')
if isinstance(planner, str) and planner:
    print('routing.planner\t%s' % planner)
print('action\t%s' % action)
# As plan stores it: an empty list is `none`, which clears the roles an earlier call stored.
if action == 'new' and isinstance(values.get('policy.fast'), list):
    print('policy.fast\t%s' % profiles._fast_roles(values['policy.fast'], profiles.FAST_ROLES))
PY
}

# Forget what `parse` wrote, so the planner's next reply can be parsed: tasks.tsv and each
# tasks/<id>/. request.md, the planner's records and plan.sendback.md stay.
forge_auto_unparse() { # forge_auto_unparse <plan-dir>
  local id
  if [ -f "$1/tasks.tsv" ]; then
    for id in $(all_ids "$1/tasks.tsv"); do
      case "$id" in ''|.*|*/*) continue ;; esac
      rm -rf "$1/tasks/$id"
    done
  fi
  rm -f "$1/tasks.tsv" "$1/tasks.declared.tsv"
}

# One planner dispatch: prompt in $PLAN/planner.prompt, reply in $PLAN/planner.last. Sets
# AUTO_RC (0, 4 or 8) for the caller instead of exiting, so a send-back can reuse it.
forge_auto_dispatch() { # forge_auto_dispatch <plan-dir> <repo> <spec> <fast-option-or-empty>
  local plan="$1" repo="$2" spec="$3" tier="$4" rc=0
  AUTO_RC=0
  note "auto: dispatching the planner ($spec) for the whole plan"
  bash "$DISPATCH" planner "$spec" --repo "$repo" --run-dir "$plan" --prompt-file "$plan/planner.prompt" \
    --output summary ${tier:+$tier} >&2 || rc=$?
  if [ "$rc" -eq 8 ]; then
    note "auto: the planner stopped on an infrastructure failure (see $plan/planner.infra). Nothing was dispatched after the planner; fix the cause and run auto again"
    AUTO_RC=8
  elif [ "$rc" -ne 0 ]; then
    note "auto: the planner failed (exit $rc). Its last message is $plan/planner.last and its log $plan/planner.log"
    AUTO_RC=4
  fi
  return 0
}

do_auto() {
  local PLAN="${1:-}"
  [ -n "$PLAN" ] && [ "${PLAN#-}" = "$PLAN" ] || die "auto needs a plan dir: forge-parallel.sh auto <plan-dir> --repo DIR --goal-file FILE ..."
  shift
  local REPO="" GOAL="" PLANNER="" DLEVEL="" GATE=auto ONE_DIFF=medium COLLAPSE=1 APPROVED=0
  local PLAN_ARGS=() RUN_ARGS=() PF_NAME="" PF_NONE=0 FAST_GIVEN=0 FAST_PARSED="" arg
  while [ $# -gt 0 ]; do
    case "$1" in
      --)             shift; RUN_ARGS=("$@"); break ;;
      --repo)         [ $# -ge 2 ] || die "--repo needs a directory"; REPO="$2"; shift 2 ;;
      --goal-file)    [ $# -ge 2 ] || die "--goal-file needs a file"; GOAL="$2"; shift 2 ;;
      --planner)      [ -n "${2:-}" ] || die "--planner needs a spec"; PLANNER="$2"; PLAN_ARGS+=("$1" "$2"); shift 2 ;;
      --decompose-level)
        case "${2:-}" in low|medium|high) DLEVEL="$2"; shift 2 ;;
          *) die "--decompose-level must be low, medium or high" ;; esac ;;
      --plan)
        case "${2:-}" in auto|always|never) GATE="$2"; shift 2 ;;
          *) die "--plan must be auto, always or never" ;; esac ;;
      --one-task-difficulty)
        case "${2:-}" in low|medium|high) ONE_DIFF="$2"; shift 2 ;;
          *) die "--one-task-difficulty must be low, medium or high" ;; esac ;;
      --no-collapse)  COLLAPSE=0; shift ;;
      --approved)     APPROVED=1; shift ;;
      --profile)      [ -n "${2:-}" ] || die "--profile needs a profile name"; PF_NAME="$2"; PLAN_ARGS+=("$1" "$2"); shift 2 ;;
      --no-profile)   PF_NONE=1; PLAN_ARGS+=("$1"); shift ;;
      --fast)
        [ -n "${2:-}" ] || die "--fast needs a comma list of roles (dwarf, qa, planner, review), all or none"
        forge_fast_parse "$2" plan; FAST_GIVEN=1
        PLAN_ARGS+=("$1" "$2"); shift 2 ;;
      --no-ripwire)   export FORGE_RIPWIRE=off; PLAN_ARGS+=("$1"); shift ;;
      *)              PLAN_ARGS+=("$1"); shift ;;
    esac
  done
  [ -n "$REPO" ] || die "auto needs --repo DIR"
  [ -d "$REPO" ] || die "--repo '$REPO' is not a directory"
  REPO="$(cd "$REPO" && pwd)"
  [ -n "$GOAL" ] || die "auto needs --goal-file FILE"
  [ -f "$GOAL" ] && [ -r "$GOAL" ] || die "--goal-file '$GOAL' is not a readable file"
  [ ! -e "$PLAN/tasks.tsv" ] || die "auto starts a new plan; use plan and run on an existing one"
  (cd "$REPO" && git rev-parse --git-dir >/dev/null 2>&1) \
    || die "'$REPO' is not a git repository. Decomposed runs need worktrees, and there is no honest fallback without them." 3
  mkdir -p "$PLAN" || die "could not create plan dir '$PLAN'" 3
  PLAN="$(cd "$PLAN" && pwd)"

  # The request, verbatim, and who started this plan.
  if ! [ "$GOAL" -ef "$PLAN/request.md" ]; then
    cp "$GOAL" "$PLAN/request.md" || die "could not write $PLAN/request.md" 3
  fi
  printf 'auto\n' > "$PLAN/orchestrator" || die "could not write $PLAN/orchestrator" 3
  # The goal line the table, every task's "Overall goal", the final review and the dataset read:
  # the request's first non-empty line, a heading's marks dropped. One the user wrote stays.
  if [ ! -s "$PLAN/goal.txt" ]; then
    awk 'NF { sub(/^#+[[:space:]]*/, ""); print substr($0, 1, 200); exit }' "$PLAN/request.md" > "$PLAN/goal.txt" \
      || die "could not write $PLAN/goal.txt" 3
  fi
  local PLANNER_PY="$SKILL_DIR/scripts/forge-planner.py" REQUEST="$PLAN/request.md" rc=0 from_planner=0 fast_tier=""
  PLAN_ARGS=(--repo "$REPO" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"})
  [ -z "$DLEVEL" ] || PLAN_ARGS+=(--decompose-level "$DLEVEL")
  [ "$COLLAPSE" = 0 ] || PLAN_ARGS+=(--collapse-chains)

  # 1. The gate.
  local decision
  case "$GATE" in
    always) decision=plan ;;
    never)  decision=one-task ;;
    *)
      decision="$(python3 "$PLANNER_PY" gate --plan "$PLAN" --goal-file "$REQUEST")" || exit $?
      note "auto: the gate says $decision" ;;
  esac

  if [ "$decision" = one-task ]; then
    # 2. One task, no planner.
    python3 "$PLANNER_PY" one-task --plan "$PLAN" --goal-file "$REQUEST" --difficulty "$ONE_DIFF" >/dev/null || exit $?
  else
    # 3. The planner: its spec, its preflight, its dispatch, its reply.
    local key val profile_planner="" profile_fast="" profile_action="" profile_lines
    forge_profile_precheck "$PLAN" "${PLAN_ARGS[@]}"
    profile_lines="$(forge_auto_profile "$PLAN" "$REPO" "$PF_NAME" "$PF_NONE")" || exit $?
    while IFS="$(printf '\t')" read -r key val; do
      case "$key" in
        routing.planner) profile_planner="$val" ;;
        policy.fast) profile_fast="$val" ;;
        action) profile_action="$val" ;;
      esac
    done <<AUTO_PROFILE
$profile_lines
AUTO_PROFILE
    if [ -z "$PLANNER" ] && [ -n "$profile_planner" ]; then
      PLANNER="$profile_planner"
      # `plan` takes routing.planner from a profile only when it freezes a new one. A kept
      # frozen profile names the planner but plan would not record it: hand it over.
      [ "$profile_action" = new ] || PLAN_ARGS+=(--planner "$PLANNER")
    fi
    [ -n "$PLANNER" ] || die "auto needs --planner SPEC, or a profile with routing.planner"
    # The fast roles `plan` is about to store: the flag, else the profile's. Written now so the
    # planner dispatch asks for the tier through the helper the other roles use.
    if [ "$FAST_GIVEN" != 1 ] && [ -n "$profile_fast" ]; then
      forge_fast_parse "$profile_fast" plan; FAST_GIVEN=1
    fi
    forge_fast_store "$PLAN"
    fast_tier="$(forge_fast_tier "$PLAN" planner)"

    note "auto: checking the planner ($PLANNER) before any model call"
    bash "$DISPATCH" doctor --spec "$PLANNER" --role planner ${fast_tier:+$fast_tier} >&2 || exit $?
    python3 "$PLANNER_PY" prompt --plan "$PLAN" --goal-file "$REQUEST" ${DLEVEL:+--level "$DLEVEL"} >/dev/null || exit $?
    forge_auto_dispatch "$PLAN" "$REPO" "$PLANNER" "$fast_tier"
    [ "$AUTO_RC" -eq 0 ] || exit "$AUTO_RC"
    python3 "$PLANNER_PY" parse --plan "$PLAN" --reply "$PLAN/planner.last" || rc=$?
    case "$rc" in
      0) ;;
      4) note "auto: the planner's reply was refused (see $PLAN/planner.refused). Nothing was dispatched after the planner"
         exit 4 ;;
      *) exit "$rc" ;;
    esac
    from_planner=1
  fi

  # 4. plan, and one send-back when the plan gates ask for it.
  bash "$SELF" plan "$PLAN" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}; rc=$?
  if [ "$rc" -eq 4 ] && [ "$from_planner" = 1 ] && [ -s "$PLAN/plan.sendback.md" ]; then
    note "auto: the plan gates sent items back; asking the planner once more"
    cp "$PLAN/planner.last" "$PLAN/planner.first.last" 2>/dev/null || true
    cp "$PLAN/planner.prompt" "$PLAN/planner.first.prompt" 2>/dev/null || true
    forge_auto_unparse "$PLAN"
    python3 "$PLANNER_PY" prompt --plan "$PLAN" --goal-file "$REQUEST" ${DLEVEL:+--level "$DLEVEL"} >/dev/null || exit $?
    {
      printf '\n\n## Your plan was sent back\n\n'
      printf 'forge checked your plan and sent the items below back. Reply with the whole corrected plan, in the same format.\n\n'
      cat "$PLAN/plan.sendback.md"
    } >> "$PLAN/planner.prompt"
    forge_auto_dispatch "$PLAN" "$REPO" "$PLANNER" "$fast_tier"
    [ "$AUTO_RC" -eq 0 ] || exit "$AUTO_RC"
    rc=0
    python3 "$PLANNER_PY" parse --plan "$PLAN" --reply "$PLAN/planner.last" || rc=$?
    case "$rc" in
      0) ;;
      4) note "auto: the planner's second reply was refused (see $PLAN/planner.refused). Nothing was dispatched after the planner"
         exit 4 ;;
      *) exit "$rc" ;;
    esac
    bash "$SELF" plan "$PLAN" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}; rc=$?
    if [ "$rc" -eq 4 ]; then
      note "auto: the plan gates sent items back a second time; stopping. See $PLAN/plan.sendback.md"
    fi
  fi
  [ "$rc" -eq 0 ] || exit "$rc"

  # 5. Stop for approval, or run.
  if [ "$APPROVED" = 1 ]; then
    bash "$SELF" run "$PLAN" ${RUN_ARGS[@]+"${RUN_ARGS[@]}"}; exit $?
  fi
  local command_line
  command_line="$(printf '%q run %q' "$SELF" "$PLAN")"
  for arg in ${RUN_ARGS[@]+"${RUN_ARGS[@]}"}; do command_line="$command_line $(printf '%q' "$arg")"; done
  printf '\nauto: the plan is ready and nothing has run. To start it:\n  %s\n' "$command_line"
  exit 0
}

# --- main ---------------------------------------------------------------------
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  awk '/^# Usage:/ {on=1} /^set -uo pipefail/ {on=0} on {sub(/^# ?/, ""); print}' "$SELF"
  forge_fractal_help
  exit 0
fi
[ $# -ge 1 ] || die "usage: forge-parallel.sh <plan|auto|run|retry|accept|adopt|review|integrate|split|attribute|combine> <plan-dir> [...]"
CMD="$1"; shift
[ ! -f "${1:-}/no_ripwire" ] || export FORGE_RIPWIRE=off
if [ -d "${1:-}" ]; then
  PLAN_PATH="$(cd "$1" && pwd)"; shift; set -- "$PLAN_PATH" "$@"
fi
case "$CMD" in
  # `combine` takes a plan dir that does not exist yet and locks its sources itself.
  run|retry|integrate|accept|adopt|review|split|attribute)
    if [ "${FORGE_PLAN_LOCK:-}" != "${1:-}" ]; then
      exec python3 "$SKILL_DIR/scripts/forge-schedule.py" lock "$SELF" "$CMD" "$@"
    fi ;;
esac
# Every user-facing subcommand leaves one record in the run dataset, whatever its exit
# path (die included). Installed after the lock re-exec, so it fires once, in the process
# that did the work; `_task` and `_merge` are parts of a `run` and are not recorded alone.
case "$CMD" in
  plan|run|retry|integrate|accept|adopt|review|split|attribute|combine)
    FORGE_DATASET_INVOCATION="inv-$(date +%s)-$$-$RANDOM"; export FORGE_DATASET_INVOCATION
    FORGE_DATASET_STARTED="$(date +%s)"
    FORGE_DATASET_ARGV=("$@")
    trap 'forge_dataset_rc=$?; trap - EXIT; forge_dataset_invocation "$CMD" "$forge_dataset_rc" "$FORGE_DATASET_STARTED" ${FORGE_DATASET_ARGV[@]+"${FORGE_DATASET_ARGV[@]}"}; exit "$forge_dataset_rc"' EXIT ;;
esac
# Subcommands implemented in the optional libraries fail clearly if the library is absent.
need_fn() { declare -F "$1" >/dev/null || die "'$CMD' is not available: $1 is missing" 3; }
# A plan with a frozen profile asks its decision-model gates from that copy (`plan` does it itself,
# after it freezes). The internal subcommands inherit it from the `run` that started them.
case "$CMD" in run|retry|integrate|accept|adopt|review|split|attribute) forge_profile_export "${1:-}" ;; esac
case "$CMD" in
  plan)      do_plan "$@"; rc=$?; forge_update_notice; exit "$rc" ;;
  auto)      do_auto "$@" ;;
  run)
    # The signal handler lives here, at the pid the user started: do_run is a subshell, and
    # bash runs a trap only between commands, so it is started in the background and waited
    # for. A signal goes to it as TERM; do_run stops the scheduler or the verification.
    run_signalled=0 run_job=""
    trap 'run_signalled=1; [ -n "$run_job" ] && kill -TERM "$run_job" 2>/dev/null' TERM INT
    ( do_run "$@" ) <&0 &
    run_job=$!
    [ "$run_signalled" = 1 ] && kill -TERM "$run_job" 2>/dev/null
    run_wait "$run_job"; rc=$run_rc
    trap - TERM INT
    [ "$run_signalled" = 1 ] && [ "$rc" -gt 128 ] && rc=130
    # Older integrations' outcomes that came due (#120), unless the run was interrupted: bounded, never the exit code.
    [ "$run_signalled" = 1 ] || forge_outcomes_harvest "${1:-}"
    forge_update_notice; exit "$rc" ;;
  retry)
    # As run: the handler lives at the pid the user started, and do_retry runs in a background
    # job it waits for. A signal stops that job's whole tree (retry_stop, #129), and the retry
    # returns only once what it dispatched has stopped.
    retry_signalled=0 retry_job=""
    trap 'retry_signalled=1; [ -n "$retry_job" ] && retry_stop "$retry_job" "${1:-}" "${2:-}"' TERM INT
    ( do_retry "$@" ) <&0 &
    retry_job=$!
    [ "$retry_signalled" = 1 ] && retry_stop "$retry_job" "${1:-}" "${2:-}"
    run_wait "$retry_job"; rc=$run_rc
    trap - TERM INT
    [ "$retry_signalled" = 1 ] && [ "$rc" -le 128 ] && rc=143
    exit "$rc" ;;
  integrate) do_integrate "$@"; rc=$?; forge_outcomes_harvest "${1:-}"; exit "$rc" ;;
  accept)    need_fn do_accept;  do_accept "$@" ;;
  adopt)     need_fn do_adopt;   do_adopt "$@" ;;
  review)    need_fn do_review;  do_review "$@" ;;
  split)     need_fn do_split;   do_split "$@" ;;
  attribute) need_fn do_attribute; do_attribute "$@" ;;
  combine)   need_fn do_combine; do_combine "$@" ;;
  _task)     do_task "$@" ;;
  _merge)    merge_task "$@" ;;
  _prepare_retry) retry_for_scheduler "$@" ;;
  _qa_only)  qa_only_next "$@" ;;
  _waves)    compute_waves "$@" ;;
  _sweep)    sweep_plan "$@" ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
