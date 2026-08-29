#!/usr/bin/env bash
# forge-parallel.sh — decomposed forge runs: many dwarves working concurrently in
# isolated git worktrees, each task reviewed by its own QA pass, only passing work
# merged. Harness-agnostic (bash + git only), and bash 3.2 compatible because that
# is what /bin/bash is on macOS — hence xargs -P for concurrency rather than
# `wait -n`, and no associative arrays anywhere.
#
# Usage:
#   forge-parallel.sh plan <plan-dir> --repo <dir> [routing flags]
#   forge-parallel.sh run  <plan-dir> [--max-parallel N] [--yolo-dwarf] [--yolo-qa]
#                                     [--setup <command>] [--dry-run]
#   forge-parallel.sh retry <plan-dir> <task-id> [--dwarf <spec>] [--setup <command>]
#   forge-parallel.sh integrate <plan-dir> --approved
#   forge-parallel.sh _task <plan-dir> <task-id>     (internal; the xargs target)
#
# Routing flags (plan):
#   --dwarf <spec>          fallback for any difficulty tier not named below
#   --dwarf-high <spec>     spec (or comma-list, pooled) for high-difficulty tasks
#   --dwarf-medium <spec>
#   --dwarf-low <spec>
#   --qa, --qa-high, --qa-medium, --qa-low    identical grammar
#
# Exit codes: 0 ok | 2 usage/validation | 3 precondition (not a git repo, etc.)
#             5 some task failed QA | 6 integration conflict
set -uo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCH="$SKILL_DIR/scripts/forge-dispatch.sh"
MEMORY="$SKILL_DIR/scripts/forge-memory.sh"

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
  local a="$1" b="$2" w
  [ "$a" = "-" ] && return 1
  for w in $(printf '%s' "$a" | tr ',' ' '); do
    case " $b " in *" $w "*) return 0 ;; esac
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
  local PLANNER="" NOMEM=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)         REPO="${2:?}"; shift 2 ;;
      --planner)      PLANNER="${2:?}"; shift 2 ;;
      --no-memory)    NOMEM=1; shift ;;
      --dwarf)        D_ANY="${2:?}"; shift 2 ;;
      --dwarf-high)   D_HI="${2:?}"; shift 2 ;;
      --dwarf-medium) D_MD="${2:?}"; shift 2 ;;
      --dwarf-low)    D_LO="${2:?}"; shift 2 ;;
      --qa)           Q_ANY="${2:?}"; shift 2 ;;
      --qa-high)      Q_HI="${2:?}"; shift 2 ;;
      --qa-medium)    Q_MD="${2:?}"; shift 2 ;;
      --qa-low)       Q_LO="${2:?}"; shift 2 ;;
      *) die "plan: unknown option '$1'" ;;
    esac
  done
  [ -d "$PLAN" ] || die "plan dir '$PLAN' does not exist"
  local T="$PLAN/tasks.tsv"
  [ -r "$T" ] || die "no tasks.tsv in '$PLAN' — decompose the goal first"

  # repo: from flag on first plan, remembered afterwards
  if [ -n "$REPO" ]; then
    [ -d "$REPO" ] || die "--repo '$REPO' is not a directory"
    (cd "$REPO" && git rev-parse --git-dir >/dev/null 2>&1) \
      || die "'$REPO' is not a git repository. Decomposed runs need worktrees, and there is no honest fallback without them." 3
    (cd "$REPO" && pwd) > "$PLAN/repo"
  fi
  [ -s "$PLAN/repo" ] || die "plan needs --repo the first time"
  REPO="$(cat "$PLAN/repo")"
  [ -s "$PLAN/run_id" ] || basename "$PLAN" | sed 's/^forge-//' > "$PLAN/run_id"
  # Markers rather than exported variables: do_task runs as a separate process
  # under xargs and inherits nothing from here.
  [ "$NOMEM" = 1 ] && : > "$PLAN/no_memory"
  [ -n "$PLANNER" ] && printf '%s\n' "$PLANNER" > "$PLAN/planner"

  # Uncommitted work is invisible to every dwarf, because worktrees branch from a
  # commit. That is the likeliest way to get a confusingly wrong result, so it is
  # stated loudly rather than left for the user to deduce from an odd diff.
  if [ -n "$(cd "$REPO" && git status --porcelain 2>/dev/null)" ]; then
    note "WARNING: '$REPO' has uncommitted changes. Task worktrees branch from HEAD,"
    note "         so those edits will NOT be visible to any dwarf. Commit or stash first"
    note "         if the tasks depend on them."
  fi

  # --- validate + resolve routing ---
  local tmp="$PLAN/.tasks.tsv.new"; : > "$tmp"
  local hi_i=0 md_i=0 lo_i=0 an_i=0 unassigned=0
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
    fi
    # resolve qa
    if [ "$qa" = "-" ] || [ -z "$qa" ]; then
      case "$diff" in
        high)   [ -n "$Q_HI" ] && qa="$Q_HI" ;;
        medium) [ -n "$Q_MD" ] && qa="$Q_MD" ;;
        low)    [ -n "$Q_LO" ] && qa="$Q_LO" ;;
      esac
      if [ "$qa" = "-" ] || [ -z "$qa" ]; then
        if [ -n "$Q_ANY" ]; then qa="$Q_ANY"; else qa="opus"; fi
      fi
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$deps" "$diff" "$files" "$dw" "$qa" "$title" >> "$tmp"
  done < "$T"
  mv "$tmp" "$T"

  compute_waves "$PLAN"
  render_table "$PLAN"

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
  local PLAN="$1" T="$1/tasks.tsv" W="$1/waves.tsv"
  : > "$W"; : > "$PLAN/deferrals.txt"
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
          "$wave" "$id" >> "$PLAN/deferrals.txt"
      fi
      if [ "$ok" = 1 ]; then
        printf '%s\t%s\n' "$wave" "$id" >> "$W"
        wave_files="$wave_files $(printf '%s' "$files" | tr ',' ' ')"
        progressed=1
      else
        next_remaining="$next_remaining $id"
      fi
    done
    [ "$progressed" = 1 ] || die "dependency cycle or unsatisfiable deps among:$next_remaining"
    for id in $(awk -F'\t' -v w="$wave" '$1==w {print $2}' "$W"); do done_ids="$done_ids$id "; done
    remaining="$next_remaining"
  done
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
  echo
  local w id
  for w in $(awk -F'\t' '{print $1}' "$W" | sort -un); do
    local cnt; cnt="$(awk -F'\t' -v w="$w" '$1==w' "$W" | wc -l | tr -d ' ')"
    echo "wave $w ($cnt parallel)"
    printf '  %-14s %-7s %-24s %-14s %s\n' id diff dwarf qa files
    for id in $(awk -F'\t' -v w="$w" '$1==w {print $2}' "$W"); do
      printf '  %-14s %-7s %-24s %-14s %s\n' \
        "$id" "$(field "$T" "$id" difficulty)" "$(field "$T" "$id" dwarf)" \
        "$(field "$T" "$id" qa)" "$(field "$T" "$id" files)"
      # The approach is shown here because this gate is the only moment a human can
      # change the plan for free. After dispatch, changing it costs a whole run.
      if [ -s "$PLAN/tasks/$id/approach.md" ]; then
        sed 's/^/                 | /' "$PLAN/tasks/$id/approach.md"
      fi
    done
    echo
  done
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
      if [ "$id" = "$me" ]; then st="THIS ONE"; else st="$(task_status "$PLAN" "$id")"; fi
      printf '%-14s %-7s %-9s %s\n' "$id" "$(field "$T" "$id" difficulty)" "$st" "$(field "$T" "$id" files)"
    done
    echo
    echo "## Ground rules"
    echo "- Your branch already contains every MERGED task's work. Do not reimplement it."
    if [ "$role" = qa ]; then
      echo "- The diff below is only this task's work. Code from MERGED tasks is already in the"
      echo "  base and is NOT part of this review — do not report it as this task's bug."
    else
      echo "- Files owned by other tasks are off limits; another dwarf is editing them now."
      echo "- If your task genuinely needs a change in someone else's file, say so in your final"
      echo "  message instead of making it."
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

# Non-fatal by design. A setup step that fails leaves a worktree the dwarf can still
# work in — it just has to install things itself, which is the status quo this exists
# to improve on. Failing the task instead would turn a slow run into a dead one.
run_worktree_setup() { # run_worktree_setup <plan> <repo> <wt> <tdir> <id>
  local PLAN="$1" REPO="$2" wt="$3" tdir="$4" id="$5" cmd
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

do_task() {
  local PLAN="${1:?}" id="${2:?}"
  local T="$PLAN/tasks.tsv" REPO run_id base wt br tdir attempt
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  tdir="$PLAN/tasks/$id"; mkdir -p "$tdir"
  [ -f "$PLAN/no_memory" ] && export FORGE_MEMORY=off
  wt="$(cat "$PLAN/wt_root")/$id"
  br="forge/$run_id/$id"
  # Owned here rather than by `retry`, so a task re-dispatched by a resumed `run`
  # is also numbered — otherwise two different attempts share a commit message.
  attempt=$(( $(cat "$tdir/attempt" 2>/dev/null || echo 0) + 1 ))
  echo "$attempt" > "$tdir/attempt"

  # The base is pinned per task on first creation and reused by every retry. The
  # integration branch moves as later waves land, so re-reading the run-level
  # base_ref on a retry would pull other tasks' merged code into this task's diff
  # and put it in front of its reviewer as if this dwarf had written it.
  if [ -s "$tdir/base_ref" ]; then
    base="$(cat "$tdir/base_ref")"
  else
    base="$(cat "$PLAN/base_ref")"; printf '%s\n' "$base" > "$tdir/base_ref"
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

  # dwarf
  write_capsule "$PLAN" "$id" dwarf >/dev/null
  {
    cat "$tdir/capsule.md"; echo
    # Memory comes from the MAIN repo, not the worktree: on a repo's first forge
    # run .forge/ is not committed yet, so a worktree branched from the base
    # commit would not have it.
    /bin/bash "$MEMORY" inject "$REPO" dwarf
    echo; echo "---"; echo
    if [ -s "$tdir/approach.md" ]; then
      echo "## Intended approach"
      echo "Planned before the run, and the other tasks were planned to fit it. Follow it"
      echo "unless it is actually wrong — if it is, say so in your final message rather than"
      echo "silently doing something else, because a sibling task may depend on this shape."
      echo; cat "$tdir/approach.md"; echo; echo "---"; echo
    fi
    cat "$tdir/prompt.md"
    if [ -s "$tdir/retry_findings.md" ]; then
      echo; echo "---"; echo
      echo "## Your previous attempt was reviewed and rejected"
      echo "Your earlier work on this task is already in the working tree. Fix the findings"
      echo "below rather than starting over, and do not revert the parts that were not"
      echo "criticised. If a finding is wrong, say so in your final message instead of"
      echo "silently ignoring it."
      echo; cat "$tdir/retry_findings.md"
    fi
    echo; /bin/bash "$MEMORY" note dwarf
  } > "$tdir/dwarf.input"
  /bin/bash "$DISPATCH" dwarf "$dw" --repo "$wt" --run-dir "$tdir" \
        --prompt-file "$tdir/dwarf.input" $yd >"$tdir/dwarf.out" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" = 7 ]; then
      echo TIMEOUT > "$tdir/status"; note "$id: dwarf hit the dispatch timeout — see $tdir/dwarf.out"
    else
      echo ERROR > "$tdir/status"; note "$id: dwarf dispatch failed — see $tdir/dwarf.out"
    fi
    return 1
  fi

  /bin/bash "$MEMORY" record "$REPO" --last "$tdir/dwarf.last" --role dwarf \
      --run-id "$run_id" --task "$id" --model "$dw" \
      --duration "$(dispatch_duration "$tdir" dwarf)" >/dev/null 2>&1

  # Capture the real diff, including new files, then commit on the task branch.
  # Diffed against the task's pinned base rather than the branch head, so a retry
  # hands QA the task's CUMULATIVE work. Reviewing only the fix would let the
  # first attempt's code through unread.
  # .forge/ is forge's own memory, rewritten in the user's tree after every run.
  # Left in, it would reach QA as if a dwarf had written it, and any task whose
  # files touched it would collide with every other task in the wave planner.
  [ -s "$tdir/changes.diff" ] && mv "$tdir/changes.diff" "$tdir/changes.prev.diff"
  (cd "$wt" && git add -A -- . ":(exclude).forge" \
      && git diff --cached "$base" -- . ":(exclude).forge") > "$tdir/changes.diff" 2>/dev/null
  if [ ! -s "$tdir/changes.diff" ]; then
    echo FAIL > "$tdir/status"
    echo "The dwarf produced no changes at all." > "$tdir/qa.last"
    note "$id: dwarf produced an empty diff"; return 1
  fi
  if [ -s "$tdir/changes.prev.diff" ] && cmp -s "$tdir/changes.diff" "$tdir/changes.prev.diff"; then
    echo FAIL > "$tdir/status"
    echo "The retry changed nothing: the diff is byte-identical to the previous attempt." \
      > "$tdir/qa.last"
    note "$id: retry produced no new changes — not re-reviewing identical code"; return 1
  fi

  check_drift "$(field "$T" "$id" files)" "$tdir" "$wt" "$base"
  if [ -s "$tdir/drift.txt" ]; then
    note "$id: touched undeclared file(s): $(tr '\n' ' ' < "$tdir/drift.txt")"
  fi

  (cd "$wt" && git -c user.name=forge -c user.email=forge@local \
      commit -q -m "forge($id) attempt $attempt: $(field "$T" "$id" title)") >/dev/null 2>&1

  # qa
  write_capsule "$PLAN" "$id" qa >/dev/null
  {
    cat "$tdir/capsule.md"; echo
    /bin/bash "$MEMORY" inject "$REPO" qa
    echo; echo "---"; echo
    if [ -s "$tdir/approach.md" ]; then
      echo "## Intended approach"
      echo "This is what the implementer was asked to build, not just what it was asked to"
      echo "achieve. Code that works but abandons this shape is a finding: a sibling task may"
      echo "have been planned against it."
      echo; cat "$tdir/approach.md"; echo
    fi
    echo "The implementer was asked to do the task above. Review the diff below for correctness"
    echo "bugs: logic errors, broken edge cases, behaviour that does not match what was asked."
    echo "Also say if it solved a different problem, or touched files outside the task's scope."
    echo
    if [ -s "$tdir/drift.txt" ]; then
      # The reviewer is already asked about scope and had no way to know the answer.
      echo "Scope note: this task declared it would touch $(field "$T" "$id" files), and the"
      echo "diff also changes files it did not declare:"
      sed 's/^/  - /' "$tdir/drift.txt"
      echo "That is not automatically wrong — judge whether each was actually necessary. It"
      echo "matters because another task may own those files."
      echo
    fi
    echo "Report findings only — do not edit any file. For each finding give the file and line,"
    echo "what breaks, and a concrete input that triggers it. Mark each CONFIRMED if you traced"
    echo "it in the code, or PLAUSIBLE if you could not fully verify it."
    echo
    echo "End your reply with exactly one line:"
    echo "  FORGE_VERDICT: PASS   — no confirmed correctness bug (style nits are not failures)"
    echo "  FORGE_VERDICT: FAIL   — at least one CONFIRMED correctness bug"
    echo
    echo '```diff'; cat "$tdir/changes.diff"; echo '```'
    echo; /bin/bash "$MEMORY" note qa
  } > "$tdir/qa.input"
  /bin/bash "$DISPATCH" qa "$qa" --repo "$wt" --run-dir "$tdir" \
        --prompt-file "$tdir/qa.input" $yq >"$tdir/qa.out" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" = 7 ]; then
      echo TIMEOUT > "$tdir/status"; note "$id: qa hit the dispatch timeout — see $tdir/qa.out"
    else
      echo ERROR > "$tdir/status"; note "$id: qa dispatch failed — see $tdir/qa.out"
    fi
    return 1
  fi

  local verdict
  verdict="$(grep -o 'FORGE_VERDICT: *[A-Za-z]*' "$tdir/qa.last" 2>/dev/null | tail -1 | awk '{print $2}')"
  /bin/bash "$MEMORY" record "$REPO" --last "$tdir/qa.last" --role qa \
      --run-id "$run_id" --task "$id" --model "$qa" --verdict "${verdict:-UNKNOWN}" \
      --duration "$(dispatch_duration "$tdir" qa)" >/dev/null 2>&1

  case "$verdict" in
    PASS) echo PASS > "$tdir/status" ;;
    FAIL) echo FAIL > "$tdir/status" ;;
    # No verdict means the reviewer never reached a conclusion. Treating that as a
    # pass would merge unreviewed code, so it is fail-safe by construction.
    *)    echo UNKNOWN > "$tdir/status" ;;
  esac
  return 0
}

# Merge one passing task onto the integration branch. Shared by `run` and `retry`
# so a retried task lands exactly the way a first-attempt task does.
merge_task() { # merge_task <plan> <id> -> 0 merged, 1 conflicted
  local PLAN="$1" id="$2" REPO run_id int_wt
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  int_wt="$(cat "$PLAN/wt_root")/_integration"
  if (cd "$int_wt" && git merge --no-ff -m "forge: merge $id" "forge/$run_id/$id" >/dev/null 2>&1); then
    touch "$PLAN/tasks/$id/merged"
    return 0
  fi
  (cd "$int_wt" && git merge --abort >/dev/null 2>&1)
  echo CONFLICT > "$PLAN/tasks/$id/status"
  return 1
}

# --- retry --------------------------------------------------------------------
# A failed task keeps its branch and worktree "so there is something to inspect and
# retry from" — this is the retry. It is a command a human types, not an automatic
# repair loop, which is what keeps forge's one-dwarf-then-QA rule intact.
do_retry() {
  local PLAN="${1:?retry needs a plan dir}" id="${2:?retry needs a task id}"; shift 2
  local NEWDWARF=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --dwarf) NEWDWARF="${2:?--dwarf needs a spec}"; shift 2 ;;
      --yolo-dwarf) touch "$PLAN/yolo_dwarf"; shift ;;
      --yolo-qa)    touch "$PLAN/yolo_qa"; shift ;;
      --setup)      printf '%s' "${2:?--setup needs a command}" > "$PLAN/setup_cmd"; shift 2 ;;
      *) die "retry: unknown option '$1'" ;;
    esac
  done
  local T="$PLAN/tasks.tsv" tdir="$PLAN/tasks/$id"
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'"
  all_ids "$T" | grep -qx "$id" || die "no task '$id' in this plan"
  [ -s "$PLAN/wt_root" ] || die "this plan has never been run — use 'run', not 'retry'"
  local st; st="$(task_status "$PLAN" "$id")"
  [ "$st" = "MERGED" ] && die "task '$id' already merged — nothing to retry"

  # The findings are the whole point of retrying rather than re-running: the dwarf
  # gets told what was wrong with its own previous attempt.
  # Only a real review replaces the findings. The runner also writes explanatory
  # text into qa.last when it fails a task without dispatching QA ("produced no
  # changes at all"), and copying that over would throw away the actual findings
  # the previous reviewer gave — the one thing a retry exists to carry forward.
  if grep -q 'FORGE_VERDICT' "$tdir/qa.last" 2>/dev/null; then
    cp "$tdir/qa.last" "$tdir/retry_findings.md"
  elif [ -s "$tdir/retry_findings.md" ]; then
    note "$id: no new review since the last retry — reusing the previous findings"
  else
    note "$id: no previous QA findings to pass on (last status was $st)"
    : > "$tdir/retry_findings.md"
  fi
  if [ -n "$NEWDWARF" ]; then
    # Written into tasks.tsv rather than kept in a side file: the table then shows
    # the model that will actually be spent, and plan's rule that an explicit
    # column value survives a re-plan protects the escalation for free.
    awk -F'\t' -v OFS='\t' -v id="$id" -v dw="$NEWDWARF" \
      '$0 !~ /^#/ && $1==id { $5=dw } { print }' "$T" > "$PLAN/.tasks.tsv.retry" \
      && mv "$PLAN/.tasks.tsv.retry" "$T"
  fi
  rm -f "$tdir/status"

  note "retrying $id with dwarf $(field "$T" "$id" dwarf)"
  do_task "$PLAN" "$id"
  st="$(task_status "$PLAN" "$id")"
  if [ "$st" = "PASS" ]; then
    if merge_task "$PLAN" "$id"; then
      note "$id PASS -> merged"
    else
      note "$id PASS but conflicted on merge — branch preserved"
    fi
  fi
  write_results "$PLAN"
  echo
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  [ "$(task_status "$PLAN" "$id")" = "MERGED" ] || return 5
  return 0
}

write_results() { # write_results <plan>
  local PLAN="$1" T="$1/tasks.tsv" run_id id
  run_id="$(cat "$PLAN/run_id")"
  : > "$PLAN/results.tsv"
  for id in $(all_ids "$T"); do
    printf '%s\t%s\tforge/%s/%s\n' "$id" "$(task_status "$PLAN" "$id")" "$run_id" "$id" >> "$PLAN/results.tsv"
  done
}

# --- run ----------------------------------------------------------------------
do_run() {
  local PLAN="${1:?run needs a plan dir}"; shift
  local MAXP=3 DRY=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --max-parallel) MAXP="${2:?}"; shift 2 ;;
      --yolo-dwarf)   touch "$PLAN/yolo_dwarf"; shift ;;
      --yolo-qa)      touch "$PLAN/yolo_qa"; shift ;;
      --setup)        printf '%s' "${2:?--setup needs a command}" > "$PLAN/setup_cmd"; shift 2 ;;
      --dry-run)      DRY=1; shift ;;
      *) die "run: unknown option '$1'" ;;
    esac
  done
  local T="$PLAN/tasks.tsv" W="$PLAN/waves.tsv"
  [ -s "$W" ] || die "no waves.tsv — run the plan subcommand first"
  grep -q 'UNASSIGNED' "$T" && die "some tasks are UNASSIGNED — assign a dwarf before running"

  local REPO run_id wt_root int_wt int_br
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
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

  mkdir -p "$wt_root"
  # A branch named exactly forge/<run-id> would occupy refs/heads/forge/<run-id> as a
  # file and make every task branch below it impossible to create. Catch it here with
  # an actionable message rather than letting all N tasks fail identically at dispatch.
  if (cd "$REPO" && git rev-parse --verify --quiet "forge/$run_id" >/dev/null 2>&1); then
    die "branch 'forge/$run_id' blocks task branches 'forge/$run_id/<task>' (git refs are files, so a ref cannot also be a directory). Delete or rename it: git branch -D forge/$run_id" 3
  fi
  if ! (cd "$REPO" && git rev-parse --verify "$int_br" >/dev/null 2>&1); then
    (cd "$REPO" && git branch "$int_br" HEAD) || die "could not create $int_br" 3
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

  local w id st failed=0 todo skipped
  for w in $(awk -F'\t' '{print $1}' "$W" | sort -un); do
    # Every wave branches from the integration branch as it currently stands, so a
    # dependent task sees its dependencies' merged code.
    (cd "$int_wt" && git rev-parse HEAD) > "$PLAN/base_ref"
    # Already-merged tasks are skipped rather than re-dispatched, which is what
    # makes an interrupted run continuable: the work that landed stays landed and
    # nobody's quota is spent twice on it.
    todo=""; skipped=0
    for id in $(awk -F'\t' -v w="$w" '$1==w {print $2}' "$W"); do
      if [ "$(task_status "$PLAN" "$id")" = "MERGED" ]; then
        skipped=$((skipped+1))
      else
        todo="$todo $id"
      fi
    done
    if [ -z "${todo// /}" ]; then
      note "wave $w: all $skipped task(s) already merged — skipping"
      continue
    fi
    [ "$skipped" -gt 0 ] && note "wave $w: $skipped task(s) already merged, resuming the rest"
    note "wave $w: dispatching $(printf '%s\n' $todo | wc -l | tr -d ' ') task(s), max $MAXP in parallel"
    printf '%s\n' $todo \
      | xargs -P "$MAXP" -I{} /bin/bash "$SELF" _task "$PLAN" {}
    for id in $todo; do
      st="$(task_status "$PLAN" "$id")"
      if [ "$st" = "PASS" ]; then
        if merge_task "$PLAN" "$id"; then
          note "wave $w: $id PASS -> merged"
        else
          note "wave $w: $id PASS but conflicted on merge — branch preserved"
          failed=1
        fi
      else
        note "wave $w: $id $st — excluded, branch forge/$run_id/$id preserved"
        failed=1
      fi
    done
  done

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
  printf '  %-14s %-9s %s\n' id status branch
  awk -F'\t' '{printf "  %-14s %-9s %s\n", $1, $2, $3}' "$PLAN/results.tsv"
  echo

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

  if [ "$failed" = 1 ]; then
    echo "retry a failed task with its reviewer's findings:"
    echo "  forge-parallel.sh retry $PLAN <task-id> [--dwarf <spec>]"
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
  [ "$failed" = 1 ] && return 5
  return 0
}

# --- integrate ----------------------------------------------------------------
do_integrate() {
  local PLAN="${1:?}"; shift
  local APPROVED=0
  while [ $# -gt 0 ]; do
    case "$1" in --approved) APPROVED=1; shift ;; *) die "integrate: unknown option '$1'" ;; esac
  done
  [ "$APPROVED" = 1 ] || die "integrate needs --approved: it merges forge's work into YOUR current branch"
  local REPO run_id int_br cur
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"; int_br="forge/$run_id-integration"
  cur="$(cd "$REPO" && git rev-parse --abbrev-ref HEAD)"
  [ "$cur" = "$int_br" ] && die "you are already on $int_br"
  [ -n "$(cd "$REPO" && git status --porcelain)" ] && die "working tree is dirty — commit or stash before integrating"
  if (cd "$REPO" && git merge --no-ff -m "forge: integrate run $run_id" "$int_br"); then
    echo "merged $int_br into $cur"
  else
    note "merge conflicted; resolve or 'git merge --abort'. $int_br is unchanged."
    return 6
  fi
}

# --- main ---------------------------------------------------------------------
[ $# -ge 1 ] || die "usage: forge-parallel.sh <plan|run|retry|integrate|_task> <plan-dir> [...]"
CMD="$1"; shift
case "$CMD" in
  plan)      do_plan "$@" ;;
  run)       do_run "$@" ;;
  retry)     do_retry "$@" ;;
  integrate) do_integrate "$@" ;;
  _task)     do_task "$@" ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
