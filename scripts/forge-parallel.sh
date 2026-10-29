#!/usr/bin/env bash
# forge-parallel.sh — decomposed forge runs: many dwarves working concurrently in
# isolated git worktrees, each task reviewed by its own QA pass, only passing work
# merged. Bash 3.2 entrypoints keep the task and merge contracts; a Python 3
# standard-library coordinator schedules ready tasks without wave barriers.
#
# Usage:
#   forge-parallel.sh plan <plan-dir> --repo <dir> [routing flags] [--decompose-level low|medium|high] [--ui]
#                                     [--qa-threshold P0|P1|P2|P3|none|default]
#   forge-parallel.sh run  <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa]
#                                     [--verify <command>] [--setup <command>] [--dry-run]
#                                     [--timeout S] [--retry-failed N] [--infra-retries M]
#                                     [--qa-threshold P0|P1|P2|P3|none|default] [--verify-retries N] [--no-forecast]
#                                     [--sweep P0|P1|P2|P3 [--sweep-plausible] [--sweep-max N]]
#                                     [--sweep-ahead | --no-sweep-ahead]
#   forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>] [--setup <command>]
#                                     [--timeout S] [--qa-threshold Pn|none|default] [--verify-retries N]
#   forge-parallel.sh integrate <plan-dir> --approved [--verify-retries N] [--final-review <qa-spec>]
#   forge-parallel.sh review <plan-dir> [--qa <spec>] [--yolo-qa] [--qa-threshold Pn|none|default]
#   forge-parallel.sh accept <plan-dir> <task-id> --reason "<why>" --approved
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
#   --explore E             on by default at 0.2 (0 turns it off, and is kept): a row left
#                           to forge gets, with probability E/2, its neighbouring model
#                           (one tier away in the given pools) instead of its pool's turn;
#                           each decision and its odds go to tasks/<id>/route.jsonl
#                           (precedence: flag, this plan's earlier choice, FORGE_ROUTE_EXPLORE)
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
for forge_lib in task verify plans review; do
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

do_plan() {
  local PLAN="${1:?plan needs a plan dir}"; shift
  local REPO="" D_ANY="" D_HI="" D_MD="" D_LO="" Q_ANY="" Q_HI="" Q_MD="" Q_LO=""
  local PLANNER="" NOMEM=0 jev_rc=0 DLEVEL="" SHADOW_QA="" SHADOW_RATE="" EXPLORE="" FAIL_FIRST="" REVIEWER_SESSION=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)         REPO="${2:?}"; shift 2 ;;
      --planner)      PLANNER="${2:?}"; shift 2 ;;
      --decompose-level)
        case "${2:-}" in low|medium|high) DLEVEL="$2"; shift 2 ;;
          *) die "--decompose-level must be low, medium or high" ;; esac ;;
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --no-memory)    NOMEM=1; shift ;;
      --shadow-qa)
        [ -n "${2:-}" ] || die "--shadow-qa needs a QA spec, a comma list of specs, or none"
        SHADOW_QA="$2"; shift 2 ;;
      --shadow-rate)
        printf '%s' "${2:-}" | grep -Eq '^(0?\.[0-9]+|0|1|1\.0+)$' || die "--shadow-rate needs a share between 0 and 1"
        SHADOW_RATE="$2"; shift 2 ;;
      --explore)      EXPLORE="${2:?--explore needs a share between 0 and 0.5}"; shift 2 ;;
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
      --reviewer-session)    REVIEWER_SESSION=on; shift ;;
      --no-reviewer-session) REVIEWER_SESSION=off; shift ;;
      *)
        # `case` is not a loop, so `continue` here belongs to the enclosing while.
        # The shared flags write into the plan dir (--qa-threshold), so it must exist first.
        [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
        if forge_pp_flag plan "$PLAN" "$@"; then shift "$PP_SHIFT"; continue; fi
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
  [ "$JEV_OPTS" = 1 ] && forge_jev_export
  [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
  # Freeze the selection next to the task table, so run and retry restore it rather
  # than re-deciding. Written on every plan: a re-plan is a new decision.
  [ "$JEV_OPTS" = 1 ] && forge_jev_record "$PLAN"
  local T="$PLAN/tasks.tsv"
  [ -r "$T" ] || die "no tasks.tsv in '$PLAN' — decompose the goal first"
  # The table as the planner or orchestrator wrote it, before Jev or route resolution
  # rewrites any column: which tier a task was DECLARED is a label of its own.
  cp "$T" "$PLAN/tasks.declared.tsv"
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
  # Reviewer-session continuation (opt-in experiment): the file holds `on`. Readers only
  # test that it exists; the dataset records its content.
  case "$REVIEWER_SESSION" in on) printf '%s\n' on > "$PLAN/reviewer_session" ;; off) rm -f "$PLAN/reviewer_session" ;; esac
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

  # Uncommitted work is invisible to every dwarf, because worktrees branch from a
  # commit. That is the likeliest way to get a confusingly wrong result, so it is
  # stated loudly rather than left for the user to deduce from an odd diff.
  if [ -n "$(cd "$REPO" && git status --porcelain 2>/dev/null)" ]; then
    note "WARNING: '$REPO' has uncommitted changes. Task worktrees branch from HEAD,"
    note "         so those edits will NOT be visible to any dwarf. Commit or stash first"
    note "         if the tasks depend on them."
  fi

  # --- jev: plan-time gates ------------------------------------------------------
  # Prompt adequacy and drift, advisory: warnings about the task text and its declared
  # files. Nothing here changes tasks.tsv.
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
    # --jev-shadow before migration keeps its gates `on` and prints a count, as it always did.
    if [ "$jev_rc" -eq 0 ] && [ -n "$jev_scores" ] && [ "${FORGE_JEV_SHADOW:-}" = on ]; then
      note "jev: scored $(printf '%s\n' "$jev_scores" | grep -c .) task(s) in shadow mode (no change)"
    elif [ "$jev_rc" -eq 0 ] && [ -n "$jev_scores" ]; then
      local jid jwarn jdrift
      # The columns score-plan prints: id, warnings, drift. Read in lockstep with
      # cmd_score_plan.
      while IFS="$(printf '\t')" read -r jid jwarn jdrift; do
        [ -n "$jid" ] || continue
        case "$jwarn" in
          *prompt_adequacy*) note "jev: $jid — the prompt may be too vague for QA to judge correctness against" ;;
        esac
        if [ -n "$jdrift" ] && [ "$jdrift" != "-" ]; then
          note "jev: $jid may also edit undeclared files: $jdrift"
          note "     undeclared files break wave disjointness — add them to the files column or split the task"
        fi
      done <<EOF
$jev_scores
EOF
    fi
  fi

  # --- validate + resolve routing ---
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
      if [ -n "$EXPLORE" ] && [ "$dw" != UNASSIGNED ]; then
        local explored
        explored="$(python3 "$SKILL_DIR/scripts/forge-route.py" explore --plan "$PLAN" --task "$id" \
          --tier "$diff" --epsilon "$EXPLORE" --default "$dw" --pools "$PLAN/fractal-routing.json" 2>/dev/null)"
        [ -z "$explored" ] || dw="$explored"
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

  compute_waves "$PLAN"

  # --- jev: advisory forward dependency -------------------------------------------
  # Wave order is structural, but it cannot see an earlier-wave task written against
  # an interface a later-wave task builds. This is advisory only: it never edits
  # tasks.tsv, never adds a dep, and
  # never changes waves.tsv, so it must run after compute_waves and before the table
  # that reports the plan is printed.
  if [ "$jev_loaded" = 1 ] && forge_decide_active forward; then
    local jev_coupling jev_coupling_rc
    jev_coupling="$(python3 "$SKILL_DIR/scripts/forge-jev.py" coupling \
      --plan "$PLAN" --repo "$REPO" 2>/dev/null)"
    jev_coupling_rc=$?
    # A shadowed forward gate is recorded in jev-coupling.json and prints no row; --jev-shadow
    # before migration prints a count, as it always did.
    if [ "$jev_coupling_rc" -eq 0 ] && [ -n "$jev_coupling" ] && [ "${FORGE_JEV_SHADOW:-}" = on ]; then
      note "jev: found $(printf '%s\n' "$jev_coupling" | grep -c .) coupled pair(s) in shadow mode (no change)"
    elif [ "$jev_coupling_rc" -eq 0 ] && [ -n "$jev_coupling" ]; then
      local ca cb cp ckind
      while IFS="$(printf '\t')" read -r ca cb cp ckind; do
        [ -n "$ca" ] || continue
        # Forward: an earlier task written against work that has not been built yet,
        # which its own review cannot catch.
        if [ "$ckind" = forward ]; then
          note "jev: task '$ca' runs before '$cb' and may depend on what it builds ($cp) — consider a dep"
        fi
      done <<EOF
$jev_coupling
EOF
    fi
  fi

  render_table "$PLAN"
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
  if [ -f "$PLAN/reviewer_session" ]; then
    echo "reviewer-session: a later review round continues the reviewer's earlier session when its QA spec is unchanged (experiment; falls back to a fresh session)"
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
  local plan="$1" id role flags
  export FORGE_CAPABILITY_CACHE="$plan/capabilities"
  for id in $(all_ids "$plan/tasks.tsv"); do
    for role in dwarf qa; do
      flags=""; [ -f "$plan/yolo_$role" ] && flags=--yolo
      /bin/bash "$DISPATCH" doctor --spec "$(field "$plan/tasks.tsv" "$id" "$role")" --role "$role" $flags || return $?
    done
  done
  if [ -s "$plan/planner" ]; then
    /bin/bash "$DISPATCH" doctor --spec "$(cat "$plan/planner")" --role planner || return $?
  fi
  # A shadow reviewer that cannot run here is dropped with a note, never a failed run.
  if [ -s "$plan/shadow_qa" ]; then
    python3 "$SKILL_DIR/scripts/forge-shadow.py" preflight --plan "$plan" || true
  fi
}

# The implementer a sweep task gets when the plan has a low tier (or a plain --dwarf pool): checked
# with the rest, before any model is paid for, since no task may have used it yet.
sweep_preflight() { # sweep_preflight <plan>
  local spec flags=""
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
  /bin/bash "$DISPATCH" doctor --spec "$spec" --role dwarf $flags
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

do_task_body() (
  local PLAN="${1:?}" id="${2:?}"
  local T="$PLAN/tasks.tsv" REPO run_id base wt br tdir attempt resume=""
  local reviewed review source_fp review_fp
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  tdir="$PLAN/tasks/$id"; mkdir -p "$tdir"
  if [ -s "$PLAN/fractal-selection.json" ]; then
    forge_fractal_select "$PLAN" "$REPO" parallel 0 || return $?
  fi
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
  rm -f "$tdir/qa.gate" "$tdir/known_issues.md" "$tdir/invalidated.reason"

  # The base is pinned per task on first creation and reused by every retry. The
  # integration branch moves as later waves land, so re-reading the run-level
  # base_ref on a retry would pull other tasks' merged code into this task's diff
  # and put it in front of its reviewer as if this dwarf had written it.
  if [ -s "$tdir/base_ref" ]; then
    base="$(cat "$tdir/base_ref")"
  else
    base="$(git -C "$(cat "$PLAN/wt_root")/_integration" rev-parse HEAD)"; printf '%s\n' "$base" > "$tdir/base_ref"
  fi

  local dw qa yd yq rc
  dw="$(field "$T" "$id" dwarf)"; qa="$(field "$T" "$id" qa)"
  yd=""; yq=""
  [ -f "$PLAN/yolo_dwarf" ] && yd="--yolo"
  [ -f "$PLAN/yolo_qa" ] && yq="--yolo"

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
      python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" --memory "$tdir/dwarf.memory"
      [ ! -f "$PLAN/sweep_ahead" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" ahead "$PLAN" "$id" --role dwarf 2>/dev/null
      echo "Implement this task in the repository. Follow the intended approach unless it is wrong; explain deviations. Run the task's requested verification and report results."
      echo "On retry, fix the findings in the existing work; preserve parts not criticised. Explain any finding you reject."
      echo; python3 "$SKILL_DIR/scripts/forge-contract.py" dwarf
      echo; /bin/bash "$MEMORY" note dwarf
    } > "$tdir/dwarf.input"
    guard_before_dwarf "$wt" "$tdir"
    forge_metric_phase dispatch
    /bin/bash "$DISPATCH" dwarf "$dw" --repo "$wt" --run-dir "$tdir" \
          --ripwire-query-file "$tdir/prompt.md" --prompt-file "$tdir/dwarf.input" $yd --output "${FORGE_OUTPUT:-summary}" >"$tdir/dwarf.out" 2>&1
    rc=$?
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
    if [ -s "$tdir/changes.prev.diff" ] && cmp -s "$tdir/changes.diff" "$tdir/changes.prev.diff"; then
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
  else
    reviewed="$(cat "$tdir/reviewed.commit")"
  fi
  review="$tdir/review-$attempt-$$"
  forge_review_snapshot "$wt" "$base" "$reviewed" "$review" || { echo ERROR > "$tdir/status"; return 1; }
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
    python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$tdir/prompt.md" --capsule "$tdir/baseline.capsule" --approach "$tdir/approach.md" --retry "$tdir/retry_findings.md" ${prompt_extra[@]+"${prompt_extra[@]}"} --memory "$tdir/dwarf.memory" --memory "$tdir/qa.memory"
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part head --kind "$qa_kind"
    echo
    [ ! -f "$PLAN/sweep_ahead" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" ahead "$PLAN" "$id" --role qa 2>/dev/null
    if [ -s "$tdir/failfirst.txt" ]; then cat "$tdir/failfirst.txt"; echo; fi
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
        /bin/bash "$DISPATCH" doctor --spec "$qa" --role qa $yq > "$tdir/qa.doctor" 2>&1 || : > "$tdir/qa.doctor"
      IFS=$'\t' read -r rs_mode rs_reason rs_id < <(review_session_decide \
        "$tdir/retry_findings.session" "$tdir/retry_findings.resolved" "$tdir/qa.doctor")
      [ "$rs_mode" = continued ] && rs_cont=(--continue-session "$rs_id")
    fi
  fi
  local qa_cmd=(/bin/bash "$DISPATCH" qa "$qa" --repo "$review" --run-dir "$tdir" \
        --review-base "$(cat "$tdir/review.base")" --ripwire-query-file "$tdir/prompt.md" --prompt-file "$tdir/qa.input" ${yq:+"$yq"} --output "${FORGE_OUTPUT:-summary}")
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
  local rc
  do_task_body "$@"; rc=$?
  record_attempt "$1" "$2"
  # The retry policy's state (escalation.next or escalation.stop), decided now rather than
  # at the next retry so a spent retry budget still reports it and the scheduler sees it.
  python3 "$SKILL_DIR/scripts/forge-route.py" escalation --plan "$1" --task "$2" --check \
    --qa-only-next "$(qa_only_next_flag "$1" "$2")" >/dev/null 2>&1 || true
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
# A failed task keeps its branch and worktree "so there is something to inspect and
# retry from" — this is the retry. It is a command a human types, not an automatic
# repair loop, which is what keeps forge's one-dwarf-then-QA rule intact.
do_retry() {
  local PLAN="${1:?retry needs a plan dir}" id="${2:?retry needs a task id}"; shift 2
  FORGE_RUN_STARTED="$(forge_now)"
  local NEWDWARF="" jev_rc=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      --output) OUTPUT="${2:?}"; shift 2 ;;
      --dwarf) NEWDWARF="${2:?--dwarf needs a spec}"; shift 2 ;;
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

  # Findings carry-forward, the optional --dwarf escalation and the QA-only resume
  # decision are shared with the scheduler's automatic retries.
  # A refusal (the retry policy found no stronger model, or the escalation could not be
  # written) must stop here: past this point the status is cleared and a dwarf dispatched.
  local prc=0
  prepare_retry "$PLAN" "$id" "$NEWDWARF" || prc=$?
  if [ "$prc" != 0 ]; then
    write_results "$PLAN"
    [ "$prc" = 3 ] && return 2
    return "$prc"
  fi
  [ "${FORGE_RIPWIRE:-}" != off ] || touch "$PLAN/no_ripwire"
  dependencies_ready "$PLAN" "$id" || { write_results "$PLAN"; return 5; }
  local vrepo vcheckout; vrepo="$(cat "$PLAN/repo" 2>/dev/null)"; vcheckout="$(cat "$PLAN/wt_root")/_integration"
  [ -d "$vcheckout" ] || vcheckout="$vrepo"
  verify_lanes_check "$PLAN" "$vrepo" "$vcheckout"
  [ ! -f "$PLAN/no_ripwire" ] || export FORGE_RIPWIRE=off
  preflight_plan "$PLAN" || return $?
  /bin/bash "$SKILL_DIR/scripts/forge-install-ripwire.sh" || true
  rm -f "$tdir/status"

  note "retrying $id with dwarf $(field "$T" "$id" dwarf)"
  export FORGE_FRACTAL_RETRY=1
  export FORGE_DATASET_TRIGGER=human
  do_task "$PLAN" "$id"
  st="$(task_status "$PLAN" "$id")"
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
  verify_result "$PLAN" "$(cat "$PLAN/wt_root")/_integration" "$PLAN" || { write_results "$PLAN"; run_report_extras "$PLAN"; return 5; }
  write_results "$PLAN"
  echo
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  run_report_extras "$PLAN"
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

verify_result() ( verify_result_body "$@" ) # plan, checkout, artifact directory

# The body is a function of its own so `run` can start it as `( verify_result_body ... ) &`:
# that one subshell is the background job, the home of the signal handler above and of
# forge_metric_begin's EXIT trap.
verify_result_body() { # plan, checkout, artifact directory
  local plan="$1" wt="$2" out="$3" cmd="" before after rc=0 repo jev_loaded=0 vretries
  FORGE_VERIFY_STOP=0 FORGE_VERIFY_GROUP="" FORGE_VERIFY_TIMER="" FORGE_VERIFY_GRACE=5
  [ "${FORGE_RUN_STOPPABLE:-0}" = 1 ] && trap verify_signal TERM INT
  mkdir -p "$out"
  forge_metric_begin "$out" verification
  forge_metric_phase verification
  repo="$(cat "$plan/repo")"
  rm -f "$out/verification.flaky" "$out/verification.retry.log" "$out/verification.coverage.txt" \
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
  local vsource; vsource="$(verify_source_for "$plan" "$repo")"
  case "$vsource" in command:*) cmd="$(cat "${vsource#*$'\t'}")" ;; esac
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
  if [ -z "$cmd" ] && [ "$jev_loaded" = 1 ] && forge_decide_active verify-discover; then
    local jev_json jev_rc jev_cmd
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
      # Loud on purpose: an unreviewed command is about to run on the user's behalf,
      # and they must never discover that later from a log instead of right here.
      note "jev chose a verification command (no --verify or .forge/verify configured): $cmd"
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
    local flaked=0 n_retry=0 rc3 b3 a3
    # The rerun itself is the evidence: it needs no model and no recorded trap. A tree
    # change is never retried.
    if [ "$rc" != 0 ] && [ "$before" = "$after" ] && [ "$vretries" -gt 0 ]; then
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
    # The integration worktree is where a merged result gets validated, so it needs
    # the same setup the task worktrees get. Without it, the one checkout holding
    # every task's merged work is the one checkout that cannot build or test it.
    mkdir -p "$PLAN/tasks/_integration"
    run_worktree_setup "$PLAN" "$REPO" "$int_wt" "$PLAN/tasks/_integration" _integration
  fi

  if [ -s "$PLAN/accepted.integration" ] && [ "$(git -C "$int_wt" rev-parse HEAD)" != "$(cat "$PLAN/accepted.integration")" ]; then
    die "integration branch changed outside this run" 3
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
      (cd "$REPO" && git worktree remove --force "$wt_root/$id" >/dev/null 2>&1)
    done
  fi

  echo
  echo "results (integration branch: $int_br)"
  echo "artifacts: $PLAN/tasks/<id>/{dwarf,qa}.{last,log}; results: $PLAN/results.tsv"
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  echo
  run_report_extras "$PLAN"

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
  # The retry hint is for tasks that actually failed; a run that only failed verification,
  # or only paused on infrastructure, has nothing to retry task by task. An interrupted task
  # has its own hint above.
  if [ "$(awk -F'\t' '$2!="MERGED" && $2!="INFRA" && $2!="PENDING" && $2!="BLOCKED" && $2!="INTERRUPTED" {n++} END {print n+0}' "$PLAN/results.tsv")" -gt 0 ]; then
    echo "retry a failed task with its reviewer's findings:"
    echo "  forge-parallel.sh retry $PLAN <task-id> [--dwarf <spec>]"
    echo "or let a run retry them itself:  forge-parallel.sh run $PLAN --retry-failed 1"
    echo
  fi
  if [ "$failed" = 1 ] && [ "$paused" = 0 ] && [ "$interrupted" = 0 ] && [ "$(cat "$PLAN/verification.status" 2>/dev/null)" = FAIL ]; then
    echo "combined verification failed: see $PLAN/verification.log"
    echo
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
  echo "merged candidate of $int_br into $cur (verification: $(cat "$out/verification.status"))"
}

# --- main ---------------------------------------------------------------------
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  awk '/^# Usage:/ {on=1} /^set -uo pipefail/ {on=0} on {sub(/^# ?/, ""); print}' "$SELF"
  forge_fractal_help
  exit 0
fi
[ $# -ge 1 ] || die "usage: forge-parallel.sh <plan|run|retry|accept|review|integrate|split|combine> <plan-dir> [...]"
CMD="$1"; shift
[ ! -f "${1:-}/no_ripwire" ] || export FORGE_RIPWIRE=off
if [ -d "${1:-}" ]; then
  PLAN_PATH="$(cd "$1" && pwd)"; shift; set -- "$PLAN_PATH" "$@"
fi
case "$CMD" in
  # `combine` takes a plan dir that does not exist yet and locks its sources itself.
  run|retry|integrate|accept|review|split)
    if [ "${FORGE_PLAN_LOCK:-}" != "${1:-}" ]; then
      exec python3 "$SKILL_DIR/scripts/forge-schedule.py" lock "$SELF" "$CMD" "$@"
    fi ;;
esac
# Every user-facing subcommand leaves one record in the run dataset, whatever its exit
# path (die included). Installed after the lock re-exec, so it fires once, in the process
# that did the work; `_task` and `_merge` are parts of a `run` and are not recorded alone.
case "$CMD" in
  plan|run|retry|integrate|accept|review|split|combine)
    FORGE_DATASET_INVOCATION="inv-$(date +%s)-$$-$RANDOM"; export FORGE_DATASET_INVOCATION
    FORGE_DATASET_STARTED="$(date +%s)"
    FORGE_DATASET_ARGV=("$@")
    trap 'forge_dataset_rc=$?; trap - EXIT; forge_dataset_invocation "$CMD" "$forge_dataset_rc" "$FORGE_DATASET_STARTED" ${FORGE_DATASET_ARGV[@]+"${FORGE_DATASET_ARGV[@]}"}; exit "$forge_dataset_rc"' EXIT ;;
esac
# Subcommands implemented in the optional libraries fail clearly if the library is absent.
need_fn() { declare -F "$1" >/dev/null || die "'$CMD' is not available: $1 is missing" 3; }
case "$CMD" in
  plan)      do_plan "$@"; rc=$?; forge_update_notice; exit "$rc" ;;
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
    forge_update_notice; exit "$rc" ;;
  retry)     do_retry "$@" ;;
  integrate) do_integrate "$@" ;;
  accept)    need_fn do_accept;  do_accept "$@" ;;
  review)    need_fn do_review;  do_review "$@" ;;
  split)     need_fn do_split;   do_split "$@" ;;
  combine)   need_fn do_combine; do_combine "$@" ;;
  _task)     do_task "$@" ;;
  _merge)    merge_task "$@" ;;
  _prepare_retry) retry_for_scheduler "$@" ;;
  _qa_only)  qa_only_next "$@" ;;
  _waves)    compute_waves "$@" ;;
  _sweep)    sweep_plan "$@" ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
