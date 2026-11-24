#!/usr/bin/env bash
# forge-parallel.sh — decomposed forge runs: many dwarves working concurrently in
# isolated git worktrees, each task reviewed by its own QA pass, only passing work
# merged. Bash 3.2 entrypoints keep the task and merge contracts; a Python 3
# standard-library coordinator schedules ready tasks without wave barriers.
#
# Usage:
#   forge-parallel.sh plan <plan-dir> --repo <dir> [routing flags] [--decompose-level low|medium|high] [--ui]
#                                     [--qa-threshold P0|P1|P2|P3|none|default] [--profile NAME | --no-profile]
#                                     [--collapse-chains] [--split-depth N] [--split-children N] [--split-nodes N]
#   forge-parallel.sh auto <plan-dir> --repo <dir> --goal-file <file> [--planner <spec>] [--decompose-level L]
#                                     [--plan auto|always|never] [--one-task-difficulty low|medium|high]
#                                     [--no-collapse] [--approved] [plan flags...] [-- run flags...]
#   forge-parallel.sh run  <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa]
#                                     [--verify <command>] [--setup <command>] [--dry-run]
#                                     [--timeout S] [--retry-failed N] [--infra-retries M]
#                                     [--qa-threshold P0|P1|P2|P3|none|default] [--verify-retries N] [--no-forecast]
#                                     [--sweep P0|P1|P2|P3 [--sweep-plausible] [--sweep-max N]]
#                                     [--sweep-ahead | --no-sweep-ahead] [--attribute | --no-attribute]
#                                     [--auto-split [--split-planner <spec>] | --no-auto-split]
#   forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>] [--setup <command>]
#                                     [--answer <text> | --answer-file <file>]   (a NEEDS_INPUT task)
#                                     [--decide retry=N|threshold=Pn] [--dwarf <spec>] --reason "<why>" --approved
#                                                                              (a NEEDS_DECISION task)
#                                     [--decide split[=fresh] [--subtasks <file> | --planner <spec>] --reason "<why>" --approved]
#                                                (split the task into subtasks: any task that has not passed)
#                                     [--no-dispatch]   (prepare it and start nothing: the next `run` dispatches it)
#                                     [--timeout S] [--qa-threshold Pn|none|default] [--verify-retries N]
#                                     [--retry-failed N] [--infra-retries M]
#   forge-parallel.sh integrate <plan-dir> --approved [--verify-retries N] [--final-review <qa-spec>]
#   forge-parallel.sh review <plan-dir> [--qa <spec>] [--yolo-qa] [--qa-threshold Pn|none|default]
#   forge-parallel.sh attribute <plan-dir> [--force]   (find a merge that breaks the failed verification)
#   forge-parallel.sh verify <plan-dir> [--verify-retries N] [--verify-parallel N]   (verify the integration branch again, nothing else)
#   forge-parallel.sh accept <plan-dir> <task-id> --reason "<why>" --approved
#   forge-parallel.sh adopt <plan-dir> <commit|a..b> | --head --reason "<why>" [--qa <spec>] [--approved]
#                                     (a commit made outside forge, reviewed alone, comes into the run)
#   forge-parallel.sh split <plan-dir> <new-plan> --tasks <id,id> [--dry-run]
#   forge-parallel.sh combine <new-plan> <plan-a> <plan-b> [<plan>...] [--verify <command>] [--dry-run]
#   forge-parallel.sh next <plan-dir> [<new-plan>] [--planner <spec>] [plan flags...]
#                                     (plan the next stage of a goal larger than one plan, after <plan-dir> merged)
#   forge-parallel.sh _task <plan-dir> <task-id>     (internal; coordinator target)
#   forge-parallel.sh _group <plan-dir> <group-id>   (internal; one QA review of a review=group group, #140)
#   forge-parallel.sh _group_settle <plan-dir> <group-id> [--tasks a,b]
#                                                   (internal; send a FAIL group's implicated members back, #140)
#   forge-parallel.sh _merge_group <plan-dir> <group-id>
#                                                   (internal; merge a PASS group's members with one merge, #140)
#   forge-parallel.sh _qa_only <plan-dir> <task-id>  (internal; exit 0 when the next entry only re-runs QA)
#   forge-parallel.sh _waves <plan-dir>             (internal; rewrite waves.tsv after a live `forge control add`)
#   forge-parallel.sh _sweep <plan-dir> --severity Pn [--plausible] [--max N] [--check]
#                                                   (internal; plan a finding sweep round, print its task ids;
#                                                    --check only counts what a round would sweep)
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
#   --groups waves|FILE     opt-in (#140): groups.tsv from the computed waves, or from FILE; a
#                           groups.tsv already in the plan dir is used as is. Validated, its
#                           group qa resolved and frozen (groups.frozen); `run` refuses when
#                           it or a member's deps/files changed since
#   --group-review          with --groups waves: the derived groups get review=group (one QA
#                           review for the whole group; high-difficulty tasks stay out)
#   --group-max-tasks N     most members of a review group (default 6)
#   --group-max-diff-kb N   most diff a group review reads, in KB (default 256; kept in the plan)
#   --reviewer-session      opt-in experiment: a later review round continues the reviewer's
#                           earlier session (claude, openclaude, codex) when the QA spec is
#                           unchanged, and starts fresh whenever it cannot; each round is
#                           logged in tasks/<id>/review_sessions.tsv and tagged in the
#                           dataset. `--no-reviewer-session` turns it off (the default)
#   --reviewer-session-rate R  the same, on a sampled share R of the tasks only (implies
#                           --reviewer-session): round(R x n) tasks of each difficulty, picked by
#                           a hash of the run id and task id; the others review fresh and log
#                           `not-sampled`, so the two halves can be compared
#   --implementer-session   opt-in: a retry continues the implementer's earlier session
#                           (claude, openclaude) when its spec is unchanged and its work is
#                           still what the review judged, and starts fresh whenever it cannot;
#                           each attempt is logged in tasks/<id>/implementer_sessions.tsv.
#                           `--no-implementer-session` turns it off (the default)
#   --retry-per-model N     a task may fail N times (1-99) on each model the retry policy
#                           gives it, then it moves a rung up. Replaces --escalate-after for
#                           this plan. Kept in retry_per_model. `--no-retry-per-model` removes
#                           it; a re-plan with neither keeps the file
#   --after PREV            start this plan from plan PREV's integration branch (#143): PREV must be
#                           fully merged, on its accepted tip, not running, and in the same repo.
#                           Kept in `after` (PREV's path) and `integration.base`; plan creates
#                           forge/<run>-integration there. A re-plan without the flag keeps it
#   --retro | --no-retro   agents record what got in their way (retro, #132); plan file `retro_mode`
#                           (on or off; not `retro`, the notes directory). Without a flag the plan
#                           keeps its earlier choice, else (first plan only) takes the user
#                           `features` setting. The first plan with it on says what is recorded
#   --brief LEVEL           advisory, gated, strict or off (#135): one cheap read-only dispatch per
#                           task before it starts. Plan files `brief`, `brief_scope` (--brief-scope
#                           task|wave|plan, default task) and `brief_model` (--brief-model SPEC). On
#                           with no model is refused (exit 2); `off` writes `off` and drops the rest
#   --summaries on|off      a generated summary per task (#133); plan files `summaries` and
#                           `summaries_model` (--summaries-model SPEC); on with no model is refused.
#                           A flag beats the user setting (forge config show), which is read once, at
#                           the first plan; a re-plan without the flag keeps what the plan has, and an
#                           explicit off (--no-retro, --brief off, --summaries off) is kept as `off`
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
#   --split-depth N         recursive decomposition (#153): how many levels of subtasks a task may be
#                           split into (default 2; 0 turns splitting off)
#   --split-children N      most subtasks one split may make (default 6)
#   --split-nodes N         most subtasks in the whole run, over every level (default 24)
#                           The three limits are shown under the task table, kept in
#                           decomposition.json and frozen by the first run: a split that would pass
#                           one is recorded as `bounded` and the task runs as one task
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
#                           narrow review; the sweep plan is printed and kept in sweep/. A rerun
#                           that merged new findings sweeps them in a next round, never the same
#                           finding twice; what it does not fix stays for `forge issues`
#   --sweep-ahead           a task is also told the open findings in files it owns, to fix only
#                           on its way (kept for the plan; --no-sweep-ahead turns it off)
#   --attribute             when the run's combined verification fails, search the merge order for
#                           a merge that breaks it (kept for the plan; --no-attribute turns it off)
#   --auto-split            a task that stops for a decision on `split` is split into subtasks by the
#                           planner, within the plan's limits, without waiting for you; without it the
#                           task waits for your `retry --decide split`. The planner is --split-planner
#                           SPEC, else the one the plan was made with. Kept for the plan;
#                           --no-auto-split turns it off
#
# Subtasks (#153): a task can be split into subtasks, at plan time by the planner or later by
# `retry <plan> <task> --decide split`. The subtasks are a plan of their own in
# <plan-dir>/tasks/<task>/sub: they are routed with the plan's difficulty pools, own paths inside the
# task's files, run on the same scheduler under the run's one --max-parallel, and merge into the
# task's branch. The task is then reviewed on everything they produced. `split` keeps the task's
# existing work and the subtasks build on it; `split=fresh` starts over from the task's base and
# keeps the old work on refs/forge/kept/<run>/<task>. A subtask that fails is retried in its own
# plan (retry <plan-dir>/tasks/<task>/sub <subtask>); the next `run` of the plan picks the task up.
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
# Windows: re-run inside MSYS2 when started from Git Bash (no-op elsewhere).
. "$SKILL_DIR/scripts/forge-win.sh"
source "$SKILL_DIR/scripts/forge-lib-env.sh"
forge_ensure_python3
DISPATCH="$SKILL_DIR/scripts/forge-dispatch.sh"
MEMORY="$SKILL_DIR/scripts/forge-memory.sh"
source "$SKILL_DIR/scripts/forge-artifact.sh"
source "$SKILL_DIR/scripts/forge-metrics.sh"
source "$SKILL_DIR/scripts/forge-ui-options.sh"
# Sourced here, at the top: an option parser sourced from inside
# a function, after that function has already walked its arguments, cannot parse
# anything. Every --jev flag was rejected as "unknown option" until this line.
JEV_OPTS=0
[ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ] \
  && source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && JEV_OPTS=1
# Flags shared by several subcommands, then the helper libraries. Functions only, resolved
# at call time, so they may use die/note/field and friends defined below.
source "$SKILL_DIR/scripts/forge-parallel-options.sh"
for forge_lib in task gates verify attribute plans review adopt group decompose; do
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

render_results_table() { # render_results_table <plan>
  local w
  w="$(id_col_width "$1/results.tsv" "$1/tasks.tsv")"
  printf '  %-*s %-14s %s\n' "$w" id status branch
  awk -F'\t' -v w="$w" '{printf "  %-*s %-14s %s\n", w, $1, $2, $3}' "$1/results.tsv"
}

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

# `script` is a task kind (tasks/<id>/run), not a model. A pool that can hand it out would
# make a blank row look scripted without a run file, or run a model under that name.
forge_refuse_script_pool() { # forge_refuse_script_pool <flag> <comma-list>
  local spec
  for spec in $(printf '%s' "${2:-}" | tr ',' ' '); do
    [ "$spec" = script ] || continue
    die "the $1 pool names 'script', which is reserved for a task that runs tasks/<id>/run"
  done
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

# What `run` reads from the frozen profile when its flags do not say: the retry budgets.
forge_profile_run_values() { # forge_profile_run_values <plan-dir>
  local key val
  [ -f "$1/profile.json" ] || return 0
  while IFS="$(printf '\t')" read -r key val; do
    case "$key" in
      policy.retry_failed) [ "$PP_RETRY_FAILED_SET" = 1 ] || PP_RETRY_FAILED="$val" ;;
      policy.infra_retries) [ "$PP_INFRA_RETRIES_SET" = 1 ] || PP_INFRA_RETRIES="$val" ;;
    esac
  done <<EOF
$(forge_profile_py value "$1/profile.json" policy.retry_failed policy.infra_retries 2>/dev/null)
EOF
}

# --- retro, brief and summaries (#132, #135, #133): options frozen into the plan -------
# `plan` writes six plan files (retro_mode, brief, brief_scope, brief_model, summaries,
# summaries_model) that forge_side.settings(plan) reads at run time. The retro file is not
# called `retro`: <plan>/retro/ is where the notes are kept. For each, a flag of this call
# wins, then what the plan already has (a re-plan without the flag keeps it, as
# --reviewer-session is kept), then the user `features` setting, read here once. The user
# setting turns a feature on or off only before the plan is first planned (no waves.tsv):
# after that the plan is frozen, here as in forge_side.settings, and a feature a re-plan does
# not name stays as it was. (A flag that turns a feature on may still take its scope and
# model from the user setting.) A feature that is on writes its files. One that is off
# because a flag said so, or because the plan already said `off`, writes `off` into its
# switch file (retro_mode, brief, summaries), so a later re-plan or a plan made from this
# one cannot take it from the user setting again; its scope and model files go. One that is
# merely off by default (no flag, no file, the user setting off) writes nothing, which is
# what keeps a plan with every feature off exactly what it was before these options.
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
  # Planned before: the user setting no longer turns anything on or off (see above).
  if [ -e "$plan/waves.tsv" ]; then u_retro=off; u_brief=off; u_summ=off; fi
  local f_retro f_brief f_scope f_bmodel f_summ f_smodel
  f_retro="$(forge_feature_file "$plan" retro_mode)"; f_brief="$(forge_feature_file "$plan" brief)"
  f_scope="$(forge_feature_file "$plan" brief_scope)"; f_bmodel="$(forge_feature_file "$plan" brief_model)"
  f_summ="$(forge_feature_file "$plan" summaries)"; f_smodel="$(forge_feature_file "$plan" summaries_model)"

  # retro: `on`; `off` when a flag or the plan says so; else no file
  local retro="${RETRO_FLAG:-${f_retro:-$u_retro}}"
  FEAT_RETRO=""
  if [ "$retro" = on ]; then FEAT_RETRO=on
  elif [ -n "$RETRO_FLAG" ] || [ -n "$f_retro" ]; then FEAT_RETRO=off; fi

  # brief: the level, its scope and its model
  local level="${BRIEF_FLAG:-${f_brief:-$u_brief}}"
  case "$level" in advisory|gated|strict|off) ;; *) level=off ;; esac
  FEAT_BRIEF=""; FEAT_BRIEF_SCOPE=""; FEAT_BRIEF_MODEL=""
  if [ "$level" = off ] && { [ -n "$BRIEF_FLAG" ] || [ -n "$f_brief" ]; }; then FEAT_BRIEF=off; fi
  if [ "$level" != off ]; then
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
  if [ "$mode" = off ] && { [ -n "$SUMM_FLAG" ] || [ -n "$f_summ" ]; }; then FEAT_SUMM=off; fi
  if [ "$mode" = on ]; then
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

# The six flags are read in a pass of their own, before the main loop: the shared flags
# (--qa-threshold, ...) write into the plan dir as they are parsed, so a refusal that came
# after them would leave their files behind. Here nothing has been written yet, and every bad
# value or missing model ends the call with the plan dir untouched.
forge_features_scan() { # forge_features_scan "$@": sets the *_FLAG variables of do_plan
  while [ $# -gt 0 ]; do
    case "$1" in
      --retro)        RETRO_FLAG=on ;;
      --no-retro)     RETRO_FLAG=off ;;
      --brief)
        case "${2:-}" in advisory|gated|strict|off) BRIEF_FLAG="$2"; shift ;;
          *) die "--brief must be advisory, gated, strict or off" ;; esac ;;
      --brief-scope)
        case "${2:-}" in task|wave|plan) BRIEF_SCOPE_FLAG="$2"; shift ;;
          *) die "--brief-scope must be task, wave or plan" ;; esac ;;
      --brief-model)
        [ -n "${2:-}" ] || die "--brief-model needs a model spec"
        BRIEF_MODEL_FLAG="$2"; shift ;;
      --summaries)
        case "${2:-}" in on|off) SUMM_FLAG="$2"; shift ;;
          *) die "--summaries must be on or off" ;; esac ;;
      --summaries-model)
        [ -n "${2:-}" ] || die "--summaries-model needs a model spec"
        SUMM_MODEL_FLAG="$2"; shift ;;
    esac
    shift
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

# 0 when this plan records retro notes. settings() is the switch: the plan file, the user
# setting before waves.tsv, and FORGE_RETRO=off. Auto's planner is dispatched before `plan`
# writes retro_mode, so AUTO_RETRO_FLAG (on, off, or empty) carries the flag that call will
# hand to plan. Off from the environment still wins. Prints nothing and writes nothing.
forge_retro_on() { # forge_retro_on <plan>
  local flag="${AUTO_RETRO_FLAG:-}"
  PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$1" "$flag" >/dev/null <<'PY'
import os, sys
import forge_side
plan, flag = sys.argv[1], sys.argv[2]
if os.environ.get('FORGE_RETRO', '').strip().lower() == 'off':
    sys.exit(1)
if flag == 'on':
    sys.exit(0)
if flag == 'off':
    sys.exit(1)
sys.exit(0 if forge_side.settings(plan).get('retro') else 1)
PY
}

# The retro instruction, printed so it can be the last block of a prompt. Nothing when retro
# is off, which is what keeps that prompt byte-identical.
forge_retro_prompt() { # forge_retro_prompt <plan> <role>
  forge_retro_on "$1" || return 0
  echo
  python3 "$SKILL_DIR/scripts/forge-contract.py" retro --role "$2" \
    || note "retro: could not write the $2 instruction"
  return 0
}

# One dispatch's notes. A failure is one warning and never the caller's result: the status
# and the exit code stay whatever the dispatch already decided. No task for a whole-run
# dispatch (the planner, the final review). The inbox is the dwarf worktree's.
forge_retro_record() { # forge_retro_record <plan> <role> <dir> [task] [attempt] [inbox]
  forge_retro_on "$1" || return 0
  local plan="$1" role="$2" dir="$3" task="${4:-}" attempt="${5:-}" inbox="${6:-}"
  local cmd=(python3 "$SKILL_DIR/scripts/forge-retro.py" record "$plan" --role "$role" --dir "$dir")
  [ -n "$task" ] && cmd+=(--task "$task")
  [ -n "$attempt" ] && cmd+=(--attempt "$attempt")
  [ -n "$inbox" ] && cmd+=(--inbox "$inbox")
  if ! "${cmd[@]}" >/dev/null 2>&1; then
    note "retro: could not record the $role notes${task:+ for $task}"
  fi
  return 0
}

# Rewrite retro.md. Quiet: integrate and review regenerate after their dispatches, and the
# run's own summary line is forge_retro_summary_line. A failure is one warning.
forge_retro_report() { # forge_retro_report <plan>
  forge_retro_on "$1" || return 0
  python3 "$SKILL_DIR/scripts/forge-retro.py" report "$1" >/dev/null 2>&1 \
    || note "retro: could not write the report"
  return 0
}

# Write retro.md and print the one summary line when the report has any pattern. Nothing
# when it does not, or when retro is off (no call, no file).
forge_retro_summary_line() { # forge_retro_summary_line <plan>
  forge_retro_on "$1" || return 0
  local json
  json="$(python3 "$SKILL_DIR/scripts/forge-retro.py" report "$1" --json 2>/dev/null)" || {
    note "retro: could not write the report"
    return 0
  }
  printf '%s' "$json" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
plan = sys.argv[1]
n = u = f = 0
for group in doc.get("groups") or []:
    k = len(group.get("patterns") or [])
    n += k
    if group.get("id") == "user":
        u += k
    elif group.get("id") == "forge":
        f += k
if n:
    sys.stdout.write("retro: %d patterns (%d you can fix, %d for forge): %s/retro.md\n" % (n, u, f, plan))
' "$1"
  return 0
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

# --- groups (#140) -----------------------------------------------------------------
# groups.tsv is the plan's frozen sets of tasks (scripts/forge_groups, forge-groups.py). Opt-in:
# with no groups.tsv, no --groups and no groups.frozen, nothing here runs or writes.
forge_groups_py() { python3 "$SKILL_DIR/scripts/forge-groups.py" "$@"; }

# forge_groups_id_of <plan> <task>: the id of the task's group, empty when it is in none.
forge_groups_id_of() { forge_groups_py of "$1" "$2" 2>/dev/null | awk -F'\t' 'NR == 1 { print $1 }'; }

# forge_groups_die <message from forge-groups.py>: its one-line message, without the prefix, exit 2.
forge_groups_die() { local msg="$1"; die "${msg#forge-groups: }" 2; }

# forge_groups_have_review <plan>: 0 when some group has review=group.
forge_groups_have_review() {
  [ -f "$1/groups.tsv" ] || return 1
  forge_groups_py show "$1" 2>/dev/null | awk -F'\t' '$2 == "group" { found = 1 } END { exit !found }'
}

# forge_groups_max_tasks <plan> [flag]: the flag, else the plan's group_max_tasks, else 6.
forge_groups_max_tasks() {
  local value="${2:-}"
  [ -n "$value" ] || value="$(head -1 "$1/group_max_tasks" 2>/dev/null)"
  printf '%s' "${value:-6}"
}

# forge_groups_plan <plan> <--groups value> <--group-review 0|1> <--group-max-tasks value>
forge_groups_plan() {
  local plan="$1" src="$2" review="$3" max="$4" out=""
  if [ "$src" = waves ]; then
    out="$(forge_groups_py derive "$plan" --review "$([ "$review" = 1 ] && echo group || echo task)" \
      --max-tasks "$(forge_groups_max_tasks "$plan" "$max")" 2>&1)" || forge_groups_die "$out"
  elif [ -n "$src" ]; then
    cp "$src" "$plan/groups.tsv" || die "could not copy '$src' to $plan/groups.tsv" 3
  fi
  if [ ! -f "$plan/groups.tsv" ] && [ ! -f "$plan/groups.frozen" ]; then
    return 0
  fi
  # validate, then resolve (writes the group qa back), then freeze
  out="$(forge_groups_py validate "$plan" --max-tasks "$(forge_groups_max_tasks "$plan" "$max")" 2>&1)" \
    || forge_groups_die "$out"
  out="$(forge_groups_py resolve "$plan" 2>&1)" || forge_groups_die "$out"
  out="$(forge_groups_py freeze "$plan" 2>&1)" || forge_groups_die "$out"
  return 0
}

# Group flags are refused before do_plan's loop writes a shared flag's file.
# A bad value, or --group-review without --groups waves, must not leave --qa-threshold
# (or any earlier shared flag) written into the plan dir.
forge_groups_flags_early() {
  local review=0 src=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --groups)
        [ -n "${2:-}" ] || die "--groups needs waves or a groups.tsv file"
        if [ "$2" != waves ]; then
          [ -f "$2" ] && [ -r "$2" ] || die "--groups '$2' is not a readable file (use waves, or a groups.tsv)"
        fi
        src="$2"; shift 2 ;;
      --group-review) review=1; shift ;;
      --group-max-tasks)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,6}$' && [ "$((10#$2))" -ge 1 ] \
          || die "--group-max-tasks needs a positive integer"
        shift 2 ;;
      --group-max-diff-kb)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,9}$' && [ "$((10#$2))" -ge 1 ] \
          || die "--group-max-diff-kb needs a positive integer"
        shift 2 ;;
      --repo|--after|--planner|--decompose-level|--profile|--shadow-qa|--shadow-rate|--explore|--dwarf|--dwarf-high|--dwarf-medium|--dwarf-low|--qa|--qa-high|--qa-medium|--qa-low|--pre-review-parallel|--reviewer-session-rate|--fast|--brief|--brief-scope|--brief-model|--summaries|--summaries-model|--qa-threshold|--max-attempts|--converge-window|--gate|--decide-budget)
        if [ $# -ge 2 ]; then shift 2; else shift; fi ;;
      *) shift ;;
    esac
  done
  [ "$review" = 0 ] || [ "$src" = waves ] || die "--group-review goes with --groups waves (it sets the review of the groups waves derives)"
}

# render_groups <plan>: the table's groups block and, when a group reviews its members together,
# the reviews line. Nothing without a groups.tsv.
render_groups() {
  [ -f "$1/groups.tsv" ] || return 0
  python3 - "$SKILL_DIR/scripts" "$1" "$(printf '%q' "$1")" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import forge_groups
try:
    groups = forge_groups.load(sys.argv[2])
except forge_groups.GroupError:
    groups = []
if groups:
    qas = ['qa=' + g.qa if g.qa != '-' else '-' for g in groups]
    ids = max(len(g.id) for g in groups)
    qa_w = max(len(q) for q in qas)
    members = [', '.join(g.members) for g in groups]
    mem_w = max(len(m) for m in members)
    print('groups:')
    for g, q, m in zip(groups, qas, members):
        goal = g.goal if g.goal != '-' else '(no goal)'
        print('  %-*s  %-12s  %-*s  %-*s  — %s' % (ids, g.id, 'review=' + g.review, qa_w, q, mem_w, m,
                                                       goal if g.title == '-' else '%s: %s' % (g.title, goal)))
    missing = [g.id for g in groups if g.goal == '-']
    if missing:
        # A goal is what a group review and a brief prompt say the group is for (#136).
        print('%d group(s) have no goal (%s): set one with forge group set %s GID --goal "..."'
              % (len(missing), ', '.join(missing), sys.argv[3]))
    print()
PY
}

# forge_groups_review_counts <plan>: "<groups> <members>" of the review=group groups, or nothing.
forge_groups_review_counts() {
  [ -f "$1/groups.tsv" ] || return 0
  forge_groups_py show "$1" 2>/dev/null | awk -F'\t' '$2 == "group" { g++; m += split($4, a, ",") } END { if (g) print g, m }'
}

do_plan() {
  local PLAN="${1:?plan needs a plan dir}"; shift
  local PF_NAME="" PF_NONE=0 PF_QT=0
  forge_profile_precheck "$PLAN" "$@"
  local REPO="" D_ANY="" D_HI="" D_MD="" D_LO="" Q_ANY="" Q_HI="" Q_MD="" Q_LO=""
  PP_INFRA_FALLBACK=""
  local PLANNER="" NOMEM=0 jev_rc=0 DLEVEL="" SHADOW_QA="" SHADOW_RATE="" EXPLORE="" FAIL_FIRST="" REVIEWER_SESSION="" RS_RATE="" IMPLEMENTER_SESSION="" PRE_REVIEW="" PRE_REVIEW_PARALLEL=""
  local FAST_GIVEN=0 FAST_PARSED=""
  local RETRO_FLAG="" BRIEF_FLAG="" BRIEF_SCOPE_FLAG="" BRIEF_MODEL_FLAG="" SUMM_FLAG="" SUMM_MODEL_FLAG=""
  local RETRY_PER_MODEL=""
  local COLLAPSE=0 COLLAPSED_NOW=0 FEAT_ERR="" GRP_SRC="" GRP_REVIEW=0 GRP_MAX="" GRP_DIFF=""
  local CHAIN_PREV="" CHAIN_BASE="" CHAIN_MOVE=0
  local SPLIT_DEPTH="" SPLIT_CHILDREN="" SPLIT_NODES="" DECOMPOSE_LINES=""
  # The retro, brief and summaries choices, settled before anything is written: a refusal
  # (a bad value, no model, a spec that does not resolve) leaves the plan dir as it was.
  forge_features_scan "$@"
  [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
  forge_features_resolve "$PLAN" || die "$FEAT_ERR" 2
  # --after (#143), settled before anything is written: every refusal leaves the plan dir and the
  # branches as they were. Its files and the integration branch are written below, once the repo is known.
  plans_chain_early "$PLAN" "$@"
  # Before the loop: --qa-threshold and --max-attempts write their files while parsing,
  # so a group flag refused later on the same command would leave those files behind.
  forge_groups_flags_early "$@"
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)         REPO="${2:?}"; shift 2 ;;
      --after)        [ -n "${2:-}" ] || die "--after needs a plan directory" 2; shift 2 ;;
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
      --groups)
        [ -n "${2:-}" ] || die "--groups needs waves or a groups.tsv file"
        if [ "$2" != waves ]; then
          [ -f "$2" ] && [ -r "$2" ] || die "--groups '$2' is not a readable file (use waves, or a groups.tsv)"
        fi
        GRP_SRC="$2"; shift 2 ;;
      --group-review) GRP_REVIEW=1; shift ;;
      --group-max-tasks)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,6}$' && [ "$((10#$2))" -ge 1 ] || die "--group-max-tasks needs a positive integer"
        GRP_MAX="$((10#$2))"; shift 2 ;;
      --group-max-diff-kb)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,9}$' && [ "$((10#$2))" -ge 1 ] || die "--group-max-diff-kb needs a positive integer"
        GRP_DIFF="$((10#$2))"; shift 2 ;;
      --collapse-chains) COLLAPSE=1; shift ;;
      --split-depth|--split-children|--split-nodes)
        printf '%s' "${2:-}" | grep -Eq '^[0-9]{1,3}$' || die "$1 needs a whole number" 2
        case "$1" in
          --split-depth) SPLIT_DEPTH="$((10#$2))" ;;
          --split-children) SPLIT_CHILDREN="$((10#$2))" ;;
          *) SPLIT_NODES="$((10#$2))" ;;
        esac
        shift 2 ;;
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
      --no-reviewer-session) REVIEWER_SESSION=off; RS_RATE=""; shift ;;
      --reviewer-session-rate)
        printf '%s' "${2:-}" | grep -Eq '^(0?\.[0-9]+|0|1|1\.0+)$' || die "--reviewer-session-rate needs a share between 0 and 1"
        REVIEWER_SESSION=on; RS_RATE="$2"; shift 2 ;;
      --retry-per-model)
        printf '%s' "${2:-}" | grep -Eq '^[1-9][0-9]?$' \
          || die "--retry-per-model needs an integer from 1 to 99" 2
        RETRY_PER_MODEL="$((10#$2))"; shift 2 ;;
      --no-retry-per-model) RETRY_PER_MODEL=off; shift ;;
      --implementer-session)    IMPLEMENTER_SESSION=on; shift ;;
      --no-implementer-session) IMPLEMENTER_SESSION=off; shift ;;
      --fast)
        [ -n "${2:-}" ] || die "--fast needs a comma list of roles (dwarf, qa, planner, review), all or none"
        forge_fast_parse "$2" plan
        FAST_GIVEN=1
        shift 2 ;;
      --retro|--no-retro) shift ;;                      # read by forge_features_scan
      --brief|--brief-scope|--brief-model|--summaries|--summaries-model) shift 2 ;;
      *)
        # `case` is not a loop, so `continue` here belongs to the enclosing while.
        # The shared flags write into the plan dir (--qa-threshold), so it must exist first.
        [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
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
        forge_removed_flag "$1"
        die "plan: unknown option '$1'"
        ;;
    esac
  done
  [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
  [ "$GRP_REVIEW" = 0 ] || [ "$GRP_SRC" = waves ] || die "--group-review goes with --groups waves (it sets the review of the groups waves derives)"
  # The decomposition limits (#153): a bad value, or a change after the first run froze them, refuses the call.
  forge_decompose_limits "$PLAN" "$SPLIT_DEPTH" "$SPLIT_CHILDREN" "$SPLIT_NODES"
  # The profile (#48) first: its gates and budget count for every decision-model step below,
  # and its values stand in for the flags this call did not give.
  forge_profile_plan
  # Pools are flags or the profile just applied. `script` in either is a reserved word.
  forge_refuse_script_pool --dwarf "$D_ANY"
  forge_refuse_script_pool --dwarf-high "$D_HI"
  forge_refuse_script_pool --dwarf-medium "$D_MD"
  forge_refuse_script_pool --dwarf-low "$D_LO"
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
  # Group options (#140): kept as plan files, only when given, so a re-plan, retry, rerun or split
  # keeps what the plan was made with.
  [ -z "$GRP_MAX" ] || printf '%s\n' "$GRP_MAX" > "$PLAN/group_max_tasks"
  [ -z "$GRP_DIFF" ] || printf '%s\n' "$GRP_DIFF" > "$PLAN/group_max_diff_kb"
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
  # A sampled share (--reviewer-session-rate): which tasks continue, fixed now so a re-plan or a
  # retry never moves a task between the two halves. `--reviewer-session` alone means every task.
  if [ -n "$RS_RATE" ]; then
    printf '%s\n' "$RS_RATE" > "$PLAN/reviewer_session_rate"
    reviewer_session_pick "$PLAN" "$RS_RATE" > "$PLAN/reviewer_session_tasks.tmp" \
      && mv "$PLAN/reviewer_session_tasks.tmp" "$PLAN/reviewer_session_tasks" \
      || die "could not pick the reviewer-session tasks" 3
  elif [ "$REVIEWER_SESSION" = on ] || [ "$REVIEWER_SESSION" = off ]; then
    rm -f "$PLAN/reviewer_session_rate" "$PLAN/reviewer_session_tasks"
  fi
  # Implementer-session continuation (opt-in, #90): the same `on` file, read the same way.
  case "$IMPLEMENTER_SESSION" in on) printf '%s\n' on > "$PLAN/implementer_session" ;; off) rm -f "$PLAN/implementer_session" ;; esac
  # N failures on each rung (#141). Omitted on a re-plan, so an earlier choice stays.
  case "$RETRY_PER_MODEL" in
    '') ;;
    off) rm -f "$PLAN/retry_per_model" ;;
    *) printf '%s\n' "$RETRY_PER_MODEL" > "$PLAN/retry_per_model" || die "could not write $PLAN/retry_per_model" ;;
  esac
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
  # Every plan, --ui or not: forge-ui's view of this repo's runs reads the registry. A sub-plan (#153)
  # is reached through the plan it belongs to.
  forge_is_sub_plan "$PLAN" || forge_ui_register "$PLAN"
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  # Persist plan choices for later run and retry processes.
  { [ "$NOMEM" = 1 ] || forge_repo_memory_off "$REPO"; } && : > "$PLAN/no_memory"
  [ -n "$PLANNER" ] && printf '%s\n' "$PLANNER" > "$PLAN/planner"
  # dwarf-routing.json; a plan made before 0.23 kept these pools under another name, read once here.
  python3 - "$PLAN/dwarf-routing.json" "$(forge_pools_file "$PLAN")" "$D_ANY" "$D_LO" "$D_MD" "$D_HI" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); old=Path(sys.argv.pop(2)); data=json.loads(old.read_text()) if old.exists() else {}
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
  if [ ! -s "$PLAN/after" ] && [ -z "${CHAIN_PREV:-}" ] && [ -n "$(user_tree_changes "$REPO")" ]; then
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

  # A refused plan (a task left UNASSIGNED below) puts tasks.tsv back as it was, so nothing it
  # routed, such as the default reviewer, is frozen into the next plan (#178). The copy is only
  # made for a refusal, under one name, so an exit in between leaves at most this one file,
  # which the next plan removes.
  local tmp="$PLAN/.tasks.tsv.new" orig_tasks="$PLAN/.tasks.tsv.orig"
  : > "$tmp"
  rm -f "$orig_tasks"
  local hi_i=0 md_i=0 lo_i=0 an_i=0 qhi_i=0 qmd_i=0 qlo_i=0 qan_i=0 unassigned=0 qa_kept_msg="" qa_flag_spec=""
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
    # `script` is an override too: no pool, no exploration, and the cell stays `script`.
    [ "$dw" = "UNASSIGNED" ] && dw="-"
    if [ "$dw" = "script" ]; then
      [ -x "$PLAN/tasks/$id/run" ] \
        || die "task '$id' is a script task but tasks/$id/run is missing or not executable"
    elif [ "$dw" = "-" ] || [ -z "$dw" ]; then
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
          --tier "$diff" --epsilon "${EXPLORE:-0}" --default "$dw" --pools "$(forge_pools_file "$PLAN")" 2>/dev/null)"
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
    else
      qa_flag_spec=""
      case "$diff" in
        high)   qa_flag_spec="${Q_HI:-$Q_ANY}" ;;
        medium) qa_flag_spec="${Q_MD:-$Q_ANY}" ;;
        low)    qa_flag_spec="${Q_LO:-$Q_ANY}" ;;
      esac
      if [ -n "$qa_flag_spec" ] && ! list_has "$qa" "$qa_flag_spec"; then
        qa_kept_msg="${qa_kept_msg:+$qa_kept_msg, }$id ($qa)"
      fi
    fi
    # A column runs one spec; a list there would only fail later, at its first dispatch.
    case "$dw" in *,*) die "task '$id': the dwarf column names one spec, not a list ('$dw'); pool models with a --dwarf flag" ;; esac
    case "$qa" in *,*) die "task '$id': the qa column names one spec, not a list ('$qa'); pool reviewers with a --qa flag" ;; esac
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$deps" "$diff" "$files" "$dw" "$qa" "$title" >> "$tmp"
  done < "$T"
  [ "$unassigned" -eq 0 ] || cp "$T" "$orig_tasks"
  mv "$tmp" "$T"
  # Which cells routing filled in, so a re-plan under another profile replaces those and no others.
  # Kept current through a --no-profile re-plan too, once a profile has recorded any.
  if [ "$unassigned" -eq 0 ] && { [ -f "$PLAN/profile.json" ] || [ -f "$PLAN/routed.json" ]; }; then
    forge_profile_py routed "$PLAN" 2>/dev/null || true
  fi

  compute_waves "$PLAN"
  # Only once tasks.tsv has passed its checks: a refused plan leaves no after, integration.base or branch.
  plans_chain_store "$PLAN" "$REPO" "$(cat "$PLAN/run_id")"

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

  # Groups (#140): after the last wave computation and the last change to a task's deps, so the
  # frozen fingerprint is of the table the run will use.
  forge_groups_plan "$PLAN" "$GRP_SRC" "$GRP_REVIEW" "$GRP_MAX"

  # Recursive decomposition (#153), once the table is routed: the splits a planner proposed become
  # sub-plans within the limits, routed with this plan's pools, and every other task is recorded as
  # kept whole. Not for a plan that is about to be refused for an unassigned task.
  [ "$unassigned" -gt 0 ] || forge_decompose_settle "$PLAN"

  # The brief each task starts from: a change after this is logged at the next dispatch (#220).
  local seen_id
  for seen_id in $(all_ids "$T"); do
    [ ! -d "$PLAN/tasks/$seen_id" ] || record_seen_inputs "$PLAN/tasks/$seen_id" || true
  done

  render_table "$PLAN"
  forge_decompose_table "$PLAN"
  if [ -n "$qa_kept_msg" ]; then
    echo "note: --qa left existing reviewer(s) unchanged on: $qa_kept_msg (set qa to '-' in tasks.tsv to let --qa fill them)"
    echo
  fi
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
    mv -f "$orig_tasks" "$T"
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

# Under the dispatch estimate when the plan set --retry-per-model (#141). Nothing without the file.
# The worst case and the tasks the attempt cap cuts short come from forge-route.py worst-case.
render_retry_per_model() { # render_retry_per_model <plan>
  [ -f "$1/retry_per_model" ] || return 0
  python3 - "$SKILL_DIR/scripts/forge-route.py" "$1" <<'PY'
import json, re, subprocess, sys
route, plan = sys.argv[1], sys.argv[2]
try:
    n = open(plan + '/retry_per_model', encoding='utf-8').read().strip()
except OSError:
    sys.exit(0)
# The scheduler and forge-route.py read only 1-99 and treat anything else as no file (#158): a
# hand-edited value is named as ignored, never shown as the setting with a worst case that assumes it.
if not re.fullmatch(r'[0-9]{1,9}', n) or not 1 <= int(n) <= 99:
    print('retry per model: ignored, %s holds %r and not a number from 1 to 99; the run uses --escalate-after '
          '(plan --retry-per-model N sets it, --no-retry-per-model removes the file)' % (plan + '/retry_per_model', n[:40]))
    sys.exit(0)
n = str(int(n))
print('retry per model: %s (a task may fail %s times on each model the retry policy gives it, then it moves a rung up)' % (n, n))
try:
    raw = subprocess.check_output([sys.executable, route, 'worst-case', '--plan', plan], text=True)
    data = json.loads(raw)
except (OSError, subprocess.CalledProcessError, ValueError):
    sys.exit(0)
print('worst case with retry-per-model %s: %s dispatches' % (n, data.get('total', 0)))
capped = data.get('capped') or []
if not capped:
    sys.exit(0)
cap = 8
try:
    text = open(plan + '/max_attempts', encoding='utf-8').read().strip()
except OSError:
    text = ''
if re.fullmatch(r'[0-9]{1,9}', text):
    cap = int(text)
print('max-attempts %s stops these tasks before the top of their ladder: %s (plan --max-attempts N raises it)'
      % (cap, ', '.join(str(task) for task in capped)))
PY
}

render_table() {
  local PLAN="$1" T="$1/tasks.tsv" W="$1/waves.tsv"
  local run_id; run_id="$(cat "$PLAN/run_id")"
  local n; n="$(all_ids "$T" | wc -l | tr -d ' ')"
  local waves; waves="$(awk -F'\t' '{print $1}' "$W" | sort -u | wc -l | tr -d ' ')"
  echo "run forge/$run_id   repo $(cat "$PLAN/repo")"
  plans_chain_line "$PLAN"
  [ -s "$PLAN/goal.txt" ] && echo "goal: $(head -1 "$PLAN/goal.txt")"
  # A scripted task runs tasks/<id>/run: no implementer dispatch. Its review still counts.
  local dwarfs scripted=0 script_ids="" sid
  dwarfs="$(awk -F'\t' '!/^#/ && NF && $5 != "script" { c++ } END { print c + 0 }' "$T")"
  while IFS= read -r sid; do
    [ -n "$sid" ] || continue
    scripted=$((scripted + 1))
    script_ids="${script_ids:+$script_ids, }$sid"
  done <<EOF
$(awk -F'\t' '!/^#/ && NF && $5 == "script" { print $1 }' "$T")
EOF
  local est=$((dwarfs + n)) extra=""
  if [ -s "$PLAN/planner" ]; then
    est=$((est+1)); extra=" + 1 planner ($(cat "$PLAN/planner"))"
  fi
  # A review=group group is reviewed once for all its members (#140): the estimate counts that.
  local grp_counts grp_n=0 grp_m=0 reviews="$n"
  grp_counts="$(forge_groups_review_counts "$PLAN")"
  if [ -n "$grp_counts" ]; then
    read -r grp_n grp_m <<<"$grp_counts"
    reviews=$((n - grp_m + grp_n))
    est=$((dwarfs + reviews + (est - dwarfs - n)))
  fi
  echo "tasks: $n   waves: $waves   estimated dispatches: $est ($dwarfs dwarf + $reviews qa$extra)"
  [ -z "$grp_counts" ] || echo "reviews: $reviews ($grp_n groups replace $grp_m task reviews)"
  if [ "$scripted" -gt 0 ]; then
    echo "scripted tasks: $scripted ($script_ids) run tasks/<id>/run; no implementer model"
  fi
  render_retry_per_model "$PLAN"
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
  if [ -f "$PLAN/reviewer_session" ] && [ -f "$PLAN/reviewer_session_rate" ]; then
    echo "reviewer-session: on for $(grep -c . "$PLAN/reviewer_session_tasks" 2>/dev/null || echo 0) of $(all_ids "$PLAN/tasks.tsv" | grep -c .) tasks (a sampled $(cat "$PLAN/reviewer_session_rate")); the others review fresh and log not-sampled, to compare"
  elif [ -f "$PLAN/reviewer_session" ]; then
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
  local w id dws starred=0 id_w pad
  id_w="$(id_col_width "$T")"
  printf -v pad '%*s' $((id_w + 3)) ''
  for w in $(awk -F'\t' '{print $1}' "$W" | sort -un); do
    local cnt; cnt="$(awk -F'\t' -v w="$w" '$1==w' "$W" | wc -l | tr -d ' ')"
    echo "wave $w ($cnt parallel)"
    printf '  %-*s %-7s %-24s %-14s %s\n' "$id_w" id diff dwarf qa files
    for id in $(awk -F'\t' -v w="$w" '$1==w {print $2}' "$W"); do
      dws="$(field "$T" "$id" dwarf)"
      if [ -s "$PLAN/tasks/$id/route.explored" ] && [ "$(cat "$PLAN/tasks/$id/route.explored")" = "$dws" ]; then
        dws="$dws*"; starred=1
      fi
      printf '  %-*s %-7s %-24s %-14s %s\n' \
        "$id_w" "$id" "$(field "$T" "$id" difficulty)" "$dws" \
        "$(field "$T" "$id" qa)" "$(field "$T" "$id" files)"
      # The approach is shown here because this gate is the only moment a human can
      # change the plan for free. After dispatch, changing it costs a whole run.
      if [ -s "$PLAN/tasks/$id/approach.md" ]; then
        sed "s/^/${pad}| /" "$PLAN/tasks/$id/approach.md"
      fi
    done
    echo
  done
  # The model each spec runs today (#187): an alias moves to newer models over time, so the
  # table says which version `sol` or `astra` is before anything is spent.
  local models_block
  models_block="$(python3 "$SKILL_DIR/scripts/forge_models.py" legend "$T" 2>/dev/null)" || models_block=""
  [ -z "$models_block" ] || printf '%s\n\n' "$models_block"
  render_groups "$PLAN"
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
      # A scripted task has no implementer model. Its reviewer is still checked.
      if [ "$role" = dwarf ] && [ "$spec" = script ]; then
        continue
      fi
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
        print(specs[0]); break' "$(forge_pools_file "$1")" 2>/dev/null)"
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
    local id_w
    id_w="$(id_col_width "$T")"
    printf '%-*s %-7s %-11s %s\n' "$id_w" id diff status files
    for id in $(all_ids "$T"); do
      if [ "$id" = "$me" ]; then st="THIS ONE"
      elif [ -s "$PLAN/tasks/$id/reviewed.commit" ] && git -C "$(cat "$PLAN/repo")" merge-base --is-ancestor "$(cat "$PLAN/tasks/$id/reviewed.commit")" "$(cat "$PLAN/tasks/$me/base_ref")"; then st=MERGED
      else st="NOT IN BASE"; fi
      printf '%-*s %-7s %-11s %s\n' "$id_w" "$id" "$(field "$T" "$id" difficulty)" "$st" "$(field "$T" "$id" files)"
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
  local PLAN="$1" REPO="$2" wt="$3" tdir="$4" id="$5" cmd before_ignored=""
  # Every worktree forge makes reaches here once, right after it exists, so the submodules'
  # files are in place before `.forge/setup` runs. The outcome lands in <tdir>/submodules.txt.
  populate_submodules "$REPO" "$wt" "$tdir" "$id"
  cmd="$(worktree_setup_command "$PLAN" "$REPO")"
  [ -n "$cmd" ] || return 0
  before_ignored="$(mktemp "${TMPDIR:-/tmp}/forge-setup-ignored.XXXXXX" 2>/dev/null || true)"
  [ -z "$before_ignored" ] || forge_capture_ignored_before "$wt" "$before_ignored"
  if (cd "$wt" && eval "$cmd") >"$tdir/setup.out" 2>&1; then
    note "$id: worktree setup ok"
  else
    note "$id: worktree setup failed (continuing) — see $tdir/setup.out"
  fi
  if [ -n "$before_ignored" ]; then
    forge_record_setup_generated "$wt" "$before_ignored"
    rm -f "$before_ignored"
  fi
}

add_task_worktree() { # add_task_worktree <repo> <wt> <br> <base>: on <br> when it exists, else a new <br> at <base>
  if (cd "$1" && git rev-parse --verify --quiet "$3" >/dev/null 2>&1); then
    (cd "$1" && git worktree add "$2" "$3")
  else
    (cd "$1" && git worktree add -b "$3" "$2" "$4")
  fi
}

ensure_worktree() { # ensure_worktree <repo> <wt> <br> <base> <tdir> <plan> <id>
  local REPO="$1" wt="$2" br="$3" base="$4" tdir="$5" PLAN="$6" id="$7"
  [ ! -e "$wt/.git" ] || return 0
  # A worktree directory deleted by hand leaves its registration behind, and that registration
  # blocks `worktree add` with "already exists": prune, then add again. Only after a failed add:
  # tasks start together, and a prune while another task's add is midway removes that task's
  # half-made registration ("could not open '.git/worktrees/<id>/gitdir'"). The second add also
  # recovers from a prune that another forge process ran at that moment.
  add_task_worktree "$REPO" "$wt" "$br" "$base" >"$tdir/worktree.out" 2>&1 || {
    (cd "$REPO" && git worktree prune >/dev/null 2>&1)
    add_task_worktree "$REPO" "$wt" "$br" "$base" >>"$tdir/worktree.out" 2>&1
  } || return 1
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
# The run summary's scope drift of one task, on one line (#220): a path the task's current brief names
# (prompt.md or approach.md, as a person may have added it mid-run) is marked as such, apart from paths
# nothing explains.
drift_labelled() { # drift_labelled <tdir>
  local tdir="$1" f out="" brief=()
  [ ! -f "$tdir/prompt.md" ] || brief+=("$tdir/prompt.md")
  [ ! -f "$tdir/approach.md" ] || brief+=("$tdir/approach.md")
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "${#brief[@]}" -gt 0 ] && grep -qF -- "$f" "${brief[@]}" 2>/dev/null; then
      out="${out:+$out }$f (named in the brief)"
    else
      out="${out:+$out }$f"
    fi
  done < "$tdir/drift.txt"
  printf '%s' "$out"
}

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
  case "$src" in review|prereview|bounce|message|answer|prompt) ;; *) src=review ;; esac
  if [ -e "$trail" ]; then
    [ -f "$trail" ] && [ -r "$trail" ] || return 1
    awk -F'\t' -v n="$n" '$1 != n' "$trail" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  else
    : > "$tmp" || return 1
  fi
  printf '%s\t%s\t%s\n' "$n" "$src" "$(date +%s)" >> "$tmp" && mv "$tmp" "$trail" || { rm -f "$tmp"; return 1; }
}

# The reviewer's harness refused the prompt for its size (#145): codex's input_too_large, claude's
# "Prompt is too long". A retry would send the same prompt; the task's note says what to do instead.
qa_input_too_large() { # qa_input_too_large <task dir>
  grep -qE 'input_too_large|Input exceeds the maximum length|[Pp]rompt is too long|context_length_exceeded' \
    "$1/qa.log" "$1/qa.out" 2>/dev/null
}

# What the implementer was given (#150): prompt.md and approach.md as they were when dwarf.input was
# built, kept next to them as prompt.dispatched / approach.dispatched. A missing approach.md is an empty
# copy. Each goes through a temp file and mv; prompt.dispatched is written last, because its presence is
# what says the pair is complete.
record_dispatched_inputs() { # record_dispatched_inputs <tdir>
  local tdir="$1" f tmp
  for f in approach prompt; do
    tmp="$tdir/.$f.dispatched.tmp"
    if [ -f "$tdir/$f.md" ]; then cp "$tdir/$f.md" "$tmp" || { rm -f "$tmp"; return 1; }
    else : > "$tmp" || return 1; fi
    mv "$tmp" "$tdir/$f.dispatched" || { rm -f "$tmp"; return 1; }
  done
}

# The brief forge last accounted for (#220): prompt.md and approach.md as `plan` left them, then as each
# change to them was logged (at a dispatch, or by a STALE check). prompt.seen is written last.
record_seen_inputs() { # record_seen_inputs <tdir>
  local tdir="$1" f tmp
  for f in approach prompt; do
    tmp="$tdir/.$f.seen.tmp"
    if [ -f "$tdir/$f.md" ]; then cp "$tdir/$f.md" "$tmp" || { rm -f "$tmp"; return 1; }
    else : > "$tmp" || return 1; fi
    mv "$tmp" "$tdir/$f.seen" || { rm -f "$tmp"; return 1; }
  done
}

# One control.log entry for a change to a task's brief: actor scheduler, action prompt-changed, the attempt,
# the files and when forge saw it (`during`: attempt, review or dispatch).
audit_brief_change() { # audit_brief_change <plan> <id> <attempt> <during> <file>...
  python3 - "$SKILL_DIR/scripts/forge-control.py" "$@" <<'PY' 2>/dev/null || note "$2: control.log not updated with the brief change"
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('forge_control', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules['forge_control'] = module
spec.loader.exec_module(module)
module.audit(Path(sys.argv[2]), 'scheduler', 'prompt-changed', sys.argv[3], 'prompt', int(sys.argv[4]), None,
             files=sys.argv[6:], during=sys.argv[5])
PY
}

# At dispatch (#220): a brief edited since forge last saw it (between attempts, while the task waited for
# a decision or its dependencies, or before its first dispatch) gets its control.log entry now, and its
# diff in brief.diff, so every change after `plan` is on record. The baseline is prompt.seen, else the
# previous attempt's prompt.dispatched; with neither (a plan from before #220) there is nothing to compare.
audit_brief_at_dispatch() { # audit_brief_at_dispatch <plan> <id> <tdir> <attempt>
  local PLAN="$1" id="$2" tdir="$3" attempt="$4" f cur old ext=seen changed=() tmp="$3/.brief.diff.tmp"
  [ -f "$tdir/prompt.seen" ] || ext=dispatched
  [ -f "$tdir/prompt.$ext" ] || { record_seen_inputs "$tdir" || true; return 0; }
  : > "$tmp" || return 0
  for f in prompt approach; do
    cur="$tdir/$f.md"; [ -f "$cur" ] || cur=/dev/null
    old="$tdir/$f.$ext"; [ -f "$old" ] || old=/dev/null
    cmp -s "$old" "$cur" && continue
    changed+=("$f.md")
    diff -u --label "$f.md (as forge last saw it)" --label "$f.md (attempt $attempt)" "$old" "$cur" >> "$tmp" || true
  done
  if [ "${#changed[@]}" -gt 0 ]; then
    mv "$tmp" "$tdir/brief.diff" || rm -f "$tmp"
    audit_brief_change "$PLAN" "$id" "$attempt" dispatch "${changed[@]}"
    note "$id: its brief changed since forge last saw it (${changed[*]}); attempt $attempt gets the new text (see $tdir/brief.diff)"
  else
    rm -f "$tmp"
  fi
  record_seen_inputs "$tdir" || true
}

# Brief inputs the last completed review judged (#209), written in reverse order so
# prompt.reviewed's presence marks the snapshot complete.
record_reviewed_inputs() { # record_reviewed_inputs <tdir> <thr>
  local tdir="$1" thr="${2:-}" f tmp
  tmp="$tdir/.approach.reviewed.tmp"
  if [ -f "$tdir/approach.md" ]; then cp "$tdir/approach.md" "$tmp" || { rm -f "$tmp"; return 1; }
  else : > "$tmp" || return 1; fi
  mv "$tmp" "$tdir/approach.reviewed" || { rm -f "$tmp"; return 1; }
  tmp="$tdir/.qa.reviewed.threshold.tmp"
  printf '%s\n' "$thr" > "$tmp" && mv "$tmp" "$tdir/qa.reviewed.threshold" || { rm -f "$tmp"; return 1; }
  tmp="$tdir/.prompt.reviewed.tmp"
  if [ -f "$tdir/prompt.md" ]; then cp "$tdir/prompt.md" "$tmp" || { rm -f "$tmp"; return 1; }
  else : > "$tmp" || return 1; fi
  mv "$tmp" "$tdir/prompt.reviewed" || { rm -f "$tmp"; return 1; }
}

brief_changed_since_review() { # brief_changed_since_review <tdir> <thr> -> 0 when changed
  local tdir="$1" thr="${2:-}" f cur
  [ -f "$tdir/prompt.reviewed" ] || return 1
  for f in prompt approach; do
    cur="$tdir/$f.md"; [ -f "$cur" ] || cur=/dev/null
    cmp -s "$tdir/$f.reviewed" "$cur" || return 0
  done
  if [ -f "$tdir/qa.reviewed.threshold" ]; then
    [ "$(cat "$tdir/qa.reviewed.threshold" 2>/dev/null)" = "$thr" ] || return 0
  fi
  return 1
}

# Whether the planner edited prompt.md or approach.md while the attempt ran (#150). Returns 0 when it did:
# the attempt is then STALE, written to stale.diff and control.log, and the caller stops before any review.
# Returns 1 when nothing changed, or when the attempt predates the .dispatched copies (no check possible).
# With `review` (#212) the check runs after the review instead: an edit made while the reviewer worked means
# its verdict judged the old text, so it does not count. The finished review is kept as qa.stale.last for the
# next attempt to read, and the attempt is STALE all the same.
prompt_drifted() { # prompt_drifted <plan> <id> <tdir> <attempt> [attempt|review]
  local PLAN="$1" id="$2" tdir="$3" attempt="$4" during="${5:-attempt}" f cur old changed=() tmp="$3/.stale.diff.tmp" what
  [ -f "$tdir/prompt.dispatched" ] || return 1
  : > "$tmp" || return 1
  for f in prompt approach; do
    cur="$tdir/$f.md"; [ -f "$cur" ] || cur=/dev/null
    old="$tdir/$f.dispatched"; [ -f "$old" ] || continue
    cmp -s "$old" "$cur" && continue
    changed+=("$f.md")
    diff -u --label "$f.md (attempt $attempt)" --label "$f.md (now)" "$old" "$cur" >> "$tmp" || true
  done
  if [ "${#changed[@]}" -eq 0 ]; then rm -f "$tmp" "$tdir/stale.diff"; return 1; fi
  mv "$tmp" "$tdir/stale.diff" || rm -f "$tmp"
  audit_brief_change "$PLAN" "$id" "$attempt" "$during" "${changed[@]}"
  # Logged once: the dispatch that follows compares with this, not with the attempt's copy (#220).
  record_seen_inputs "$tdir" || true
  what="${changed[0]}"; [ "${#changed[@]}" -eq 1 ] || what="${changed[0]} and ${changed[1]}"
  if [ "$during" = review ]; then
    rm -f "$tdir/qa.stale.last"
    [ ! -f "$tdir/qa.last" ] || mv -f "$tdir/qa.last" "$tdir/qa.stale.last"
    note "$id: $what changed during the review of attempt $attempt — its verdict does not count; the implementer gets the new text and the review's findings (no attempt spent)"
  else
    note "$id: $what changed during attempt $attempt — not reviewed; the implementer gets the new text (no attempt spent)"
  fi
  echo STALE > "$tdir/status"
  # The implementer has to run again, so a saved QA-only resume of this attempt no longer applies.
  rm -f "$tdir/resume"
  return 0
}

# A scripted task (#142) has the literal `script` in its dwarf column: its command is the executable
# tasks/<id>/run, and its qa column reviews the result.
task_is_script() { # task_is_script <plan> <id>
  [ "$(field "$1/tasks.tsv" "$2" dwarf)" = script ]
}

# A fenced block of a file (its last <chars> characters when given), the fence longer than any run of
# backticks inside it so the content cannot close it early.
script_fenced() { # script_fenced <file> [chars]
  python3 - "$1" "${2:-}" <<'PY'
import re, sys
text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
if sys.argv[2]:
    text = text[-int(sys.argv[2]):]
fence = '`' * max([3] + [len(m) + 1 for m in re.findall(r'`+', text)])
print(fence)
print(text, end='' if text.endswith('\n') or not text else '\n')
print(fence)
PY
}

# Runs a scripted task's command from the task's base and records the run. Returns 0 when the command
# exited 0 (the caller goes on to the snapshot and the review); otherwise the status and qa.last are
# written and it returns 1: no review, no noretry (a person may edit run, and the next attempt resets
# the worktree).
run_script_task() { # run_script_task <plan> <id> <tdir> <wt> <base>
  local PLAN="$1" id="$2" tdir="$3" wt="$4" base="$5" run="$3/run"
  local limit="${FORGE_TIMEOUT:-2700}" pid rc=0 timed_out=0 started deadline waited=0 hex rec exit_json note_txt
  # As for a dispatch: 0 is no limit, and a leading zero is decimal (bash would read 08 as octal).
  case "$limit" in ''|*[!0-9]*) limit=2700 ;; esac
  limit=$(( 10#$limit ))
  local cmd=("$run")
  if [ ! -f "$run" ] || [ ! -x "$run" ]; then
    echo FAIL > "$tdir/status"
    echo "tasks/$id/run is missing or not executable." > "$tdir/qa.last"
    note "$id: tasks/$id/run is missing or not executable"; return 1
  fi
  head -c 2 "$run" | grep -q '^#!' || cmd=(/bin/bash "$run")
  # Every attempt starts from the base, so the cumulative diff is what the command produced. The branch
  # history stays; .forge is forge's own.
  if ! { git -C "$wt" read-tree -u --reset "$base" && git -C "$wt" clean -fdq -e .forge; } >"$tdir/script.reset.out" 2>&1; then
    echo ERROR > "$tdir/status"
    note "$id: could not reset the worktree to its base — see $tdir/script.reset.out"; return 1
  fi
  : > "$tdir/script.log"
  started="$(python3 -c 'import time; print(time.time())')"
  # exec keeps the pid: forge-guard.py session makes it a process group leader, so pid is the group id.
  ( cd "$wt" && export FORGE_TASK_ID="$id" FORGE_PLAN="$PLAN" FORGE_WORKTREE="$wt" FORGE_BASE_REF="$base" \
      && exec python3 "$SKILL_DIR/scripts/forge-guard.py" session -- "${cmd[@]}" ) </dev/null >"$tdir/script.log" 2>&1 &
  pid=$!
  deadline=$(( SECONDS + limit ))
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$limit" -gt 0 ] && [ "$SECONDS" -ge "$deadline" ]; then timed_out=1; break; fi
    sleep 0.2
  done
  if [ "$timed_out" = 1 ]; then
    # TERM the group, KILL after 10 s whatever is left of it (timeout(1) is not on macOS).
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    while kill -0 -- "-$pid" 2>/dev/null && [ "$waited" -lt 50 ]; do sleep 0.2; waited=$(( waited + 1 )); done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  else
    wait "$pid"; rc=$?
    # What the command left running in its group is stopped with it.
    python3 "$SKILL_DIR/scripts/forge-guard.py" reap "$pid" "$tdir/script.orphans" >/dev/null 2>&1 || true
  fi
  exit_json="$rc"; [ "$timed_out" = 0 ] || exit_json=""
  hex="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:16])')"
  rec="$tdir/attempts/script-$hex"
  rm -f "$tdir/script.ran"
  if mkdir -p "$rec" && cp "$tdir/script.log" "$rec/script.log" && cp "$run" "$rec/run"; then
    # The review shows this copy: run may be edited before a QA-only resume reviews this attempt.
    printf '%s\n' "$rec" > "$tdir/script.ran"
    python3 - "$rec/metrics.json" "$started" "$exit_json" <<'PY' || note "$id: the script run was not recorded in attempts/"
import json, os, sys, time
path, started, code = sys.argv[1:4]
data = {'role': 'script', 'harness': 'script', 'exit_code': int(code) if code else None,
        'wall_started': float(started), 'elapsed_s': {'total': time.time() - float(started)}}
with open(path + '.tmp', 'w') as handle:
    json.dump(data, handle, indent=2)
os.replace(path + '.tmp', path)
PY
  else
    note "$id: the script run was not recorded in attempts/"
  fi
  [ "$timed_out" = 1 ] || [ "$rc" != 0 ] || return 0
  if [ "$timed_out" = 1 ]; then note_txt="The script timed out after ${limit}s."; else note_txt="The script exited $rc."; fi
  { echo "$note_txt"; echo; echo "Its output (last 3000 characters of script.log):"; script_fenced "$tdir/script.log" 3000; } > "$tdir/qa.last"
  echo FAIL > "$tdir/status"
  note "$id: ${note_txt%.} — not reviewed (see $tdir/script.log)"
  return 1
}

do_task_body() (
  local PLAN="${1:?}" id="${2:?}"
  local T="$PLAN/tasks.tsv" REPO run_id base wt br tdir attempt resume=""
  local reviewed review source_fp review_fp
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  tdir="$PLAN/tasks/$id"; mkdir -p "$tdir"
  forge_metric_begin "$tdir" task
  dependencies_ready "$PLAN" "$id" || return 1
  echo RUNNING > "$tdir/status"
  rm -f "$tdir/noretry"
  [ -f "$PLAN/no_memory" ] && export FORGE_MEMORY=off
  # --timeout given to run/retry is kept in the plan and wins over the environment.
  [ -s "$PLAN/timeout" ] && export FORGE_TIMEOUT="$(cat "$PLAN/timeout")"
  wt="$(cat "$PLAN/wt_root")/$id"
  br="forge/$run_id/$id"
  # A member of a review=group group (#140) stops at IMPLEMENTED: its group is reviewed once, later.
  # Looked up once. A task in no group, or in a review=task group, takes the path below unchanged,
  # and a plan with no groups.tsv starts no process for it.
  local grp_id="" grp_review="" grp_qa=""
  if [ -f "$PLAN/groups.tsv" ]; then
    IFS=$'\t' read -r grp_id grp_review grp_qa < <(forge_groups_py of "$PLAN" "$id" 2>/dev/null) || true
    [ "$grp_review" = group ] || grp_id=""
  fi
  # No task review happened for a member, so a QA-only resume cannot apply to it.
  [ -z "$grp_id" ] || rm -f "$tdir/resume"
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

  local dw qa yd yq rc ft_dwarf ft_qa qa_thr
  dw="$(field "$T" "$id" dwarf)"; qa="$(field "$T" "$id" qa)"
  yd=""; yq=""
  [ -f "$PLAN/yolo_dwarf" ] && yd="--yolo"
  [ -f "$PLAN/yolo_qa" ] && yq="--yolo"
  # Empty when the role is not listed, so the recorded command line is unchanged.
  ft_dwarf="$(forge_fast_tier "$PLAN" dwarf)"
  ft_qa="$(forge_fast_tier "$PLAN" qa)"
  # Read once per attempt: governs brief-change re-review (#209), the QA prompt, and qa_gate.
  qa_thr="$(qa_threshold_for "$PLAN" "$id")"

  if ! ensure_worktree "$REPO" "$wt" "$br" "$base" "$tdir" "$PLAN" "$id"; then
    echo ERROR > "$tdir/status"
    note "$id: could not create worktree — $(tail -1 "$tdir/worktree.out")"
    return 1
  fi

  local is_script=0 is_split=0
  task_is_script "$PLAN" "$id" && is_script=1
  # A split task (#153) whose subtasks are not on its branch yet runs them here, where a model task
  # runs its implementer. Once they are (sub.merged), a retry is an ordinary one: its implementer
  # fixes the review's findings in the combined work, and no subtask runs twice.
  if forge_split_open "$PLAN" "$id"; then
    is_split=1
    if [ -n "$resume" ]; then resume=""; rm -f "$tdir/resume"; attempt=$(( attempt + 1 )); echo "$attempt" > "$tdir/attempt"; fi
  fi
  if [ -z "$resume" ]; then
    # Persist each source separately; QA receives complete context once.
    write_capsule "$PLAN" "$id" dwarf >/dev/null
    cp "$tdir/capsule.md" "$tdir/baseline.capsule"
    # A brief edited since forge last saw it is logged now, whenever the edit landed (#220).
    audit_brief_at_dispatch "$PLAN" "$id" "$tdir" "$attempt"
    # What this attempt's implementer is given, to tell later whether the planner edited it (#150).
    record_dispatched_inputs "$tdir" || { rm -f "$tdir/prompt.dispatched"; note "$id: could not record the dispatched prompt; a later edit will not be detected"; }
    if [ "$is_script" = 1 ]; then
      # A scripted task (#142) runs its command here instead of the implementer below (memory,
      # dispatch, gates). The snapshot and everything after it are the same.
      forge_metric_phase dispatch
      run_script_task "$PLAN" "$id" "$tdir" "$wt" "$base" || return 1
    fi
    if [ "$is_split" = 1 ]; then
      # The subtasks run as a plan of their own and merge into this task's branch. The snapshot and
      # everything after it are the same: one commit, then the review of the cumulative diff.
      forge_metric_phase dispatch
      run_split_task "$PLAN" "$id" "$tdir" "$wt" "$base" "$attempt" || return 1
    fi
    # The implementer's part, from here to the snapshot, belongs to model tasks only (no reindent, to
    # keep the diff of this section readable).
    if [ "$is_script" != 1 ] && [ "$is_split" != 1 ]; then
    # --task lets Jev narrow the injected slice to facts that bear on THIS task. Without
    # it, or with Jev off, the full role slice is injected exactly as before.
    /bin/bash "$MEMORY" inject "$REPO" dwarf --task "$tdir/prompt.md" \
        --run-dir "$tdir" > "$tdir/dwarf.memory"
    # The messages queued for this task's next attempt (forge control steer, #134) are this attempt's.
    steer_take "$PLAN" "$id" "$attempt"
    {
      python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" --retry-source "$(cat "$tdir/retry_findings.source" 2>/dev/null || echo review)" --memory "$tdir/dwarf.memory"
      steer_section "$tdir" "$attempt" dwarf
      [ ! -f "$PLAN/sweep_ahead" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" ahead "$PLAN" "$id" --role dwarf 2>/dev/null
      echo "Implement this task in the repository. Follow the intended approach unless it is wrong; explain deviations. Run the task's requested verification and report results."
      echo "On retry, fix the findings in the existing work; preserve parts not criticised. Explain any finding you reject."
      # With repository verification configured, the whole suite runs once at the end of the run, never per attempt.
      echo; python3 "$SKILL_DIR/scripts/forge-contract.py" dwarf $([ ! -f "$PLAN/pre_review_tests" ] || echo --pre-review) \
        $([ -z "$(verify_source_for "$PLAN" "$REPO" "$(cat "$PLAN/wt_root" 2>/dev/null)/_integration")" ] || echo --verify-after-merge)
      echo; /bin/bash "$MEMORY" note dwarf
      # Last, after the memory note. Nothing is printed when retro is off.
      forge_retro_prompt "$PLAN" dwarf
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
      if [ "$attempt" = 1 ]; then
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
        # The fresh dispatch deletes dwarf.last and dwarf.log as it starts. Record the
        # continuation first; a failure to record does not change the attempt.
        forge_retro_record "$PLAN" dwarf "$tdir" "$id" "$attempt" "$wt/.forge/retro-inbox.jsonl"
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
      # The dispatch ended without a result memory would record. Keep its notes anyway;
      # a failure to record them does not change the status this case is about to write.
      forge_retro_record "$PLAN" dwarf "$tdir" "$id" "$attempt" "$wt/.forge/retro-inbox.jsonl"
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
    forge_retro_record "$PLAN" dwarf "$tdir" "$id" "$attempt" "$wt/.forge/retro-inbox.jsonl"

    guard_after_dwarf "$wt" "$tdir" "$id"
    # Before the snapshot, so a dwarf that only asked a question (and changed nothing) still reaches it.
    forge_post_dwarf_gates "$PLAN" "$id" "$tdir" "$wt" "$attempt" || return 0
    fi
    forge_metric_phase snapshot
    # Capture the real diff, including new files, then commit on the task branch.
    # Diffed against the task's pinned base rather than the branch head, so a retry
    # hands QA the task's CUMULATIVE work. Reviewing only the fix would let the
    # first attempt's code through unread.
    # .forge/ is forge's own memory, rewritten in the user's tree after every run.
    # Left in, it would reach QA as if a dwarf had written it, and any task whose
    # files touched it would collide with every other task in the wave planner.
    [ -s "$tdir/changes.diff" ] && mv "$tdir/changes.diff" "$tdir/changes.prev.diff"
    (cd "$wt" && git add -A -- . ":(exclude).forge" \
        && git diff --cached --binary "$base" -- . ":(exclude).forge") > "$tdir/changes.diff" 2>/dev/null || { echo ERROR > "$tdir/status"; return 1; }
    if [ ! -s "$tdir/changes.diff" ]; then
      # A harness that exited 0 with an empty final message and changed nothing did not
      # decline the task; it never really ran (login, quota, a dropped connection). Nor did one
      # its CLI cancelled first (grok, at a tool call a headless run cannot approve): a pause,
      # not a failed attempt marked never to retry.
      if grep -qxE 'class=(empty|cancelled)' "$tdir/dwarf.infra" 2>/dev/null; then
        infra_stop "$tdir" "$id" dwarf "$attempt"; return 8
      fi
      echo FAIL > "$tdir/status"
      : > "$tdir/noretry"
      if [ "$is_script" = 1 ]; then
        echo "The script produced no changes." > "$tdir/qa.last"
        note "$id: the script produced an empty diff"; return 1
      fi
      if [ "$is_split" = 1 ]; then
        echo "The subtasks produced no changes at all." > "$tdir/qa.last"
        note "$id: its subtasks produced an empty diff"; return 1
      fi
      echo "The dwarf produced no changes at all." > "$tdir/qa.last"
      note "$id: dwarf produced an empty diff"; return 1
    fi
    # A bounced attempt (retry_findings.source `bounce`) was never reviewed, so a retry that keeps
    # its code is reviewed rather than refused as identical: the refusal is for a diff a review
    # already judged. The same applies when the brief or threshold changed since that review (#209).
    if [ -s "$tdir/changes.prev.diff" ] && cmp -s "$tdir/changes.diff" "$tdir/changes.prev.diff" \
        && [ "$is_split" != 1 ] && [ "$(cat "$tdir/retry_findings.source" 2>/dev/null)" != bounce ]; then
      if brief_changed_since_review "$tdir" "$qa_thr"; then
        note "$id: brief changed since the last review — reviewing the unchanged diff against the updated brief"
      else
        # A retry its CLI cancelled before it changed anything did not keep the code on purpose.
        if grep -qx 'class=cancelled' "$tdir/dwarf.infra" 2>/dev/null; then
          infra_stop "$tdir" "$id" dwarf "$attempt"; return 8
        fi
        echo FAIL > "$tdir/status"
        : > "$tdir/noretry"
        echo "The retry changed nothing: the diff is byte-identical to the previous attempt." \
          > "$tdir/qa.last"
        note "$id: retry produced no new changes — not re-reviewing identical code"; return 1
      fi
    fi

    if ! git -C "$wt" diff --cached --quiet "$base" -- .forge; then
      echo FAIL > "$tdir/status"; note "$id: changed excluded Forge memory; cannot accept an unreviewed change"; return 1
    fi
    check_drift "$(field "$T" "$id" files)" "$tdir" "$wt" "$base"
    if [ -s "$tdir/drift.txt" ]; then
      note "$id: touched undeclared file(s): $(tr '\n' ' ' < "$tdir/drift.txt")"
    fi

    (cd "$wt" && git -c user.name=forge -c user.email=forge@local \
        commit --allow-empty -q -m "forge($id) attempt $attempt: $(field "$T" "$id" title)$([ "$is_script" != 1 ] || echo ' (script)')$([ "$is_split" != 1 ] || echo ' (subtasks)')") >/dev/null 2>&1 || {
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
  # A prompt or approach edited since the implementer was given it: judged against text it never saw.
  # STALE, not reviewed, nothing spent; the committed work stays on the branch (#150).
  if prompt_drifted "$PLAN" "$id" "$tdir" "$attempt"; then return 0; fi
  if [ -n "$grp_id" ]; then
    # The member's own pipeline ends here: no review snapshot, QA, shadow or fail-first. The tests of
    # --pre-review-tests still run, on a snapshot that lives only for them: a red attempt is FAIL and
    # goes back unreviewed, as for any task.
    if [ -f "$PLAN/pre_review_tests" ]; then
      review="$tdir/review-$attempt-$$"
      local stale
      for stale in "$tdir"/review-*; do forge_drop_snapshot "$stale"; done
      FORGE_REVIEW_SNAPSHOT="$review"
      forge_review_snapshot "$wt" "$base" "$reviewed" "$review" || { echo ERROR > "$tdir/status"; return 1; }
      if ! forge_pre_review "$review" "$tdir" "$REPO" "$PLAN"; then
        : > "$tdir/prereview.failed"
        rm -f "$tdir/qa.last"
        echo FAIL > "$tdir/status"
        note "$id: the tests this attempt affects fail — sent back to the implementer without a review (see $tdir/prereview.md)"
        return 0
      fi
      forge_drop_snapshot "$review"; FORGE_REVIEW_SNAPSHOT=""
    else
      rm -f "$tdir/prereview.json" "$tdir/prereview.txt" "$tdir/prereview.md"
    fi
    # The fingerprint a review would have recorded, then the status: a reader that sees IMPLEMENTED
    # can trust reviewed.commit and source.fingerprint.
    forge_fingerprint "$wt" > "$tdir/source.fingerprint.tmp" && mv "$tdir/source.fingerprint.tmp" "$tdir/source.fingerprint" || return 1
    echo IMPLEMENTED > "$tdir/status"
    note "$id: implemented — waits for group $grp_id's review"
    return 0
  fi
  review="$tdir/review-$attempt-$$"
  # One review of a task at a time, so any snapshot already here is an interrupted review's: a signal
  # ends this subshell without its EXIT trap (#139).
  local stale
  for stale in "$tdir"/review-*; do forge_drop_snapshot "$stale"; done
  FORGE_REVIEW_SNAPSHOT="$review"          # forge_metric_begin's EXIT trap removes it, whatever the outcome
  forge_review_snapshot "$wt" "$base" "$reviewed" "$review" || { echo ERROR > "$tdir/status"; return 1; }
  forge_review_deps "$wt" "$review" "$tdir/review.deps"
  source_fp="$(forge_fingerprint "$wt")" || return 1
  # The review copy is a commit: an untracked file the reviewer leaves there (a test run's
  # bytecode) changes nothing that merges, so only tracked content counts (#95).
  review_fp="$(forge_fingerprint "$review" --tracked)" || return 1
  echo "$source_fp" > "$tdir/source.fingerprint"
  echo "$review_fp" > "$tdir/review.fingerprint"

  forge_metric_phase preparation
  local rs_on=0
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
  # What changed since the review whose findings this reviewer is handed. dwarf.last is the reply of
  # the attempt under review: a dwarf run just rewrote it, or $resume (the validated QA-only
  # path, never the raw marker) guarantees the reviewed commit is the one it describes.
  local prompt_extra=(--role qa)
  attempt_delta "$tdir" "$wt" "$reviewed"
  if [ -s "$tdir/changes.since.diff" ]; then
    prompt_extra+=(--retry-diff "$tdir/changes.since.diff" --retry-stat "$tdir/changes.since.stat"
      --retry-range "$(awk '{ print substr($1, 1, 7) ".." substr($2, 1, 7) }' "$tdir/changes.since.range")")
  fi
  if [ -s "$tdir/retry_findings.md" ] && [ -s "$tdir/dwarf.last" ]; then
    prompt_extra+=(--reply "$tdir/dwarf.last")
  fi
  # A finding sweep's fix task gets the narrow review: one FIXED / NOT FIXED line per finding.
  local qa_kind=task
  [ ! -f "$tdir/sweep.json" ] || qa_kind=sweep
  [ "$is_script" != 1 ] || qa_kind=script
  # QA uses the dispatch-time ownership and baseline, never later merge state.
  /bin/bash "$MEMORY" inject "$REPO" qa --task "$tdir/prompt.md" \
      --run-dir "$tdir" > "$tdir/qa.memory"
  # The diff goes inline up to the whole-run review's limit (#145). Over it, the prompt holds the
  # stat and the reviewer reads the diff in its snapshot, which holds this attempt as one commit over
  # its base, as the whole-run review and adopt already do. At most the limit plus one byte is read.
  # --irreversible-delete: a deleted file shows as a header, not every line it had (one deleted
  # data directory once made a 7.1M-character prompt that the reviewer's CLI refused).
  local qa_inline=1
  git -C "$wt" diff --binary --irreversible-delete "$base" "$reviewed" -- . ':(exclude).forge' 2>/dev/null |
    head -c $((REVIEW_DIFF_LIMIT + 1)) > "$tdir/.qa.diff"
  [ "$(wc -c < "$tdir/.qa.diff" | tr -d ' ')" -le "$REVIEW_DIFF_LIMIT" ] || qa_inline=0
  {
    python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" --retry-source "$(cat "$tdir/retry_findings.source" 2>/dev/null || echo review)" ${prompt_extra[@]+"${prompt_extra[@]}"} --memory "$tdir/dwarf.memory" --memory "$tdir/qa.memory"
    steer_section "$tdir" "$attempt" qa
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part head --kind "$qa_kind"
    echo
    if [ "$is_script" = 1 ]; then
      local ran_dir="$tdir"
      [ ! -s "$tdir/script.ran" ] || [ ! -f "$(cat "$tdir/script.ran")/run" ] || ran_dir="$(cat "$tdir/script.ran")"
      echo "The command (tasks/$id/run, as it ran):"; script_fenced "$ran_dir/run"
      echo; echo "Its output (last 3000 characters of script.log):"; script_fenced "$ran_dir/script.log" 3000
      echo
    fi
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
    local blocked_claims=""
    [ ! -f "$tdir/dwarf.last" ] || blocked_claims="$(python3 "$SKILL_DIR/scripts/forge-contract.py" blocked --message "$tdir/dwarf.last" 2>/dev/null \
      | python3 -c 'import json, sys
for r in json.load(sys.stdin):
    print("  - %s: %s" % (r["path"], r["why"]))' 2>/dev/null)" || blocked_claims=""
    if [ -n "$blocked_claims" ]; then
      # A claim, not a fact: only the reviewer's {outside=...} on a finding can stop the task (#149).
      echo "The implementer says these fixes need files outside the task:"
      printf '%s\n' "$blocked_claims"
      echo "Confirm a real one with {outside=<path>} on the finding it blocks, or refute it."
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
    local qa_test_mins="" qa_test_mins_flag=()
    for _tmf in "$tdir/qa_test_minutes" "$PLAN/qa_test_minutes"; do
      if [ -f "$_tmf" ]; then
        qa_test_mins="$(head -1 "$_tmf" 2>/dev/null | tr -d ' \r')"
        case "$qa_test_mins" in ''|*[!0-9]*) qa_test_mins="" ;; *) qa_test_mins_flag=(--test-minutes "$qa_test_mins") ; break ;; esac
      fi
    done
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part tail --kind "$qa_kind" --threshold "$qa_thr" ${qa_test_mins_flag[@]+"${qa_test_mins_flag[@]}"}
    echo
    if [ "$qa_inline" = 1 ]; then
      echo '```diff'; cat "$tdir/.qa.diff"; echo '```'
    else
      echo "The diff is larger than $((REVIEW_DIFF_LIMIT / 1024)) KB, so it is not inlined. Read it in your"
      echo "working directory, which holds this attempt as the last commit over its base:"
      echo "  git diff HEAD~1 HEAD -- . ':(exclude).forge'"
      echo "Its stat ($(git -C "$wt" diff --shortstat "$base" "$reviewed" -- . ':(exclude).forge' 2>/dev/null | sed 's/^ *//')):"
      echo '```text'
      git -C "$wt" diff --stat=200 "$base" "$reviewed" -- . ':(exclude).forge' 2>/dev/null | head -c 65536
      echo '```'
    fi
    echo; /bin/bash "$MEMORY" note qa
    echo "Place any learning notes before the final FORGE_VERDICT: PASS or FORGE_VERDICT: FAIL line."
    # Last, so a shadow review, which copies this file byte for byte, carries the same
    # instruction. Its notes are not recorded: only this task's own dispatch is.
    forge_retro_prompt "$PLAN" qa
  } > "$tdir/qa.input"
  rm -f "$tdir/.qa.diff"
  # Opt-in (plan --shadow-qa): a second, independent review of this same diff, to measure
  # how often two reviewers agree. It reviews its own snapshot from its own dir under
  # $PLAN/shadow/, and nothing in this script ever reads its result.
  local shadow_pid=""
  if [ -s "$PLAN/shadow_qa" ]; then
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
    if [ -f "$PLAN/reviewer_session_rate" ] && ! grep -qxF "$id" "$PLAN/reviewer_session_tasks" 2>/dev/null; then
      rs_reason=not-sampled   # the comparison half of --reviewer-session-rate: every round is fresh
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
    # The fresh dispatch deletes qa.last and qa.log as it starts. Record the continuation
    # first; a failure to record does not change the round.
    forge_retro_record "$PLAN" qa "$tdir" "$id" "$attempt"
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
    forge_retro_record "$PLAN" qa "$tdir" "$id" "$attempt"
    case "$rc" in
      7) echo TIMEOUT > "$tdir/status"; note "$id: qa hit the dispatch timeout — see $tdir/qa.out" ;;
      8) infra_stop "$tdir" "$id" qa "$attempt"; return 8 ;;
      *) echo ERROR > "$tdir/status"
         if qa_input_too_large "$tdir"; then
           # The same prompt fails the same way: say so, and what does work (#145).
           note "$id: the reviewer's harness refused the prompt as too large ($(wc -c < "$tdir/qa.input" | tr -d ' ') bytes; see $tdir/qa.log). A retry sends the same prompt. Review the commit from a snapshot instead: forge-parallel.sh adopt $PLAN $(cut -c1-12 "$tdir/reviewed.commit" 2>/dev/null) --qa <spec> --reason \"...\", or split the task"
         else
           note "$id: qa dispatch failed — see $tdir/qa.out"
         fi ;;
    esac
    return 1
  fi
  rm -f "$tdir/resume"

  forge_metric_phase verification
  # Before the invalidation return: a review that changed a tracked file still finished,
  # and its notes are the point of that dispatch. Recording never changes the status.
  forge_retro_record "$PLAN" qa "$tdir" "$id" "$attempt"
  if [ "$(forge_fingerprint "$wt")" != "$source_fp" ] || [ "$(forge_fingerprint "$review" --tracked)" != "$review_fp" ]; then
    # The committed work is untouched, so --retry-failed may review it again (reason `review`);
    # the merge-time invalidations below leave no reason and wait for a human.
    echo review > "$tdir/invalidated.reason"
    echo INVALIDATED > "$tdir/status"; note "$id: artifact changed during review"; return 1
  fi
  # The brief edited while the reviewer worked (#212): the verdict judged text that is gone, so it does not
  # count. STALE as before review, nothing spent; the next attempt gets the new text and these findings.
  if prompt_drifted "$PLAN" "$id" "$tdir" "$attempt" review; then return 0; fi
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
  record_reviewed_inputs "$tdir" "$qa_thr" || true
  return 0
)

# Entry point for both the scheduler's `_task` and `retry`: runs the pipeline, then logs
# the attempt (including infrastructure stops, which spend nothing) for later audit.
do_task() {
  local rc
  do_task_body "$@"; rc=$?
  record_attempt "$1" "$2"
  # A split task waiting for its subtasks (#153) was not attempted: nothing to judge or escalate.
  [ -s "$1/tasks/$2/sub.waiting" ] || post_attempt "$1" "$2"
  # run --auto-split (#153): a task that just stopped on `split` is split by the planner, and its
  # subtasks run now, in this same entry, so the scheduler sees one task that took longer.
  if forge_auto_split "$1" "$2"; then
    do_task_body "$@"; rc=$?
    record_attempt "$1" "$2"
    [ -s "$1/tasks/$2/sub.waiting" ] || post_attempt "$1" "$2"
  fi
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
    local integration
    integration="$(git -C "$int_wt" rev-parse HEAD)"
    printf '%s\n' "$integration" > "$PLAN/accepted.integration"
    mark_verification_stale "$PLAN" "$integration"
    touch "$PLAN/tasks/$id/merged"
    rm -f "$tdir/conflict.files"
    # Which of a sweep task's findings its reviewer marked fixed: what `forge issues` leaves out.
    [ ! -f "$tdir/sweep.json" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" settle "$PLAN" "$id" >/dev/null 2>&1 || true
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=MERGED --set commit="$reviewed" \
      --set integration="$integration"
    return 0
  fi
  # Which files collided is the label for whether two tasks were coupled, and the abort
  # below erases it, so it is kept first.
  (cd "$int_wt" && git diff --name-only --diff-filter=U) > "$tdir/conflict.files" 2>/dev/null
  (cd "$int_wt" && git merge --abort >/dev/null 2>&1)
  echo CONFLICT > "$PLAN/tasks/$id/status"
  forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=CONFLICT --set commit="$reviewed" \
    --set conflicts="$(tr '\n' ' ' 2>/dev/null < "$tdir/conflict.files")"
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
      --output|--verify|--setup|--subtasks|--planner) [ $# -ge 2 ] || break; shift 2 ;;
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
  local plan="$1" id tests_note untested=0 id_w; shift
  id_w="$(id_col_width "$plan/tasks.tsv")"
  for id in "$@"; do
    tests_note="$(forge_tests_note "$plan/tasks/$id/qa.last")"
    if [ -n "$tests_note" ]; then
      [ "$untested" = 0 ] && echo "reviews whose tests did not complete:"
      untested=1
      printf '  %-*s %s\n' "$id_w" "$id" "$tests_note"
    fi
  done
  [ "$untested" = 0 ] || echo
}

do_retry() {
  local PLAN="${1:?retry needs a plan dir}" id="${2:?retry needs a task id}"; shift 2
  FORGE_RUN_STARTED="$(forge_now)"
  forge_refuse_fractal_run "$PLAN" retry
  local NEWDWARF="" jev_rc=0 ANSWER="" ANSWER_FILE="" HAVE_ANSWER=0 DECIDE="" REASON="" APPROVED=0 NO_DISPATCH=0
  local SUBTASKS="" SPLIT_PLANNER=""
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
    # Before any option writes: an implemented group member has nothing to retry.
    [ "$(task_status "$PLAN" "$id")" != IMPLEMENTED ] || plans_implemented_refuse "$PLAN" "$id"
    retry_decision_gate "$PLAN" "$id" "$(task_status "$PLAN" "$id")" "$pre_decide" "$pre_dwarf" "$pre_reason" "$pre_approved"
  fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --output) OUTPUT="${2:?}"; shift 2 ;;
      --dwarf) NEWDWARF="${2:?--dwarf needs a spec}"; shift 2 ;;
      --decide) DECIDE="${2:?--decide needs retry=N, threshold=Pn or split}"; shift 2 ;;
      --subtasks) SUBTASKS="${2:?--subtasks needs a file}"; shift 2 ;;
      --planner) SPLIT_PLANNER="${2:?--planner needs a spec}"; shift 2 ;;
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
        forge_removed_flag "$1"
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
  [ "$st" != IMPLEMENTED ] || plans_implemented_refuse "$PLAN" "$id"
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
  # A split (#153): --subtasks and --planner belong to it alone, and it does not name a model for the task.
  local is_split_decision=0 cap_decide="$DECIDE"
  if forge_decide_is_split "$DECIDE"; then
    is_split_decision=1
    cap_decide="retry=1"   # the split grants the one attempt that reviews the whole task
    [ -z "$NEWDWARF" ] || die "retry: --decide split does not take --dwarf: the subtasks are routed with the plan's pools" 2
    [ -z "$SUBTASKS" ] || [ -z "$SPLIT_PLANNER" ] || die "retry: give --subtasks FILE or --planner SPEC, not both" 2
    [ -n "$SUBTASKS" ] || [ -n "$SPLIT_PLANNER" ] || SPLIT_PLANNER="$(forge_split_planner "$PLAN")"
  else
    [ -z "$SUBTASKS$SPLIT_PLANNER" ] || die "retry: --subtasks and --planner go with --decide split" 2
  fi
  # A task at its attempt cap (#69) is refused with the decision commands unless --decide retry=N reaches past it.
  if ! attempt_cap_gate "$PLAN" "$id" "$st" "$cap_decide"; then write_results "$PLAN"; return 2; fi
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
  if [ "$is_split_decision" = 1 ]; then
    # The sub-plan is made and the decision recorded; the task's next entry runs its subtasks.
    retry_split "$PLAN" "$id" "$DECIDE" "$REASON" "$SUBTASKS" "$SPLIT_PLANNER" user || prc=$?
  elif [ "$st" = NEEDS_DECISION ]; then
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
    [ "$prc" = 6 ] && return 3   # --decide files+= refused (#199): nothing was written
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

  if forge_split_open "$PLAN" "$id"; then note "running the subtasks of $id"
  else note "retrying $id with dwarf $(field "$T" "$id" dwarf)"; fi
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
      forge_brief_stale "$PLAN" "$id"
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
  if forge_is_sub_plan "$PLAN"; then
    # A sub-plan (#153) has no verification of its own; its task picks the work up at the next run.
    note "$PLAN is the sub-plan of a split task: forge-parallel.sh run $(forge_decompose_py root "$PLAN" 2>/dev/null) brings its merged subtasks into the task and reviews it"
  elif [ -n "$left" ]; then
    note "verification deferred: tasks still to run ($left); the next run, or integrate, verifies the integration branch"
  else
    verify_result "$PLAN" "$(cat "$PLAN/wt_root")/_integration" "$PLAN" || { write_results "$PLAN"; run_report_extras "$PLAN"; return 5; }
  fi
  write_results "$PLAN"
  echo
  # 14 covers NEEDS_DECISION; 9 pushes the branch column out for IMPLEMENTED (11) too.
  render_results_table "$PLAN"
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
  if forge_is_windows && [ -r "/proc/$group/winpid" ]; then
    forge_kill_group "$group" TERM
    kill -TERM "$group" 2>/dev/null || true
  else
    kill -TERM -- "-$group" 2>/dev/null || { termed=0; kill -TERM "$group" 2>/dev/null; }
  fi
  # The timer polls in short naps rather than sleeping the whole grace in one: it ends by
  # itself within a nap of the group's last member, and a long sleep left behind would hold
  # the plan lock it inherited.
  ( trap - TERM INT
    ticks=$((FORGE_VERIFY_GRACE * 10))
    while kill -0 -- "-$group" 2>/dev/null || { [ "$termed" = 0 ] && kill -0 "$group" 2>/dev/null; } || { forge_is_windows && [ -r "/proc/$group/winpid" ] && kill -0 "$group" 2>/dev/null; }; do
      if [ "$termed" = 0 ] && kill -TERM -- "-$group" 2>/dev/null; then termed=1; fi
      if [ "$ticks" -le 0 ]; then
        if forge_is_windows && [ -r "/proc/$group/winpid" ]; then
          forge_kill_group "$group" KILL
          kill -KILL "$group" 2>/dev/null
        elif [ "$termed" = 1 ]; then
          kill -KILL -- "-$group" 2>/dev/null
        else
          kill -KILL "$group" 2>/dev/null
        fi
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
  local wt="$1" cmd="$2" log="$3" rc=0 group deadline guard_py="$SKILL_DIR/scripts/forge-guard.py"
  if [ "${FORGE_RUN_STOPPABLE:-0}" != 1 ]; then
    (cd "$wt" && /bin/bash -c "$cmd") > "$log" 2>&1
    return $?
  fi
  FORGE_VERIFY_GRACE="${4:-5}"
  [ "$FORGE_VERIFY_STOP" = 1 ] && return 130
  ! forge_is_windows || guard_py="$(forge_host_path "$guard_py")"
  # A background job ignores SIGINT and SIGQUIT, and the command would inherit that; the
  # wrapper restores both before the guard puts the command in a session of its own.
  (cd "$wt" && { ! forge_is_windows || export MSYS_NO_PATHCONV=1; } && exec python3 -c 'import os, signal, subprocess, sys
for sig_name in ("SIGINT", "SIGQUIT"):
    sig = getattr(signal, sig_name, None)
    if sig is not None:
        signal.signal(sig, signal.SIG_DFL)
if os.name == "nt":
    sys.exit(subprocess.run([sys.executable] + sys.argv[1:]).returncode)
os.execv(sys.executable, [sys.executable] + sys.argv[1:])' \
    "$guard_py" session -- /bin/bash -c "$cmd") > "$log" 2>&1 <&0 &
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
  rm -f "$out/verification.status" "$out/verification.exit" \
    "$out/verification.flaky" "$out/verification.retry.log" "$out/verification.failing.log" "$out/verification.coverage.txt" \
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
    vsource="$(verify_source_for "$plan" "$repo" "$wt")"
    # The checkout's own lanes or command run (#151): say where the root's copy differs.
    case "${vsource%%$'\t'*}" in
      *:checkout) local diffline
        while IFS= read -r diffline; do note "$diffline"; done < <(verify_root_difference "$vsource" "$repo") ;;
    esac
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
# --- brief mode in a run (#135) ------------------------------------------------------
# The effective level: advisory, gated, strict or off. Settings are the switch (the plan file and
# FORGE_BRIEF=off); anything that cannot be read is off, so a broken brief never holds a run.
forge_brief_level() { # forge_brief_level <plan>
  local level
  level="$(PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - "$1" 2>/dev/null <<'PY'
import sys
import forge_side
print(forge_side.settings(sys.argv[1])['brief']['level'])
PY
)" || level=off
  case "$level" in advisory|gated|strict) echo "$level" ;; *) echo off ;; esac
}

# `forge-brief.py write` in a session of its own, output to brief.out, without waiting. brief.pid holds
# the writer's supervisor, which removes it when the writer ends: the scheduler's gated wait reads it.
forge_brief_start() { # forge_brief_start <plan>
  local plan="$1"
  rm -f "$plan/brief.pid"
  # The writer's own output goes to brief.out: say the missing-groups note here, on the console, first.
  PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
    'import sys, forge_brief; forge_brief.note_missing_groups(sys.argv[1])' "$plan" || true
  python3 - "$plan" "$SKILL_DIR/scripts/forge-brief.py" <<'PY' || { note "brief: could not start the question writer"; return 0; }
import os, subprocess, sys, time
plan, script = sys.argv[1], sys.argv[2]
pidfile = os.path.join(plan, 'brief.pid')
if not hasattr(os, 'fork'):
    worker = (
        'import os, subprocess, sys\n'
        'plan, script, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]\n'
        'try:\n'
        '    with open(pidfile, "w") as handle:\n'
        '        handle.write("%d\\n" % os.getpid())\n'
        '    subprocess.run([sys.executable, script, "write", plan], stdin=subprocess.DEVNULL)\n'
        'finally:\n'
        '    try: os.unlink(pidfile)\n'
        '    except OSError: pass\n'
    )
    flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
    with open(os.path.join(plan, 'brief.out'), 'ab') as out:
        proc = subprocess.Popen(
            [sys.executable, '-c', worker, plan, script, pidfile],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            creationflags=flags, close_fds=True,
        )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not os.path.exists(pidfile) and proc.poll() is None:
        time.sleep(0.02)
    sys.exit(0)
ready, signal_ready = os.pipe()
child = os.fork()
if child:
    os.close(signal_ready)
    os.read(ready, 1)          # the child wrote brief.pid (or gave up): the file is there before this returns
    sys.exit(0)
code = 1
try:
    os.close(ready)
    os.setsid()
    out = os.open(os.path.join(plan, 'brief.out'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0); os.dup2(out, 1); os.dup2(out, 2)
    with open(pidfile, 'w') as handle:
        handle.write('%d\n' % os.getpid())
    os.write(signal_ready, b'1'); os.close(signal_ready)
    code = subprocess.run([sys.executable, script, 'write', plan], stdin=subprocess.DEVNULL).returncode
except BaseException:
    pass
finally:
    try:
        os.write(signal_ready, b'1')
    except OSError:
        pass
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    os._exit(code)
PY
}

# Strict: the questions of the tasks that have not started and nobody answered, each with the command that
# answers it. Exit 3 when there are any, so the caller refuses before the scheduler starts.
forge_brief_strict() { # forge_brief_strict <plan>
  local plan="$1" rc=0
  python3 "$SKILL_DIR/scripts/forge-brief.py" write "$plan" >&2 || note "brief: the question writer did not finish; tasks without questions are not held"
  PYTHONPATH="$SKILL_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - "$plan" <<'PY' || rc=$?
import shlex, sys
import forge_brief
import forge_brief.answers as answers
plan = sys.argv[1]
try:
    found = forge_brief.collect(plan)['questions']
except Exception:
    sys.exit(0)
open_ = [q for q in found if q['state'] in ('open', 'possibly_stale') and not answers.past_start(plan, q['task'])]
if not open_:
    sys.exit(0)
err = sys.stderr
err.write('forge: strict brief: %d question(s) wait for your answer before any task starts:\n' % len(open_))
for q in open_:
    err.write('  %s %s: %s\n' % (q['task'], q['id'], q['text']))
    err.write('      default: %s\n' % (q['default'] or '(none)'))
    err.write('      forge brief answer %s %s %s "…"\n' % (shlex.quote(plan), q['task'], q['id']))
err.write('forge: nothing was started. Answer them, then run again.\n')
sys.exit(3)
PY
  return "$rc"
}

# After a merge, with brief on: flag the dependents' open questions when the merge touched files outside
# the task's own. A failure is one line and changes nothing else.
forge_brief_stale() { # forge_brief_stale <plan> <merged-task>
  [ "$(forge_brief_level "$1")" = off ] && return 0
  python3 "$SKILL_DIR/scripts/forge-brief.py" stale "$1" "$2" >/dev/null 2>&1 \
    || note "brief: could not flag the dependents of $2 as possibly stale"
  return 0
}

# The values this run is using, after profile defaults. verify_cmd and timeout are paths:
# python reads the files, so a quote or a trailing newline is not stripped on the way in.
# A failure here is one note; the run goes on. run.flags.json describes this run of the plan,
# not a choice a split or rerun copies, so it is not in the plan option lists.
forge_run_flags_write() { # forge_run_flags_write <plan> <max-parallel> [argv...]
  local plan="$1" maxp="$2" timeout_file="" verify_file="" ahead=0 sweep_max=""
  shift 2
  [ ! -s "$plan/timeout" ] || timeout_file="$plan/timeout"
  [ ! -f "$plan/verify_cmd" ] || verify_file="$plan/verify_cmd"
  [ ! -f "$plan/sweep_ahead" ] || ahead=1
  [ "$PP_SWEEP_MAX_SET" = 1 ] && sweep_max="$PP_SWEEP_MAX"
  python3 - "$plan/run.flags.json" \
    "$PP_RETRY_FAILED" "$PP_INFRA_RETRIES" "$PP_SWEEP" "$sweep_max" \
    "$PP_SWEEP_PLAUSIBLE" "$ahead" "$maxp" "$timeout_file" "$verify_file" \
    -- "$@" <<'PY' >/dev/null 2>&1 || note "could not write $plan/run.flags.json"
import json, os, sys, tempfile, time

def text(path):
    if not path:
        return None
    with open(path, 'r', encoding='utf-8', newline='') as handle:
        raw = handle.read()
    return raw if raw != '' else None

def whole(value):
    return int(value) if value != '' else None

args = sys.argv[1:]
if len(args) < 11 or args[10] != '--':
    sys.exit(1)
dest = args[0]
retry, infra, sweep, sweep_max, plausible, ahead, maxp, timeout_file, verify_file = args[1:10]
timeout = text(timeout_file)
data = {
    'retry_failed': int(retry),
    'infra_retries': int(infra),
    'sweep': sweep or None,
    'sweep_max': whole(sweep_max),
    'sweep_plausible': plausible == '1',
    'sweep_ahead': ahead == '1',
    'max_parallel': int(maxp),
    'timeout': int(timeout.strip()) if timeout is not None else None,
    'verify': text(verify_file),
    'argv': args[11:],
    'at': time.time(),
}
parent = os.path.dirname(dest) or '.'
fd, tmp = tempfile.mkstemp(prefix='.run.flags.json.', dir=parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(tmp, dest)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(1)
PY
  return 0
}

do_run() {
  local run_child="" run_stop=0 run_phase=pre run_rc=0 interrupted=0 vstopped=0
  FORGE_RUN_STOPPABLE=1
  trap run_signal TERM INT
  local PLAN="${1:?run needs a plan dir}"; shift
  FORGE_RUN_STARTED="$(forge_now)"   # the summary's overridden verdicts are this run's
  local run_argv=("$@")
  local MAXP=3 DRY=0 jev_rc=0 FORECAST=1 AUTO_SPLIT="" SPLIT_PLANNER="" is_sub=0
  forge_is_sub_plan "$PLAN" && is_sub=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-forecast) FORECAST=0; shift ;;
      --auto-split)    AUTO_SPLIT=on; shift ;;
      --no-auto-split) AUTO_SPLIT=off; shift ;;
      --split-planner) SPLIT_PLANNER="${2:?--split-planner needs a spec}"; shift 2 ;;
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
        forge_removed_flag "$1"
        die "run: unknown option '$1'"
        ;;
    esac
  done
  forge_refuse_fractal_run "$PLAN" run
  forge_profile_run_values "$PLAN"
  [ "$JEV_OPTS" = 1 ] && forge_jev_settle "$PLAN"
  [ -n "$PP_SWEEP" ] || [ "$PP_SWEEP_PLAUSIBLE$PP_SWEEP_MAX_SET" = 00 ] || die "--sweep-plausible and --sweep-max go with --sweep"
  # Auto-split (#153): kept for the plan, and refused now when there is no planner to ask.
  forge_auto_split_store "$PLAN" "$AUTO_SPLIT" "$SPLIT_PLANNER"
  case "$OUTPUT" in summary|full) ;; *) die "--output must be summary or full" ;; esac
  export FORGE_OUTPUT="$OUTPUT"
  case "$MAXP" in ''|*[!0-9]*) die "--max-parallel must be positive" ;; esac
  [ "$MAXP" -gt 0 ] || die "--max-parallel must be positive"
  # Said once, before any dispatch. The run itself still follows retry_per_model.
  if [ -f "$PLAN/retry_per_model" ]; then
    local rpm; rpm="$(tr -d '[:space:]' < "$PLAN/retry_per_model")"
    # The reading forge-route.py and the scheduler use: 1 to 99, at most 9 digits.
    if [[ "$rpm" =~ ^[0-9]{1,9}$ ]] && [ "$((10#$rpm))" -ge 1 ] && [ "$((10#$rpm))" -le 99 ]; then
      [ ! -f "$PLAN/escalate_after" ] || note "retry-per-model $((10#$rpm)) replaces --escalate-after for this plan"
    else
      note "retry_per_model holds '$(printf '%.40s' "$rpm")', not a number from 1 to 99, so this run ignores it"
    fi
  fi
  local T="$PLAN/tasks.tsv" W="$PLAN/waves.tsv"
  [ -s "$W" ] || die "no waves.tsv — run the plan subcommand first"
  grep -q 'UNASSIGNED' "$T" && die "some tasks are UNASSIGNED — assign a dwarf before running"
  # groups.tsv and its members' deps and files are as the plan froze them (#140). Nothing to
  # check without groups; checked before anything is dispatched, a dry run included.
  if [ -f "$PLAN/groups.tsv" ] || [ -f "$PLAN/groups.frozen" ]; then
    local groups_out
    groups_out="$(forge_groups_py check "$PLAN" 2>&1)" || forge_groups_die "$groups_out"
  fi

  compute_waves "$PLAN"
  local REPO run_id wt_root int_wt int_br
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  # A plan made with --after: the plan it follows must still end where this one starts (#143).
  plans_chain_run_check "$PLAN" "$REPO"
  # Before any decision-model step: the privacy floor and the run budget read these.
  [ "$JEV_OPTS" = 1 ] && forge_decide_env "$REPO" "$PLAN"
  if [ "$DRY" != 1 ]; then
    # The concurrency limit this run was given, for forge-ui to draw; a later run rewrites it.
    printf '%s\n' "$MAXP" > "$PLAN/max_parallel"
    # Plan registers too; this also picks up plans made before it did. A sub-plan (#153) is reached
    # through the plan it belongs to, and its limits are that plan's.
    if [ "$is_sub" = 0 ]; then
      forge_ui_register "$PLAN"
      forge_storage_check "$PLAN" "$REPO"
      # The decomposition limits do not change once the plan runs.
      forge_decompose_py freeze "$PLAN" >/dev/null 2>&1 || note "could not freeze the decomposition limits (decomposition.json)"
    fi
  fi
  # Durable, beside the repo — never TMPDIR. A temp worktree root has destroyed
  # real work here before: isolated worktrees are never pushed, so when the root
  # goes, the only copy of that work goes with it.
  wt_root="$(forge_host_path "$(cd "$REPO/.." && pwd)/.forge-worktrees/$run_id")"
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

  # run.flags.json describes this run of the plan, not a choice a split or rerun copies.
  forge_run_flags_write "$PLAN" "$MAXP" ${run_argv[@]+"${run_argv[@]}"}

  forge_metric_begin "$PLAN" parallel
  forge_metric_phase preflight
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  [ ! -f "$PLAN/no_ripwire" ] || export FORGE_RIPWIRE=off
  preflight_plan "$PLAN" || return $?
  [ -z "$PP_SWEEP" ] || sweep_preflight "$PLAN" || return $?
  # The planner --auto-split would ask, checked with the rest before any model is paid for.
  if [ -f "$PLAN/auto_split" ] && [ -n "$(forge_split_planner "$PLAN")" ]; then
    /bin/bash "$DISPATCH" doctor --spec "$(forge_split_planner "$PLAN")" --role planner $(forge_fast_tier "$PLAN" planner) || return $?
  fi
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
    # A chained plan starts where the plan it follows ended, not at HEAD, even when its branch is gone.
    local int_start=HEAD
    [ ! -s "$PLAN/after" ] || [ ! -s "$PLAN/integration.base" ] || int_start="$(head -1 "$PLAN/integration.base")"
    (cd "$REPO" && git branch "$int_br" "$int_start") || die "could not create $int_br" 3
    # Where the integration branch started, so a whole-run review can diff exactly what
    # this run added. Plans created before this file existed fall back to a merge-base.
    git -C "$REPO" rev-parse "$int_start" > "$PLAN/integration.base"
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
  # Invalid verification lanes stop the run here, before any model is paid for, so before the question writer below.
  # A sub-plan (#153) has no verification of its own: its task's review and the root plan's verification judge it.
  [ "$is_sub" = 1 ] || verify_lanes_check "$PLAN" "$REPO" "$int_wt"
  # Brief mode (#135), when the plan has it on. Strict asks first and refuses to start while a question of a
  # task that has not started is unanswered; advisory and gated write the questions beside the scheduler.
  local brief_level
  brief_level="$(forge_brief_level "$PLAN")"
  case "$brief_level" in
    strict) forge_brief_strict "$PLAN" || exit $? ;;
    advisory|gated) forge_brief_start "$PLAN" ;;
  esac
  # Say now, before any model is paid for, which suites the verify command will not run.
  [ "$is_sub" = 1 ] || verify_coverage_report "$REPO" "$(verify_command_for "$PLAN" "$REPO" "$int_wt")" "$PLAN/verification.coverage.txt"
  # What a split task's own run of its subtasks takes from this run (#153): the budgets, and one
  # --max-parallel for the whole tree, counted in the root plan.
  export FORGE_RUN_RETRY_FAILED="$PP_RETRY_FAILED" FORGE_RUN_INFRA_RETRIES="$PP_INFRA_RETRIES"
  if [ "$is_sub" = 0 ] || [ -z "${FORGE_TREE_ROOT:-}" ]; then
    FORGE_TREE_ROOT="$(forge_decompose_py root "$PLAN" 2>/dev/null)"; FORGE_TREE_ROOT="${FORGE_TREE_ROOT:-$PLAN}"
    export FORGE_TREE_ROOT FORGE_TREE_CAP="$MAXP"
  fi
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
  if [ "$paused" = 0 ] && [ "$interrupted" = 0 ] && [ "$is_sub" = 0 ]; then
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

  # After the scheduler (and verification) and before the summary. Retro off: no call,
  # no retro.md. A failure here does not change the exit code decided below.
  local retro_line=""
  retro_line="$(forge_retro_summary_line "$PLAN")"

  echo
  echo "results (integration branch: $int_br)"
  echo "artifacts: $PLAN/tasks/<id>/{dwarf,qa}.{last,log}; results: $PLAN/results.tsv"
  # 14 covers NEEDS_DECISION; 9 pushes the branch column out for IMPLEMENTED (11) too.
  render_results_table "$PLAN"
  echo
  if [ -n "$retro_line" ]; then printf '%s\n\n' "$retro_line"; fi
  run_report_extras "$PLAN"
  # Pre-review tests (#85): every check of the plan, and the reviews the red ones saved.
  local pre_review
  pre_review="$(cat "$PLAN"/tasks/*/prereview.log 2>/dev/null | awk -F'\t' 'NF >= 3 { n++; if ($2 == "failed") f++; s += $3 }
    END { if (n) printf "pre-review tests: %d check(s), %d attempt(s) sent back without a review, %ds of test time\n", n, f, s }')"
  [ -z "$pre_review" ] || { echo "$pre_review"; echo; }

  # Undeclared files are reported here rather than buried in a task log, because
  # this is the list that explains any CONFLICT above.
  local drift=0 id_w
  id_w="$(id_col_width "$T")"
  for id in $(all_ids "$T"); do
    if [ -s "$PLAN/tasks/$id/drift.txt" ]; then
      [ "$drift" = 0 ] && echo "scope drift (touched files the task did not declare):"
      drift=1
      printf '  %-*s %s\n' "$id_w" "$id" "$(drift_labelled "$PLAN/tasks/$id")"
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
  # Split tasks whose subtasks have not all merged (#153): not failures of the task either.
  report_subtasks "$PLAN"
  # Members implemented and waiting for their group's review (#140): not failures either.
  local implemented; implemented="$(awk -F'\t' '$2=="IMPLEMENTED" {print $1}' "$PLAN/results.tsv")"
  if [ -n "$implemented" ]; then
    echo "implemented, waiting for their group's review:"
    for id in $implemented; do
      printf '  %s: group %s\n' "$id" "$(forge_groups_id_of "$PLAN" "$id")"
    done
    echo
  fi
  # The retry hint is for tasks that actually failed; a run that only failed verification,
  # or only paused on infrastructure, has nothing to retry task by task. An interrupted task
  # has its own hint above, a task waiting for an answer or a decision its own block.
  if [ "$(awk -F'\t' '$2!="MERGED" && $2!="INFRA" && $2!="PENDING" && $2!="BLOCKED" && $2!="INTERRUPTED" && $2!="NEEDS_INPUT" && $2!="NEEDS_DECISION" && $2!="IMPLEMENTED" {n++} END {print n+0}' "$PLAN/results.tsv")" -gt 0 ]; then
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
  # A goal larger than this plan (#153): once this stage has merged, the next one can be planned.
  [ "$failed" = 1 ] || [ "$interrupted" = 1 ] || [ "$paused" = 1 ] || report_later "$PLAN"
  local mem_home=""
  [ -f "$PLAN/no_memory" ] || mem_home="$(/bin/bash "$MEMORY" where "$REPO" 2>/dev/null)"
  if [ -n "$mem_home" ] && [ -s "$mem_home/.forge/memory.md" ]; then
    if [ "$mem_home" = "$REPO" ]; then
      echo "project memory: $(grep -c '^- ' "$mem_home/.forge/memory.md") fact(s) in .forge/ —"
      echo "  uncommitted, and the only thing forge wrote outside its own branches."
      echo
      echo "Your branch and working tree were not touched, apart from .forge/ above."
    else
      # #159: a linked worktree's learnings live in the main checkout, so removing it loses none.
      echo "project memory: $(grep -c '^- ' "$mem_home/.forge/memory.md") fact(s) in $mem_home/.forge/ —"
      echo "  the main checkout's, since $REPO is a linked worktree: removing that worktree loses none of them."
      echo "  Uncommitted, and the only thing forge wrote outside its own branches."
      echo
      echo "Your branch and working tree were not touched."
    fi
  else
    echo "Your branch and working tree were not touched."
  fi
  [ "$interrupted" = 1 ] && return 130
  [ "$paused" = 1 ] && return 8
  [ "$failed" = 1 ] && return 5
  return 0
}

# The changes in the user's checkout that make it dirty, as `git status --porcelain` lines, leaving
# out forge's own project memory (#156): .forge/memory.md and .forge/ledger.tsv are written into the
# repository by forge and never committed by it, so they must not block forge's own integrate, nor be
# what a person is told to commit or stash.
user_tree_changes() { # user_tree_changes <repo>
  git -C "$1" status --porcelain -- . ':(exclude).forge/memory.md' ':(exclude).forge/ledger.tsv' 2>/dev/null
}

# --- verify -------------------------------------------------------------------
# The plan's verification of its integration branch, and nothing else (#151): after a lane was
# fixed, or to tell a flaky failure from a real one, without a `run` that has nothing to dispatch.
# The same lanes or command a run would use, on the same checkout, with its results in the plan.
do_verify() {
  local PLAN="${1:?verify needs a plan dir}"; shift
  while [ $# -gt 0 ]; do
    if forge_pp_flag verify "$PLAN" "$@"; then shift "$PP_SHIFT"; else die "verify: unknown option '$1'"; fi
  done
  [ -s "$PLAN/tasks.tsv" ] || die "no tasks.tsv in '$PLAN'" 3
  [ -s "$PLAN/wt_root" ] && [ -s "$PLAN/repo" ] && [ -s "$PLAN/accepted.integration" ] \
    || die "this plan has never been run, so it has no integration branch to verify" 3
  local REPO int_wt
  REPO="$(cat "$PLAN/repo")"; int_wt="$(cat "$PLAN/wt_root")/_integration"
  [ -e "$int_wt/.git" ] || die "the integration worktree $int_wt is missing" 3
  [ "$(git -C "$int_wt" rev-parse HEAD 2>/dev/null)" = "$(cat "$PLAN/accepted.integration")" ] \
    || die "integration branch changed outside this run. To bring those commits into the run: forge-parallel.sh adopt $PLAN --head --reason \"...\"" 3
  verify_lanes_check "$PLAN" "$REPO" "$int_wt"
  local left="" lid
  for lid in $(all_ids "$PLAN/tasks.tsv"); do
    case "$(task_status "$PLAN" "$lid")" in PENDING|BLOCKED|RUNNING) left="${left:+$left }$lid" ;; esac
  done
  [ -z "$left" ] || note "tasks still to run ($left): this verifies the integration branch as it is now, without them"
  local rc=0
  verify_result "$PLAN" "$int_wt" "$PLAN" || rc=$?
  write_results "$PLAN"
  report_verify_notes "$PLAN"
  echo "verification of $(git -C "$int_wt" rev-parse --short HEAD 2>/dev/null): $(cat "$PLAN/verification.status" 2>/dev/null || echo UNVERIFIED)"
  case "$rc" in 0) return 0 ;; 130) return 130 ;; *) return 5 ;; esac
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
  ! forge_is_sub_plan "$PLAN" || die "integrate: '$PLAN' is the sub-plan of a split task; its work merges into that task. Integrate the plan it belongs to: $(forge_decompose_py root "$PLAN" 2>/dev/null)" 3
  report_unmerged "$PLAN"
  local REPO run_id int_br cur
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"; int_br="forge/$run_id-integration"
  cur="$(cd "$REPO" && git rev-parse --abbrev-ref HEAD)"
  [ "$cur" = HEAD ] && die "integration requires a checked-out user branch" 3
  [ "$cur" = "$int_br" ] && die "you are already on $int_br"
  local dirty; dirty="$(user_tree_changes "$REPO")"
  [ -z "$dirty" ] || die "working tree is dirty — commit or stash before integrating (forge's own .forge/memory.md and .forge/ledger.tsv do not count):
$(printf '%s\n' "$dirty" | head -10)"
  # Verify the actual combined candidate, including changes on the user's branch.
  local parent target candidate candidate_sha out
  parent="$(git -C "$REPO" rev-parse HEAD)" || return 3
  target="$(git -C "$REPO" rev-parse "$int_br")" || return 3
  [ -s "$PLAN/accepted.integration" ] && [ "$target" = "$(cat "$PLAN/accepted.integration")" ] || die "integration revision does not match the accepted run" 3
  # The branch holds the work of every plan it follows: one merge brings them all.
  [ ! -s "$PLAN/after" ] || echo "this brings plans $(plans_chain_names "$PLAN")"
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
    [ -z "$(user_tree_changes "$REPO")" ] || die "branch or working tree changed during verification" 3
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

# The planner's notes, once. A dispatch that ran before `plan` wrote run_id leaves
# AUTO_PLANNER_RECORDED=0; recording then would store run_id null for good. The caller
# records after `plan`, or on an exit that will never reach it. A later dispatch (the
# send-back) already has the file and records itself.
forge_auto_record_planner() { # forge_auto_record_planner <plan>
  [ "${AUTO_PLANNER_RECORDED:-1}" = 0 ] || return 0
  forge_retro_record "$1" planner "$1"
  AUTO_PLANNER_RECORDED=1
}

# One planner dispatch: prompt in $PLAN/planner.prompt, reply in $PLAN/planner.last. Sets
# AUTO_RC (0, 4 or 8) for the caller instead of exiting, so a send-back can reuse it.
forge_auto_dispatch() { # forge_auto_dispatch <plan-dir> <repo> <spec> <fast-option-or-empty>
  local plan="$1" repo="$2" spec="$3" tier="$4" rc=0
  AUTO_RC=0
  AUTO_PLANNER_RECORDED=0
  note "auto: dispatching the planner ($spec) for the whole plan"
  # Last. The planner runs before `plan` stores retro_mode; AUTO_RETRO_FLAG says whether
  # this call's flags will turn it on. Off appends nothing, so the file stays as written.
  forge_retro_prompt "$plan" planner >> "$plan/planner.prompt"
  bash "$DISPATCH" planner "$spec" --repo "$repo" --run-dir "$plan" --prompt-file "$plan/planner.prompt" \
    --output summary ${tier:+$tier} >&2 || rc=$?
  # No task: the planner is the whole plan. Its result is AUTO_RC, set below, not this.
  # run_id is written by `plan`, which has not run yet on a new directory. Record now only
  # when that file is already there (a send-back); otherwise the caller records after `plan`.
  if [ -s "$plan/run_id" ]; then
    forge_retro_record "$plan" planner "$plan"
    AUTO_PLANNER_RECORDED=1
  fi
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

    # The flag `plan` is about to store. The planner is dispatched before that write, and
    # FORGE_RETRO=off still wins inside forge_retro_on. Last flag wins, as plan's own scan.
    AUTO_RETRO_FLAG=""
    local retro_scan=""
    for retro_scan in ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}; do
      case "$retro_scan" in
        --retro) AUTO_RETRO_FLAG=on ;;
        --no-retro) AUTO_RETRO_FLAG=off ;;
      esac
    done
    note "auto: checking the planner ($PLANNER) before any model call"
    bash "$DISPATCH" doctor --spec "$PLANNER" --role planner ${fast_tier:+$fast_tier} >&2 || exit $?
    python3 "$PLANNER_PY" prompt --plan "$PLAN" --goal-file "$REQUEST" ${DLEVEL:+--level "$DLEVEL"} >/dev/null || exit $?
    forge_auto_dispatch "$PLAN" "$REPO" "$PLANNER" "$fast_tier"
    if [ "$AUTO_RC" -ne 0 ]; then
      forge_auto_record_planner "$PLAN"
      exit "$AUTO_RC"
    fi
    python3 "$PLANNER_PY" parse --plan "$PLAN" --reply "$PLAN/planner.last" || rc=$?
    case "$rc" in
      0) ;;
      4) forge_auto_record_planner "$PLAN"
         note "auto: the planner's reply was refused (see $PLAN/planner.refused). Nothing was dispatched after the planner"
         exit 4 ;;
      *) forge_auto_record_planner "$PLAN"
         exit "$rc" ;;
    esac
    from_planner=1
  fi

  # 4. plan, and one send-back when the plan gates ask for it.
  bash "$SELF" plan "$PLAN" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}; rc=$?
  # planner.last is still the first reply. plan has written run_id when it got that far,
  # and the send-back below overwrites the reply, so this is the moment to record it.
  forge_auto_record_planner "$PLAN"
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
    if [ "$AUTO_RC" -ne 0 ]; then
      forge_auto_record_planner "$PLAN"
      exit "$AUTO_RC"
    fi
    rc=0
    python3 "$PLANNER_PY" parse --plan "$PLAN" --reply "$PLAN/planner.last" || rc=$?
    case "$rc" in
      0) ;;
      4) forge_auto_record_planner "$PLAN"
         note "auto: the planner's second reply was refused (see $PLAN/planner.refused). Nothing was dispatched after the planner"
         exit 4 ;;
      *) forge_auto_record_planner "$PLAN"
         exit "$rc" ;;
    esac
    bash "$SELF" plan "$PLAN" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}; rc=$?
    forge_auto_record_planner "$PLAN"
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
  exit 0
fi
[ $# -ge 1 ] || die "usage: forge-parallel.sh <plan|auto|run|retry|accept|adopt|review|integrate|split|attribute|verify|combine|next> <plan-dir> [...]"
CMD="$1"; shift
[ ! -f "${1:-}/no_ripwire" ] || export FORGE_RIPWIRE=off
if [ -d "${1:-}" ]; then
  PLAN_PATH="$(cd "$1" && pwd)"; shift; set -- "$PLAN_PATH" "$@"
fi
case "$CMD" in
  # `combine` takes a plan dir that does not exist yet and locks its sources itself.
  run|retry|integrate|accept|adopt|review|split|attribute|verify)
    if [ "$(forge_posix_path "${FORGE_PLAN_LOCK:-}")" != "${1:-}" ]; then
      exec python3 "$SKILL_DIR/scripts/forge-schedule.py" lock "$SELF" "$CMD" "$@"
    fi ;;
esac
# Every user-facing subcommand leaves one record in the run dataset, whatever its exit
# path (die included). Installed after the lock re-exec, so it fires once, in the process
# that did the work; `_task` and `_merge` are parts of a `run` and are not recorded alone.
case "$CMD" in
  plan|run|retry|integrate|accept|adopt|review|split|attribute|verify|combine)
    FORGE_DATASET_INVOCATION="inv-$(date +%s)-$$-$RANDOM"; export FORGE_DATASET_INVOCATION
    FORGE_DATASET_STARTED="$(date +%s)"
    FORGE_DATASET_ARGV=("$@")
    # An adopt's or final review's snapshot goes too, whatever the exit path (#139).
    trap 'forge_dataset_rc=$?; trap - EXIT; forge_drop_snapshot "${FORGE_REVIEW_SNAPSHOT:-}"; forge_drop_snapshot "${FINAL_REVIEW_SNAPSHOT:-}"; forge_dataset_invocation "$CMD" "$forge_dataset_rc" "$FORGE_DATASET_STARTED" ${FORGE_DATASET_ARGV[@]+"${FORGE_DATASET_ARGV[@]}"}; exit "$forge_dataset_rc"' EXIT ;;
esac
# Subcommands implemented in the optional libraries fail clearly if the library is absent.
need_fn() { declare -F "$1" >/dev/null || die "'$CMD' is not available: $1 is missing" 3; }
# A plan with a frozen profile asks its decision-model gates from that copy (`plan` does it itself,
# after it freezes). The internal subcommands inherit it from the `run` that started them.
case "$CMD" in run|retry|integrate|accept|adopt|review|split|attribute|verify) forge_profile_export "${1:-}" ;; esac
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
  verify)    do_verify "$@" ;;
  combine)   need_fn do_combine; do_combine "$@" ;;
  next)      need_fn do_next;    do_next "$@" ;;
  _task)     do_task "$@" ;;
  _merge)    merge_task "$@" ;;
  _group)    need_fn do_group; do_group "$@" ;;
  _group_settle) need_fn do_group_settle; do_group_settle "$@" ;;
  _merge_group)  need_fn merge_group; merge_group "$@" ;;
  # The live scheduler's accept (forge control accept, #138): it holds the plan lock already.
  _accept)   need_fn do_accept; FORGE_ACCEPT_LIVE=1 do_accept "$@" ;;
  _prepare_retry) retry_for_scheduler "$@" ;;
  _qa_only)  qa_only_next "$@" ;;
  _waves)    compute_waves "$@" ;;
  _sweep)    sweep_plan "$@" ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
