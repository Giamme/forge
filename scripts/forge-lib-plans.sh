#!/usr/bin/env bash
# Plan operations for forge-parallel.sh: `accept`, `combine` and `split`.
#
# Sourced by forge-parallel.sh, which provides die/note/field/all_ids/task_status,
# merge_task, compute_waves, write_results, record_attempt, run_worktree_setup,
# forge_fingerprint and SELF. Functions only; nothing runs at source time.
#
#   accept  <plan> <task> --reason "<why>" --approved      (runs under the plan's lock)
#   split   <plan> <new-plan> --tasks a,b [--dry-run]      (runs under the plan's lock)
#   combine <new-plan> <plan-a> <plan-b>... [--verify CMD] [--dry-run]
#
# Exit codes follow forge-parallel.sh: 0 ok, 2 usage, 3 precondition (nothing was
# changed), 6 integration conflict (anything created was removed again).
#
# `combine` and `split` only ever move git refs and worktrees that forge itself made
# (forge/<run>-integration, forge/<run>/<task>, <wt_root>/...). Nothing they do touches
# the user's branches or working tree.

# --- shared helpers -------------------------------------------------------------
plans_abs() { # absolute path of a directory that may not exist yet
  if [ -d "$1" ]; then (cd "$1" && pwd)
  else python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
  fi
}

plans_dir_empty() { # plans_dir_empty <dir>: true when it holds nothing at all
  local f
  for f in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    if [ -e "$f" ] || [ -L "$f" ]; then return 1; fi
  done
  return 0
}

plans_run_id() { basename "$1" | sed 's/^forge-//'; }

plans_short() { printf '%s' "${1:0:7}"; }

# What `run` would call this plan's worktree root: beside the repository, never in TMPDIR.
plans_wt_root_for() { # plans_wt_root_for <repo path as written in a plan> <run id>
  printf '%s/.forge-worktrees/%s' "$(cd "$1/.." && pwd)" "$2"
}

# Refuses (exit 3, nothing touched) unless <new-plan> and its git names are free to use.
plans_target_checks() { # plans_target_checks <repo> <new-plan> <run id> <wt root>
  local repo="$1" new="$2" rid="$3" wtr="$4"
  [ -n "$rid" ] || die "'$new' has no usable name: its run id is the directory name minus a leading 'forge-'" 3
  git check-ref-format "refs/heads/forge/$rid-integration" >/dev/null 2>&1 &&
    git check-ref-format "refs/heads/forge/$rid/x" >/dev/null 2>&1 ||
    die "'$rid' (from '$new') cannot be part of a branch name; use a plan directory name of plain letters, digits, '-' and '_'" 3
  if [ -e "$new" ] || [ -L "$new" ]; then
    [ -d "$new" ] || die "'$new' exists and is not a directory" 3
    plans_dir_empty "$new" || die "'$new' already exists and is not empty; pick a new plan directory" 3
  fi
  if git -C "$repo" show-ref --verify --quiet "refs/heads/forge/$rid-integration"; then
    die "branch forge/$rid-integration already exists; pick a different plan directory name" 3
  fi
  if git -C "$repo" show-ref --verify --quiet "refs/heads/forge/$rid"; then
    die "branch forge/$rid would block the task branches forge/$rid/<task> (git refs are files); pick a different plan directory name" 3
  fi
  if [ -n "$(git -C "$repo" for-each-ref --count=1 --format=x "refs/heads/forge/$rid/" 2>/dev/null)" ]; then
    die "branches forge/$rid/... already exist; pick a different plan directory name" 3
  fi
  if [ -e "$wtr" ]; then
    plans_dir_empty "$wtr" || die "worktree root '$wtr' already exists; pick a different plan directory name" 3
  fi
  return 0
}

# Copies a task's artifact directory, minus review snapshots (review-<attempt>-<pid>/ is a
# full export of the reviewed tree, rebuilt by every review, and nothing reads it back).
plans_copy_taskdir() { # plans_copy_taskdir <src dir> <dst dir>
  local src="$1" dst="$2" f
  mkdir -p "$dst" || return 1
  for f in "$src"/* "$src"/.[!.]*; do
    if [ -e "$f" ] || [ -L "$f" ]; then
      case "$(basename "$f")" in review-*) continue ;; esac
      cp -pR "$f" "$dst/" || return 1
    fi
  done
  return 0
}

# The state `undo` must reverse. Globals, so an interrupt trap can reach them.
plans_reset_undo() {
  PLANS_U_REPO=""; PLANS_U_BR=""; PLANS_U_WT=""; PLANS_U_WTR=""; PLANS_U_WTR_NEW=0
  PLANS_U_PLAN=""; PLANS_U_PLAN_NEW=0; PLANS_U_JOURNAL=""
}

# Reverses everything combine/split created, newest first. Best effort and silent: it runs
# on the way out of a failure that has already been reported.
plans_undo() {
  local kind a b line
  trap - INT TERM
  while IFS="$(printf '\t')" read -r kind a b; do
    case "$kind" in
      head)   git -C "$a" symbolic-ref HEAD "refs/heads/$b" >/dev/null 2>&1 ;;
      move)   git -C "$PLANS_U_REPO" worktree move "$b" "$a" >/dev/null 2>&1 ;;
      branch) git -C "$PLANS_U_REPO" branch -D "$a" >/dev/null 2>&1 ;;
    esac
  done <<EOF
$PLANS_U_JOURNAL
EOF
  if [ -n "$PLANS_U_WT" ]; then
    git -C "$PLANS_U_REPO" worktree remove --force "$PLANS_U_WT" >/dev/null 2>&1
    rm -rf "$PLANS_U_WT"
  fi
  [ -z "$PLANS_U_BR" ] || git -C "$PLANS_U_REPO" branch -D "$PLANS_U_BR" >/dev/null 2>&1
  if [ "$PLANS_U_WTR_NEW" = 1 ] && [ -n "$PLANS_U_WTR" ]; then rm -rf "$PLANS_U_WTR"; fi
  if [ -n "$PLANS_U_PLAN" ]; then
    if [ "$PLANS_U_PLAN_NEW" = 1 ]; then rm -rf "$PLANS_U_PLAN"
    else rm -rf "$PLANS_U_PLAN"/* "$PLANS_U_PLAN"/.[!.]* 2>/dev/null
    fi
  fi
  [ -z "$PLANS_U_REPO" ] || git -C "$PLANS_U_REPO" worktree prune >/dev/null 2>&1
  return 0
}

plans_journal() { # plans_journal <kind> <a> <b>: newest entry first
  PLANS_U_JOURNAL="$1$(printf '\t')$2$(printf '\t')$3
$PLANS_U_JOURNAL"
}

# Creates <branch> at <commit> with a worktree at <wt>, both recorded for undo.
plans_make_integration() { # plans_make_integration <repo> <branch> <commit> <wt>
  git -C "$1" branch "$2" "$3" >/dev/null 2>&1 || return 1
  PLANS_U_BR="$2"
  git -C "$1" worktree add "$4" "$2" >/dev/null 2>&1 || return 1
  PLANS_U_WT="$4"
}

# Per-plan option files carried to a derived plan. The cost-bearing choices a user made
# for this plan apply to the plan made from it; `planner` does not (nothing plans again).
PLANS_OPTION_FILES="no_ripwire no_memory yolo_dwarf yolo_qa qa_threshold timeout verify_retries verify_cmd setup_cmd final_qa fractal-routing.json fractal-selection.json jev-selection.json"

# --- accept ---------------------------------------------------------------------
# A reviewer's FAIL is the QA gate; overriding it is the user's call and never forge's or
# the orchestrator's, hence both --reason and --approved. Only work the reviewer actually
# judged can be accepted: the branch tip and the worktree must still be exactly what was
# reviewed, or the human would be signing off on code nobody reviewed.
plans_why_not() { # plans_why_not <plan> <id> <status>: exit 3 with advice for a status accept cannot take
  local plan="$1" id="$2" st="$3"
  case "$st" in
    MERGED) die "task '$id' is already merged — nothing to accept" 3 ;;
    PASS) die "task '$id' already passed review; 'forge-parallel.sh run $plan' merges it — nothing to accept" 3 ;;
    INVALIDATED) die "task '$id' is INVALIDATED: its work or the integration branch changed after the review, so the review no longer describes it. Use 'forge-parallel.sh retry $plan $id' for a fresh review" 3 ;;
    CONFLICT) die "task '$id' is CONFLICT: its reviewed work does not merge cleanly, so accepting it cannot help. Resolve the conflict or 'forge-parallel.sh retry $plan $id'" 3 ;;
    ERROR|TIMEOUT|INFRA|INTERRUPTED) die "task '$id' is $st: no reviewer verdict exists to override. Use 'forge-parallel.sh retry $plan $id' (or 'run')" 3 ;;
    BLOCKED|PENDING) die "task '$id' is $st: it has not been reviewed yet, so there is nothing to override" 3 ;;
    RUNNING) die "task '$id' is RUNNING (or a run was killed): re-run 'forge-parallel.sh run $plan' so it settles first" 3 ;;
    *) die "task '$id' has status '$st'; only a FAIL or UNKNOWN task can be accepted" 3 ;;
  esac
}

# Prints the reviewer's text for known_issues.md: the reply minus the verdict line, and,
# when the last attempt was a no-progress retry whose qa.last is just forge's explanation,
# the real review that retry carried forward.
plans_reviewer_text() { # plans_reviewer_text <tdir> <status>
  local tdir="$1" st="$2"
  if [ -s "$tdir/qa.last" ]; then tr -d '\r' < "$tdir/qa.last" | grep -v '^FORGE_VERDICT:'; fi
  if [ "$st" = FAIL ] && [ -s "$tdir/retry_findings.md" ] && ! grep -q '^FORGE_VERDICT:' "$tdir/qa.last" 2>/dev/null; then
    echo
    echo "Earlier review of the same work (retry_findings.md):"
    echo
    tr -d '\r' < "$tdir/retry_findings.md" | grep -v '^FORGE_VERDICT:'
  fi
  return 0
}

do_accept() {
  local PLAN="${1:?accept needs a plan dir}" id="" reason="" approved=0
  shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --reason) [ $# -ge 2 ] || die "accept: --reason needs a text"; reason="$2"; shift 2 ;;
      --approved) approved=1; shift ;;
      -*) die "accept: unknown option '$1'" ;;
      *) [ -z "$id" ] || die "accept: unexpected argument '$1'"; id="$1"; shift ;;
    esac
  done
  [ -n "$id" ] || die "usage: forge-parallel.sh accept <plan-dir> <task-id> --reason \"<why>\" --approved"
  reason="$(printf '%s' "$reason" | tr '\n\r\t' '   ' | sed 's/^ *//; s/ *$//')"
  [ -n "$reason" ] || die "accept needs --reason \"<why this work is acceptable despite the reviewer>\": it is recorded with the work"
  [ "$approved" = 1 ] || die "accept needs --approved: it overrides the reviewer's verdict and is only run on the user's explicit instruction"

  local T="$PLAN/tasks.tsv" tdir="$PLAN/tasks/$id" REPO run_id wt br int_wt st dep dst
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'"
  all_ids "$T" | grep -qx "$id" || die "no task '$id' in this plan"
  [ -s "$PLAN/wt_root" ] && [ -s "$PLAN/run_id" ] && [ -s "$PLAN/repo" ] \
    || die "this plan has never been run, so there is no reviewed work to accept" 3
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  wt="$(cat "$PLAN/wt_root")/$id"; br="forge/$run_id/$id"
  int_wt="$(cat "$PLAN/wt_root")/_integration"

  st="$(task_status "$PLAN" "$id")"
  case "$st" in FAIL|UNKNOWN) ;; *) plans_why_not "$PLAN" "$id" "$st" ;; esac

  # The reviewed work must be exactly what is on disk: same commit on the branch, same
  # tree, index and HEAD in the worktree as when QA finished.
  [ -s "$tdir/reviewed.commit" ] && [ -s "$tdir/source.fingerprint" ] \
    || die "task '$id' has no saved review state (it never reached a reviewed commit); use 'forge-parallel.sh retry $PLAN $id'" 3
  [ -e "$wt/.git" ] || die "task '$id': its worktree $wt is gone, so the reviewed work cannot be verified; use 'forge-parallel.sh retry $PLAN $id'" 3
  [ "$(git -C "$REPO" rev-parse --verify --quiet "refs/heads/$br" 2>/dev/null)" = "$(cat "$tdir/reviewed.commit")" ] \
    || die "task '$id': branch $br is no longer at the commit that was reviewed — the work changed since review. Use 'forge-parallel.sh retry $PLAN $id' for a fresh review" 3
  [ "$(forge_fingerprint "$wt" 2>/dev/null)" = "$(cat "$tdir/source.fingerprint")" ] \
    || die "task '$id': the worktree $wt changed since it was reviewed (edited, staged or committed). Accepting would sign off on code no reviewer saw; use 'forge-parallel.sh retry $PLAN $id' for a fresh review" 3

  # Checked here, not left to merge_task: it records INVALIDATED/BLOCKED on a task it
  # refuses, and a refused accept must leave the task exactly as it was.
  for dep in $(field "$T" "$id" deps | tr ',' ' '); do
    [ "$dep" = - ] && continue
    dst="$(task_status "$PLAN" "$dep")"
    [ "$dst" = MERGED ] || die "task '$id' depends on '$dep', which is not merged (status $dst); merge or accept '$dep' first" 3
  done
  [ -e "$int_wt/.git" ] || die "the integration worktree $int_wt is missing" 3
  [ -s "$PLAN/accepted.integration" ] && [ "$(git -C "$int_wt" rev-parse HEAD 2>/dev/null)" = "$(cat "$PLAN/accepted.integration")" ] \
    || die "integration branch changed outside this run; refusing to merge onto it" 3
  [ -z "$(git -C "$int_wt" status --porcelain -- . ':(exclude).forge' 2>/dev/null)" ] \
    || die "the integration worktree $int_wt has uncommitted changes; refusing to merge onto it" 3

  # Record first, merge second, and put it all back if the merge does not happen.
  local ki="$tdir/known_issues.md" kibak="$tdir/.known_issues.before" tmp="$tdir/.known_issues.new"
  local who="${USER:-${LOGNAME:-unknown}}" when body n sha attempt
  when="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  attempt="$(cat "$tdir/attempt" 2>/dev/null || echo 0)"
  sha="$(cat "$tdir/reviewed.commit")"
  body="$(plans_reviewer_text "$tdir" "$st")"
  n="$(printf '%s\n' "$body" | grep -c '^- ')"
  rm -f "$kibak"; [ -f "$ki" ] && cp -p "$ki" "$kibak"
  {
    if [ -s "$ki" ]; then cat "$ki"; echo; fi
    echo "## Accepted by a human despite the reviewer's $st verdict"
    echo
    echo "Reason: $reason"
    echo "Accepted by $who on $when (task $id, attempt $attempt, reviewed commit ${sha:0:12})."
    echo "The reviewer's reply follows, copied from tasks/$id/qa.last. None of it was fixed."
    echo
    [ -z "$body" ] || printf '%s\n' "$body"
    if [ "$n" = 0 ]; then
      echo
      echo "- [$st] the reviewer left no itemised findings; read tasks/$id/qa.last"
    fi
  } > "$tmp" && mv "$tmp" "$ki" || { rm -f "$tmp"; die "could not write $ki" 3; }
  {
    echo "task=$id"
    echo "status=$st"
    echo "reason=$reason"
    echo "by=$who"
    echo "at=$when"
    echo "epoch=$(date +%s)"
    echo "attempt=$attempt"
    echo "reviewed_commit=$sha"
  } > "$tdir/accepted.by"
  echo PASS > "$tdir/status"

  if ! merge_task "$PLAN" "$id"; then
    # merge_task already wrote CONFLICT or INVALIDATED. The task was not accepted, so the
    # acceptance record goes too; a stale known_issues.md would be listed as accepted work.
    rm -f "$tdir/accepted.by" "$ki"
    [ -f "$kibak" ] && mv "$kibak" "$ki"
    write_results "$PLAN"
    die "task '$id' could not be merged (status now $(task_status "$PLAN" "$id")); nothing was accepted. See 'git -C $int_wt status' and the task branch $br" 6
  fi
  rm -f "$kibak"
  record_attempt "$PLAN" "$id"
  write_results "$PLAN"
  echo "$id accepted by a human and merged"
  echo "known issues recorded in $ki"
  if [ "$(awk -F'\t' '$2!="MERGED" {n++} END {print n+0}' "$PLAN/results.tsv")" -gt 0 ]; then
    echo "tasks that were waiting on it are unchanged; continue with: forge-parallel.sh run $PLAN"
  fi
  return 0
}

# --- combine --------------------------------------------------------------------
# Every source plan is locked for the whole operation (a concurrent run would move the
# very integration branch being merged). The lock is taken by re-exec, like the router's
# own lock for run/retry/integrate, so the file descriptors stay open for bash.
PLANS_LOCK_PY='
import fcntl, os, sys
script, count = sys.argv[1], int(sys.argv[2])
held = []
for path in sys.argv[3:3 + count]:
    try:
        handle = open(path, "a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        sys.stderr.write("forge: plan %s is locked by another forge command (%s); wait for it to finish\n"
                         % (os.path.dirname(path), error))
        sys.exit(3)
    os.set_inheritable(handle.fileno(), True)
    held.append(handle)
env = dict(os.environ, FORGE_COMBINE_LOCKED="1")
os.execve("/bin/bash", ["/bin/bash", script, "combine"] + sys.argv[3 + count:], env)
'

plans_predict_conflict() { # 0 merges cleanly, 1 would conflict, 2 cannot tell (old git)
  git -C "$1" merge-tree --write-tree "$2" "$3" >/dev/null 2>&1
  case "$?" in 0) return 0 ;; 1) return 1 ;; *) return 2 ;; esac
}

do_combine() {
  local orig=("$@") NEW="" VERIFY="" DRY=0 SRC=() p f i n id d
  [ $# -ge 1 ] || die "usage: forge-parallel.sh combine <new-plan> <plan-a> <plan-b> [<plan>...] [--verify <command>] [--dry-run]"
  NEW="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --verify) [ -n "${2:-}" ] || die "combine: --verify needs a command"; VERIFY="$2"; shift 2 ;;
      --dry-run) DRY=1; shift ;;
      -*) die "combine: unknown option '$1'" ;;
      *) SRC+=("$1"); shift ;;
    esac
  done
  [ "${#SRC[@]}" -ge 2 ] || die "combine needs at least two source plans to combine (got ${#SRC[@]})" 3
  NEW="$(plans_abs "$NEW")"

  local norm=() seen=""
  for p in "${SRC[@]}"; do
    [ -d "$p" ] || die "combine: plan dir '$p' does not exist" 3
    p="$(cd "$p" && pwd)"
    [ -f "$p/tasks.tsv" ] || die "combine: '$p' is not a forge plan (no tasks.tsv)" 3
    [ "$p" != "$NEW" ] || die "combine: the new plan cannot be one of its sources" 3
    case "$seen" in *"|$p|"*) die "combine: plan '$p' is listed twice" 3 ;; esac
    seen="$seen|$p|"
    norm+=("$p")
  done
  SRC=("${norm[@]}")

  if [ "${FORGE_COMBINE_LOCKED:-}" != 1 ]; then
    local locks=()
    for p in "${SRC[@]}"; do locks+=("$p/schedule.lock"); done
    exec python3 -c "$PLANS_LOCK_PY" "$SELF" "${#SRC[@]}" "${locks[@]}" "${orig[@]}"
  fi

  # --- every precondition, before anything is created -------------------------------
  local REPO="" repo_i rid wtr acc br wt tip
  local S_RUN=() S_TIP=() S_NAME=()
  for p in "${SRC[@]}"; do
    for f in repo run_id wt_root accepted.integration; do
      [ -s "$p/$f" ] || die "plan '$p' has no $f: it never ran far enough to have integration state to combine" 3
    done
    repo_i="$(cat "$p/repo")"
    [ -d "$repo_i" ] || die "plan '$p': its repository '$repo_i' does not exist" 3
    repo_i="$(cd "$repo_i" && pwd -P)"
    if [ -z "$REPO" ]; then REPO="$repo_i"
    elif [ "$repo_i" != "$REPO" ]; then
      die "plans from different repositories cannot be combined: '${SRC[0]}' is in $REPO but '$p' is in $repo_i" 3
    fi
    rid="$(cat "$p/run_id")"; wtr="$(cat "$p/wt_root")"; acc="$(cat "$p/accepted.integration")"
    br="forge/$rid-integration"; wt="$wtr/_integration"
    tip="$(git -C "$REPO" rev-parse --verify --quiet "refs/heads/$br^{commit}")" \
      || die "plan '$p': integration branch $br does not exist" 3
    [ "$tip" = "$acc" ] \
      || die "plan '$p': $br is at $(plans_short "$tip") but the run accepted $(plans_short "$acc"); it changed outside forge, so it is not safe to combine" 3
    [ -e "$wt/.git" ] || die "plan '$p': its integration worktree $wt is missing" 3
    [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null)" = "$tip" ] \
      || die "plan '$p': its integration worktree $wt is not at the accepted tip" 3
    [ -z "$(git -C "$wt" status --porcelain -- . ':(exclude).forge' 2>/dev/null)" ] \
      || die "plan '$p': its integration worktree $wt has uncommitted changes; commit or discard them first" 3
    [ -z "$(awk -F'\t' '!/^#/ && NF && NF!=7 {print $1}' "$p/tasks.tsv")" ] \
      || die "plan '$p': tasks.tsv must have exactly seven columns" 3
    S_RUN+=("$rid"); S_TIP+=("$tip"); S_NAME+=("$(basename "$p")")
  done

  # The new plan's own names are checked before the sources' contents: they are the
  # cheapest refusal and the one most often caused by a typo.
  local NEWRID NEWWTR NEWBR NEWINT repo_text
  repo_text="$(cat "${SRC[0]}/repo")"
  NEWRID="$(plans_run_id "$NEW")"
  NEWWTR="$(plans_wt_root_for "$repo_text" "$NEWRID")"
  NEWBR="forge/$NEWRID-integration"; NEWINT="$NEWWTR/_integration"
  plans_target_checks "$REPO" "$NEW" "$NEWRID" "$NEWWTR"

  local owners="" owner
  n="${#SRC[@]}"; i=0
  while [ "$i" -lt "$n" ]; do
    for id in $(all_ids "${SRC[$i]}/tasks.tsv"); do
      owner="$(printf '%s' "$owners" | awk -F'\t' -v id="$id" '$1==id {print $2; exit}')"
      [ -z "$owner" ] || die "duplicate task id '$id': it is in both '$owner' and '${SRC[$i]}'; ids must be unique across the plans being combined" 3
      owners="$owners$id$(printf '\t')${SRC[$i]}
"
    done
    i=$((i+1))
  done
  i=0
  while [ "$i" -lt "$n" ]; do
    for id in $(all_ids "${SRC[$i]}/tasks.tsv"); do
      for d in $(field "${SRC[$i]}/tasks.tsv" "$id" deps | tr ',' ' '); do
        [ "$d" = - ] && continue
        printf '%s' "$owners" | awk -F'\t' -v d="$d" '$1==d {f=1} END {exit !f}' \
          || die "plan '${SRC[$i]}': task '$id' depends on '$d', which is in none of the plans being combined" 3
      done
    done
    i=$((i+1))
  done

  # Only finished work can be combined: a task that never merged has no place in the result
  # and silently dropping it would hide a failure.
  local left="" st
  i=0
  while [ "$i" -lt "$n" ]; do
    for id in $(all_ids "${SRC[$i]}/tasks.tsv"); do
      st="$(task_status "${SRC[$i]}" "$id")"
      [ "$st" = MERGED ] || left="$left
  ${S_NAME[$i]}: $id ($st)"
    done
    i=$((i+1))
  done
  [ -z "$left" ] || die "tasks left behind: every task of every source plan must be MERGED before plans are combined.$left
Retry or accept them (or split them off into their own plan) first." 3

  # The base the new plan's whole-run review diffs from: the oldest base of any source.
  local bases="" b
  i=0
  while [ "$i" -lt "$n" ]; do
    b="$(cat "${SRC[$i]}/integration.base" 2>/dev/null)"
    [ -n "$b" ] || b="$(git -C "$REPO" merge-base "${S_TIP[$i]}" HEAD 2>/dev/null)"
    [ -z "$b" ] || bases="$bases $b"
    i=$((i+1))
  done
  local newbase=""
  [ -z "$bases" ] || newbase="$(git -C "$REPO" merge-base --octopus $bases 2>/dev/null)"

  local ntasks=0
  for p in "${SRC[@]}"; do ntasks=$((ntasks + $(all_ids "$p/tasks.tsv" | wc -l | tr -d ' '))); done

  if [ "$DRY" = 1 ]; then
    echo "combine: would create plan $NEW (run forge/$NEWRID) from $n plans, $ntasks tasks, all MERGED"
    echo "  integration branch $NEWBR at $(plans_short "${S_TIP[0]}") (${S_NAME[0]}), worktree $NEWINT"
    i=1
    while [ "$i" -lt "$n" ]; do
      echo "  then git merge --no-ff ${S_NAME[$i]} ($(plans_short "${S_TIP[$i]}"))"
      i=$((i+1))
    done
    i=0
    while [ "$i" -lt "$n" ]; do
      local j=$((i+1))
      while [ "$j" -lt "$n" ]; do
        plans_predict_conflict "$REPO" "${S_TIP[$i]}" "${S_TIP[$j]}"
        case "$?" in
          0) echo "  merge check: ${S_NAME[$i]} and ${S_NAME[$j]} merge cleanly" ;;
          1) echo "  merge check: ${S_NAME[$i]} and ${S_NAME[$j]} WOULD CONFLICT; the real run would stop with exit 6" ;;
        esac
        j=$((j+1))
      done
      i=$((i+1))
    done
    echo "  tasks:$(for p in "${SRC[@]}"; do for id in $(all_ids "$p/tasks.tsv"); do printf ' %s' "$id"; done; done)"
    if [ -n "$VERIFY" ]; then echo "  verify: $VERIFY"; fi
    echo "(dry run — nothing created or changed)"
    return 0
  fi

  # --- build ----------------------------------------------------------------------
  plans_reset_undo
  PLANS_U_REPO="$REPO"; PLANS_U_PLAN="$NEW"; PLANS_U_WTR="$NEWWTR"
  trap 'plans_undo; exit 130' INT TERM
  if [ -d "$NEW" ]; then PLANS_U_PLAN_NEW=0
  else mkdir -p "$NEW" || { plans_undo; die "could not create '$NEW'" 3; }; PLANS_U_PLAN_NEW=1
  fi
  if [ -d "$NEWWTR" ]; then PLANS_U_WTR_NEW=0
  else mkdir -p "$NEWWTR" || { plans_undo; die "could not create '$NEWWTR'" 3; }; PLANS_U_WTR_NEW=1
  fi
  plans_make_integration "$REPO" "$NEWBR" "${S_TIP[0]}" "$NEWINT" \
    || { plans_undo; die "could not create $NEWBR and its worktree" 3; }

  local mlog="$NEW/.merge.log" files merged_names="${S_NAME[0]}"
  i=1
  while [ "$i" -lt "$n" ]; do
    if ! git -C "$NEWINT" -c user.name=forge -c user.email=forge@local \
         merge --no-ff -m "forge: combine ${S_NAME[$i]}" "${S_TIP[$i]}" > "$mlog" 2>&1; then
      files="$(git -C "$NEWINT" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')"
      git -C "$NEWINT" merge --abort >/dev/null 2>&1
      local why; why="$(tail -3 "$mlog" 2>/dev/null | tr '\n' ' ')"
      plans_undo
      trap - INT TERM
      if [ -n "$files" ]; then
        die "plans conflict: '${S_NAME[$i]}' cannot be merged with $merged_names (conflicting files: $files). Nothing was created; split the overlapping work into one plan or run it in sequence." 6
      fi
      die "merging '${S_NAME[$i]}' into $merged_names failed: $why Nothing was created." 6
    fi
    merged_names="$merged_names + ${S_NAME[$i]}"
    i=$((i+1))
  done
  rm -f "$mlog"

  local ok=1 tsv="$NEW/tasks.tsv"
  {
    printf '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
    for p in "${SRC[@]}"; do awk -F'\t' '!/^#/ && NF' "$p/tasks.tsv"; done
  } > "$tsv" || ok=0
  printf '%s\n' "$repo_text" > "$NEW/repo" || ok=0
  printf '%s\n' "$NEWRID" > "$NEW/run_id" || ok=0
  printf '%s\n' "$NEWWTR" > "$NEW/wt_root" || ok=0
  git -C "$NEWINT" rev-parse HEAD > "$NEW/accepted.integration" || ok=0
  [ -z "$newbase" ] || printf '%s\n' "$newbase" > "$NEW/integration.base"
  for p in "${SRC[@]}"; do
    for id in $(all_ids "$p/tasks.tsv"); do
      plans_copy_taskdir "$p/tasks/$id" "$NEW/tasks/$id" || ok=0
    done
  done
  [ "$ok" = 1 ] || { plans_undo; trap - INT TERM; die "could not write the combined plan" 3; }

  # Options: a plan-wide choice carries over when every source made it identically; the
  # two permission flags only when every source granted them.
  local goals="" g
  for p in "${SRC[@]}"; do
    if [ -s "$p/goal.txt" ]; then g="$(tr '\n' ' ' < "$p/goal.txt" | sed 's/  *$//')"; else g="$(basename "$p")"; fi
    if [ -z "$goals" ]; then goals="$g"; else goals="$goals + $g"; fi
  done
  printf '%s\n' "$goals" > "$NEW/goal.txt"
  for f in no_ripwire no_memory; do
    for p in "${SRC[@]}"; do [ -f "$p/$f" ] && { : > "$NEW/$f"; break; }; done
  done
  local all same
  for f in yolo_dwarf yolo_qa; do
    all=1; for p in "${SRC[@]}"; do [ -f "$p/$f" ] || all=0; done
    [ "$all" = 1 ] && : > "$NEW/$f"
  done
  for f in qa_threshold timeout verify_retries; do
    same=1
    for p in "${SRC[@]}"; do
      if [ ! -f "$p/$f" ] || ! cmp -s "$p/$f" "${SRC[0]}/$f"; then same=0; fi
    done
    if [ "$same" = 1 ]; then cp -p "${SRC[0]}/$f" "$NEW/$f"
    else
      for p in "${SRC[@]}"; do
        if [ -f "$p/$f" ]; then note "the plans do not agree on $f; the combined plan leaves it unset"; break; fi
      done
    fi
  done
  # The integration worktree needs a setup step to build and test the combined result, and
  # no task is ever dispatched here, so the first source's is as good as any.
  for p in "${SRC[@]}"; do [ -s "$p/setup_cmd" ] && { cp -p "$p/setup_cmd" "$NEW/setup_cmd"; break; }; done
  if [ -n "$VERIFY" ]; then printf '%s' "$VERIFY" > "$NEW/verify_cmd"
  else
    for p in "${SRC[@]}"; do [ -s "$p/verify_cmd" ] && { cp -p "$p/verify_cmd" "$NEW/verify_cmd"; break; }; done
  fi
  for f in fractal-routing.json jev-selection.json; do
    for p in "${SRC[@]}"; do [ -f "$p/$f" ] && { cp -p "$p/$f" "$NEW/$f"; break; }; done
  done
  {
    i=0
    while [ "$i" -lt "$n" ]; do
      printf '%s\t%s\t%s\n' "${SRC[$i]}" "${S_RUN[$i]}" "${S_TIP[$i]}"
      i=$((i+1))
    done
  } > "$NEW/combined-from.txt"

  compute_waves "$NEW"
  write_results "$NEW"
  trap - INT TERM
  # The integration worktree validates the combined result, so it needs what a task
  # worktree gets: whatever setup the repository asks for. Never fatal.
  mkdir -p "$NEW/tasks/_integration"
  run_worktree_setup "$NEW" "$repo_text" "$NEWINT" "$NEW/tasks/_integration" _integration

  echo "combined $n plans into $NEW (run forge/$NEWRID): $ntasks tasks, all MERGED"
  echo "integration branch: $NEWBR at $(plans_short "$(cat "$NEW/accepted.integration")")"
  i=0
  while [ "$i" -lt "$n" ]; do
    echo "  from ${S_NAME[$i]}: $(all_ids "${SRC[$i]}/tasks.tsv" | tr '\n' ' ')"
    i=$((i+1))
  done
  echo "task branches keep their original names (forge/<original run>/<task>); results.tsv shows the new run id."
  echo "next: forge-parallel.sh run $NEW        (verification only; no model is called)"
  echo "      forge-parallel.sh integrate $NEW --approved"
  return 0
}

# --- split ----------------------------------------------------------------------
# Moves some tasks of a plan, with their branches, worktrees and saved review state, into
# a new plan that can run on its own. Copy first, delete last: until the new plan is fully
# written the source is untouched, and any failure on the way puts the moved worktrees and
# branches back.
do_split() {
  local PLAN="${1:?split needs a plan dir}" NEW="" LIST="" DRY=0 id f PICKED
  shift
  [ $# -ge 1 ] && [ "${1#-}" = "$1" ] || die "usage: forge-parallel.sh split <plan-dir> <new-plan> --tasks <id,id> [--dry-run]"
  NEW="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --tasks) [ -n "${2:-}" ] || die "split: --tasks needs a comma-separated list of task ids"; LIST="$2"; shift 2 ;;
      --dry-run) DRY=1; shift ;;
      *) die "split: unknown option '$1'" ;;
    esac
  done
  [ -n "$LIST" ] || die "split needs --tasks <id,id>: the tasks to move into the new plan"
  NEW="$(plans_abs "$NEW")"
  [ "$NEW" != "$PLAN" ] || die "split: the new plan cannot be the source plan" 3

  local T="$PLAN/tasks.tsv" SEL=" "
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'" 3
  for id in $(printf '%s' "$LIST" | tr ',' ' '); do
    all_ids "$T" | grep -qx "$id" || die "task '$id' is not in this plan" 3
    case "$SEL" in *" $id "*) ;; *) SEL="$SEL$id " ;; esac
  done
  PICKED="$(printf '%s' "$SEL" | sed 's/^ *//; s/ *$//')"

  # Fractal binds a task's state to hashed output paths under the plan directory; moving
  # the task out would orphan it, and no honest way to re-bind it exists.
  if [ -s "$PLAN/fractal-selection.json" ]; then
    python3 -c 'import json, sys
try:
    sys.exit(0 if json.load(open(sys.argv[1])).get("enabled") is False else 1)
except Exception:
    sys.exit(1)' "$PLAN/fractal-selection.json" \
      || die "this plan runs under Fractal, which binds each task to output paths under the plan directory; a Fractal-backed plan cannot be split" 3
  fi
  for f in repo run_id wt_root accepted.integration; do
    [ -s "$PLAN/$f" ] || die "plan has no $f: it has not run far enough to split ('run' it first)" 3
  done
  local REPO repo_text run_id old_wtr acc
  repo_text="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  old_wtr="$(cat "$PLAN/wt_root")"; acc="$(cat "$PLAN/accepted.integration")"
  [ -d "$repo_text" ] || die "the plan's repository '$repo_text' does not exist" 3
  REPO="$(cd "$repo_text" && pwd -P)"
  git -C "$REPO" rev-parse --verify --quiet "$acc^{commit}" >/dev/null \
    || die "the accepted integration commit $acc is not in $REPO" 3

  # Per selected task: what it is, and whether it may move.
  local st dep dst others=0
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) others=$((others+1)) ;; esac
  done
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) continue ;; esac
    st="$(task_status "$PLAN" "$id")"
    case "$st" in
      MERGED) die "task '$id' is MERGED: its work is already in the integration branch and cannot move to another plan" 3 ;;
      RUNNING) die "task '$id' is RUNNING (or a run was killed); 'forge-parallel.sh run $PLAN' settles it first" 3 ;;
    esac
  done
  # Closed under dependents: a task left behind may not depend on a task that moves away.
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) continue ;; esac
    for dep in $(field "$T" "$id" deps | tr ',' ' '); do
      case "$SEL" in *" $dep "*) die "task '$id' stays behind but depends on '$dep', which is being moved; include '$id' in --tasks (or leave '$dep' too)" 3 ;; esac
    done
  done
  # A moved task may depend on merged work (the new integration branch already has it),
  # but not on a task that is neither moving with it nor merged.
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) continue ;; esac
    for dep in $(field "$T" "$id" deps | tr ',' ' '); do
      [ "$dep" = - ] && continue
      case "$SEL" in *" $dep "*) continue ;; esac
      dst="$(task_status "$PLAN" "$dep")"
      [ "$dst" = MERGED ] || die "task '$id' depends on '$dep', which is not merged (status $dst) and is not being moved; move '$dep' too or finish it first" 3
    done
  done
  local STAYS=""   # built in a loop: bash 3.2 cannot parse a `case` inside $( )
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) STAYS="$STAYS $id" ;; esac
  done
  [ "$others" -gt 0 ] || die "that selection is every task in the plan: nothing would stay behind. Choose a subset, or just use a new plan directory" 3

  local NEWRID NEWWTR NEWBR NEWINT
  NEWRID="$(plans_run_id "$NEW")"
  NEWWTR="$(plans_wt_root_for "$repo_text" "$NEWRID")"
  NEWBR="forge/$NEWRID-integration"; NEWINT="$NEWWTR/_integration"
  plans_target_checks "$REPO" "$NEW" "$NEWRID" "$NEWWTR"

  if [ "$DRY" = 1 ]; then
    echo "split: would move $PICKED from $PLAN to a new plan $NEW (run forge/$NEWRID)"
    echo "  integration branch $NEWBR at $(plans_short "$acc"), worktree $NEWINT"
    for id in $(all_ids "$T"); do
      case "$SEL" in *" $id "*) ;; *) continue ;; esac
      echo "  $id ($(task_status "$PLAN" "$id")): branch forge/$run_id/$id -> forge/$NEWRID/$id; worktree $old_wtr/$id -> $NEWWTR/$id"
    done
    echo "  stays in $PLAN:$STAYS"
    echo "(dry run — nothing created or changed)"
    return 0
  fi

  # --- copy ------------------------------------------------------------------------
  plans_reset_undo
  PLANS_U_REPO="$REPO"; PLANS_U_PLAN="$NEW"; PLANS_U_WTR="$NEWWTR"
  trap 'plans_undo; exit 130' INT TERM
  git -C "$REPO" worktree prune >/dev/null 2>&1
  if [ -d "$NEW" ]; then PLANS_U_PLAN_NEW=0
  else mkdir -p "$NEW" || { plans_undo; die "could not create '$NEW'" 3; }; PLANS_U_PLAN_NEW=1
  fi
  if [ -d "$NEWWTR" ]; then PLANS_U_WTR_NEW=0
  else mkdir -p "$NEWWTR" || { plans_undo; die "could not create '$NEWWTR'" 3; }; PLANS_U_WTR_NEW=1
  fi
  plans_make_integration "$REPO" "$NEWBR" "$acc" "$NEWINT" \
    || { plans_undo; trap - INT TERM; die "could not create $NEWBR and its worktree" 3; }

  local oldbr newbr oldwt newwt head_ref old_branches=""
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) continue ;; esac
    oldbr="forge/$run_id/$id"; newbr="forge/$NEWRID/$id"
    oldwt="$old_wtr/$id"; newwt="$NEWWTR/$id"
    git -C "$REPO" show-ref --verify --quiet "refs/heads/$oldbr" || continue   # never ran
    git -C "$REPO" branch "$newbr" "$oldbr" >/dev/null 2>&1 \
      || { plans_undo; trap - INT TERM; die "could not create branch $newbr" 3; }
    plans_journal branch "$newbr" ""
    old_branches="$old_branches $oldbr"
    if [ -e "$oldwt/.git" ]; then
      head_ref="$(git -C "$oldwt" symbolic-ref -q HEAD 2>/dev/null)"
      git -C "$REPO" worktree move "$oldwt" "$newwt" >/dev/null 2>&1 \
        || { plans_undo; trap - INT TERM; die "could not move worktree $oldwt to $newwt" 3; }
      plans_journal move "$oldwt" "$newwt"
      # Same commit, same index, same working tree: only the name HEAD points at changes.
      if [ "$head_ref" = "refs/heads/$oldbr" ]; then
        git -C "$newwt" symbolic-ref HEAD "refs/heads/$newbr" >/dev/null 2>&1 \
          || { plans_undo; trap - INT TERM; die "could not switch $newwt to branch $newbr" 3; }
        plans_journal head "$newwt" "$oldbr"
      fi
    fi
  done

  # --- write the new plan -----------------------------------------------------------
  local ok=1
  {
    printf '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
    # Dependencies on merged tasks are satisfied by the new integration branch and are
    # dropped; dependencies between moved tasks are kept.
    awk -F'\t' -v OFS='\t' -v ids="$SEL" '
      /^#/ || !NF { next }
      index(ids, " " $1 " ") {
        n = split($2, d, ","); out = ""
        for (i = 1; i <= n; i++)
          if (d[i] != "" && d[i] != "-" && index(ids, " " d[i] " "))
            out = out (out == "" ? "" : ",") d[i]
        $2 = (out == "" ? "-" : out); print
      }' "$T"
  } > "$NEW/tasks.tsv" || ok=0
  printf '%s\n' "$repo_text" > "$NEW/repo" || ok=0
  printf '%s\n' "$NEWRID" > "$NEW/run_id" || ok=0
  printf '%s\n' "$NEWWTR" > "$NEW/wt_root" || ok=0
  printf '%s\n' "$acc" > "$NEW/accepted.integration" || ok=0
  [ ! -s "$PLAN/integration.base" ] || cp -p "$PLAN/integration.base" "$NEW/integration.base" || ok=0
  [ ! -s "$PLAN/goal.txt" ] || cp -p "$PLAN/goal.txt" "$NEW/goal.txt" || ok=0
  for f in $PLANS_OPTION_FILES; do
    [ ! -e "$PLAN/$f" ] || cp -p "$PLAN/$f" "$NEW/$f" || ok=0
  done
  for id in $(all_ids "$T"); do
    case "$SEL" in *" $id "*) ;; *) continue ;; esac
    plans_copy_taskdir "$PLAN/tasks/$id" "$NEW/tasks/$id" || ok=0
  done
  { echo "split from $PLAN"; echo "run_id $run_id"; echo "tasks $PICKED"; } > "$NEW/split-from.txt" || ok=0
  [ "$ok" = 1 ] || { plans_undo; trap - INT TERM; die "could not write the new plan" 3; }
  compute_waves "$NEW"
  write_results "$NEW"

  # --- delete ----------------------------------------------------------------------
  # Only now does the source lose anything. From here an interrupt must NOT undo the move:
  # the new plan is complete, and undoing it would leave the source without its rows.
  trap - INT TERM
  local srctmp="$PLAN/.tasks.tsv.split" marker="$PLAN/split-$(basename "$NEW").txt"
  if ! awk -F'\t' -v ids="$SEL" '/^#/ || !NF {print; next} index(ids, " " $1 " ") {next} {print}' "$T" > "$srctmp" \
     || ! mv "$srctmp" "$T"; then
    rm -f "$srctmp"; plans_undo; trap - INT TERM
    die "could not update '$T'; nothing was moved" 3
  fi
  {
    echo "moved to $NEW (run forge/$NEWRID) on $(date '+%Y-%m-%dT%H:%M:%S%z')"
    for id in $PICKED; do
      printf '%s\t%s\n' "$id" "forge/$run_id/$id -> forge/$NEWRID/$id"
    done
  } > "$marker"
  for id in $PICKED; do rm -rf "$PLAN/tasks/$id"; done
  compute_waves "$PLAN"
  write_results "$PLAN"
  for oldbr in $old_branches; do
    git -C "$REPO" branch -D "$oldbr" >/dev/null 2>&1 || note "could not delete the old branch $oldbr; it is harmless, delete it by hand"
  done
  mkdir -p "$NEW/tasks/_integration"
  run_worktree_setup "$NEW" "$repo_text" "$NEWINT" "$NEW/tasks/_integration" _integration

  echo "split: moved $PICKED from $PLAN to $NEW (run forge/$NEWRID)"
  echo "integration branch: $NEWBR at $(plans_short "$acc")"
  for id in $PICKED; do
    echo "  $id ($(task_status "$NEW" "$id")): forge/$NEWRID/$id   worktree $NEWWTR/$id"
  done
  echo "stays in $PLAN: $(all_ids "$T" | tr '\n' ' ')"
  echo "next: forge-parallel.sh run $NEW [--retry-failed 1]   or   forge-parallel.sh retry $NEW <task-id>"
  echo "      the two plans have separate locks and integration branches; 'combine' joins them again"
  return 0
}
