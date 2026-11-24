#!/usr/bin/env bash
# Recursive decomposition for forge-parallel.sh (#153): a task split into subtasks, and a goal
# split into plans.
#
# Sourced by forge-parallel.sh, which provides die/note/field/all_ids/task_status, compute_waves,
# carry_findings, forge_dataset_event, forge_fast_tier, forge_pools_file, DISPATCH and SELF.
# Functions only; nothing runs at source time. The node model, the limits and the validation of a
# split are scripts/forge_decompose.py; the planner's side is scripts/forge-planner.py.
#
#   plan  <plan> --split-depth N --split-children N --split-nodes N    the limits, frozen by the first run
#   run   <plan> --auto-split [--split-planner SPEC] | --no-auto-split  a task that stops on `split` is
#                                                                       split by the planner, within the limits
#   retry <plan> <task> --decide split[=fresh] [--subtasks FILE | --planner SPEC] --reason "..." --approved
#   next  <plan> [<new-plan-dir>] [--planner SPEC] [plan flags...]      plan the next stage of a larger goal
#
# A split task's subtasks are a plan of their own, <plan>/tasks/<id>/sub. The task's next entry runs
# that plan instead of an implementer (run_split_task), merges its integration branch into the task's
# branch, and the task is then snapshotted and reviewed as any attempt is: on its cumulative diff.

forge_decompose_py() { python3 "$SKILL_DIR/scripts/forge_decompose.py" "$@"; }

forge_is_split() { # forge_is_split <plan> <id>: the task has a sub-plan
  [ -f "$1/tasks/$2/sub/tasks.tsv" ]
}

# The task's next entry runs its sub-plan: it has one, and its work is not on the task's branch yet.
forge_split_open() { # forge_split_open <plan> <id>
  [ -f "$1/tasks/$2/sub/tasks.tsv" ] && [ ! -e "$1/tasks/$2/sub.merged" ]
}

forge_is_sub_plan() { # forge_is_sub_plan <plan>
  [ -f "$1/parent.json" ] || return 1
  case "$1" in */tasks/*/sub) return 0 ;; esac
  return 1
}

forge_decide_is_split() { # forge_decide_is_split <--decide value>
  case "${1:-}" in split|split=keep|split=fresh) return 0 ;; esac
  return 1
}

# --- plan ------------------------------------------------------------------------------------

# The limits a plan was given, set before anything else of the plan is written: a value out of
# range, or a change after the first run froze them, refuses the whole `plan` call (exit 2).
forge_decompose_limits() { # forge_decompose_limits <plan> <depth|""> <children|""> <nodes|"">
  local out args=()
  [ -z "$2" ] || args+=(--depth "$2")
  [ -z "$3" ] || args+=(--children "$3")
  [ -z "$4" ] || args+=(--nodes "$4")
  [ "${#args[@]}" -gt 0 ] || return 0
  out="$(forge_decompose_py limits "$1" "${args[@]}" 2>&1 >/dev/null)" || die "${out#refused: }" 2
}

# One sub-plan made ready to run: its waves, and the tier each subtask's model runs in (what `plan`
# records for a row it routes, so an escalation of a subtask knows where it starts).
forge_sub_prepare() { # forge_sub_prepare <sub-plan>
  local sub="$1" id
  ( compute_waves "$sub" ) || return 1
  for id in $(all_ids "$sub/tasks.tsv"); do
    python3 "$SKILL_DIR/scripts/forge-route.py" assign --plan "$sub" --task "$id" \
      --tier "$(field "$sub/tasks.tsv" "$id" difficulty)" --epsilon 0 \
      --default "$(field "$sub/tasks.tsv" "$id" dwarf)" --pools "$(forge_pools_file "$sub")" >/dev/null 2>&1 || true
  done
  forge_decompose_snapshot "$sub"
}

# The dataset's record of a plan (a sub-plan is never made by `plan`, which records the others), and
# again of the plan a task was just split in: the decision is part of what was planned.
forge_decompose_snapshot() { # forge_decompose_snapshot <plan>
  forge_dataset_off && return 0
  python3 "$SKILL_DIR/scripts/forge-dataset.py" plan "$1" >/dev/null 2>&1 || true
}

forge_sub_prepare_all() { # forge_sub_prepare_all <plan>: every sub-plan under it that has no waves yet
  local sub rc=0
  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    [ -s "$sub/waves.tsv" ] && continue
    forge_sub_prepare "$sub" || rc=1
  done <<FORGE_SUBS
$(forge_decompose_py subs "$1" 2>/dev/null)
FORGE_SUBS
  return $rc
}

# After `plan` routed the table: the planner's proposed splits become sub-plans within the limits,
# and every other task is recorded as kept whole. DECOMPOSE_LINES is what happened, for the table.
forge_decompose_settle() { # forge_decompose_settle <plan>
  local by=default
  DECOMPOSE_LINES=""
  [ ! -s "$1/planner.prompt" ] || by=planner
  DECOMPOSE_LINES="$(forge_decompose_py settle "$1" --by "$by" 2>/dev/null)" || true
  forge_sub_prepare_all "$1" || note "a sub-plan's waves could not be computed; forge-parallel.sh run reports it"
}

# Under the task table: the limits (always: they are what the user approves with the plan), what the
# planner split, and the stages it proposes after this plan.
forge_decompose_table() { # forge_decompose_table <plan>
  local plan="$1" tree stages
  forge_decompose_py limits "$plan" 2>/dev/null || return 0
  if [ -f "$plan/auto_split" ]; then
    echo "  auto-split is on: a task that stops on \`split\` is split by the planner within these limits"
  else
    echo "  a task that stops on \`split\` waits for your choice (run --auto-split lets the planner split it within these limits)"
  fi
  [ -z "${DECOMPOSE_LINES:-}" ] || printf '%s\n' "$DECOMPOSE_LINES" | sed 's/^/  /'
  tree="$(forge_decompose_py tree "$plan" --split-only 2>/dev/null)"
  if [ -n "$tree" ]; then
    echo "subtasks (a split task runs its subtasks as a plan of its own, on its branch, and is then reviewed on all of it):"
    printf '%s\n' "$tree" | sed 's/^/  /'
  fi
  stages="$(forge_decompose_py later "$plan" 2>/dev/null)"
  if [ -n "$stages" ]; then
    echo "later stages (the goal is larger than this plan; each is planned when the one before has merged):"
    printf '%s\n' "$stages" | sed 's/^/  /'
    echo "  plan the next one with: forge-parallel.sh next $plan"
  fi
  echo
}

# --- run -------------------------------------------------------------------------------------

# The planner a split is asked of: run --split-planner, else the planner the plan was made with.
forge_split_planner() { # forge_split_planner <plan>
  if [ -s "$1/split_planner" ]; then head -1 "$1/split_planner"
  elif [ -s "$1/planner" ]; then head -1 "$1/planner"; fi
}

# run --auto-split / --no-auto-split / --split-planner, as do_run parsed them. Kept as plan files.
forge_auto_split_store() { # forge_auto_split_store <plan> <on|off|""> <planner spec|"">
  local plan="$1"
  [ -z "$3" ] || printf '%s\n' "$3" > "$plan/split_planner" || die "could not write $plan/split_planner"
  case "$2" in
    on)  : > "$plan/auto_split" || die "could not write $plan/auto_split" ;;
    off) rm -f "$plan/auto_split" ;;
  esac
  if [ -f "$plan/auto_split" ] && [ -z "$(forge_split_planner "$plan")" ]; then
    die "--auto-split needs a planner to ask: give --split-planner SPEC (forge does not pick a model on your behalf)" 2
  fi
}

# The summary block for tasks whose subtasks have not all merged: not failures of the task itself.
report_subtasks() { # report_subtasks <plan>
  local plan="$1" id any=0
  for id in $(all_ids "$plan/tasks.tsv"); do
    [ -s "$plan/tasks/$id/sub.waiting" ] || continue
    [ "$any" = 1 ] || echo "waiting for their subtasks:"
    any=1
    printf '  %s: %s\n' "$id" "$(tr '\n' ',' < "$plan/tasks/$id/sub.waiting" | sed 's/,$//; s/,/, /g')"
    echo "      its plan: $plan/tasks/$id/sub (retry a subtask there: forge-parallel.sh retry $plan/tasks/$id/sub <subtask>)"
  done
  [ "$any" = 0 ] || { echo "  once they have merged, forge-parallel.sh run $plan brings them into the task and reviews it"; echo; }
}

# After a run whose every task merged: the stage the planner proposed next, if any.
report_later() { # report_later <plan>
  local plan="$1" stages
  forge_is_sub_plan "$plan" && return 0
  stages="$(forge_decompose_py later "$plan" 2>/dev/null)"
  [ -n "$stages" ] || return 0
  [ "$(awk -F'\t' '$2 != "MERGED" { n++ } END { print n + 0 }' "$plan/results.tsv" 2>/dev/null)" = 0 ] || return 0
  echo "this plan is one stage of a larger goal; the stages that follow:"
  printf '%s\n' "$stages" | sed 's/^/  /'
  echo "  plan the next one (it starts from this plan's integration branch): forge-parallel.sh next $plan"
  echo
}

# One slot of the tree's concurrency bound, held until this task process ends. A split task takes
# it for its own review: its scheduler gave its slot back while the subtasks ran. Never blocks a
# task that no scheduler is bounding (a `retry`).
forge_tree_slot() { # forge_tree_slot <task dir>
  local root="${FORGE_TREE_ROOT:-}" cap="${FORGE_TREE_CAP:-}" ready="$1/.slot.ready.$$" pid
  [ -n "$root" ] && [ -n "$cap" ] || return 0
  rm -f "$ready"
  python3 "$SKILL_DIR/scripts/forge_decompose.py" slot --root "$root" --cap "$cap" --while-pid "$$" --ready "$ready" \
    </dev/null >/dev/null 2>&1 &
  pid=$!
  while [ ! -e "$ready" ] && kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
  rm -f "$ready"
}

# The entry of a split task: runs its sub-plan and, when every subtask has merged, brings the
# sub-plan's integration branch onto the task's branch. Returns 0 then, and the caller goes on to
# the snapshot and the review of the cumulative diff. Otherwise the status is written and it
# returns non-zero: BLOCKED with sub.waiting (a subtask failed, stopped or waits for a person; the
# attempt is not spent, and the next `run` enters here again), or INTERRUPTED.
run_split_task() { # run_split_task <plan> <id> <tdir> <wt> <base> <attempt>
  local PLAN="$1" id="$2" tdir="$3" wt="$4" base="$5" attempt="$6"
  local sub="$3/sub" REPO run_id sub_run sub_br start tip rc=0 left args=() grace
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  sub_run="$(cat "$sub/run_id")"; sub_br="forge/$sub_run-integration"
  rm -f "$tdir/sub.waiting" "$tdir/sub.infra"
  if ! git -C "$REPO" rev-parse --verify --quiet "$sub_br" >/dev/null 2>&1; then
    # First entry: where the subtasks start. `keep` builds on the task's work as it stands, with
    # whatever an interrupted attempt left uncommitted; `fresh` starts over from the task's base and
    # keeps the earlier work on a ref of its own.
    start="$(python3 -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get("start") or "keep")
except Exception:
    print("keep")' "$sub/parent.json" 2>/dev/null)"
    if [ "$start" = fresh ]; then
      tip="$(git -C "$wt" rev-parse HEAD)" || { echo ERROR > "$tdir/status"; return 1; }
      if [ "$tip" != "$base" ]; then
        git -C "$REPO" update-ref "refs/forge/kept/$run_id/$id" "$tip" >/dev/null 2>&1 \
          && printf '%s\n' "$tip" > "$tdir/sub.kept" \
          || { echo ERROR > "$tdir/status"; note "$id: could not keep its earlier work on a ref; nothing was reset"; return 1; }
        note "$id: starting over from its base; its earlier work stays on refs/forge/kept/$run_id/$id"
      fi
      if ! { git -C "$wt" reset -q --hard "$base" && git -C "$wt" clean -fdq -e .forge; } >"$tdir/sub.reset.out" 2>&1; then
        echo ERROR > "$tdir/status"; note "$id: could not reset its worktree to its base (see $tdir/sub.reset.out)"; return 1
      fi
      rm -f "$tdir/changes.diff" "$tdir/changes.prev.diff" "$tdir/reviewed.commit"
    elif [ -n "$(git -C "$wt" status --porcelain -- . ':(exclude).forge' 2>/dev/null)" ]; then
      (cd "$wt" && git add -A -- . ':(exclude).forge' && git -c user.name=forge -c user.email=forge@local \
          commit -q -m "forge($id): work in progress, kept for its subtasks") >/dev/null 2>&1 \
        || { echo ERROR > "$tdir/status"; note "$id: could not commit the work its subtasks build on"; return 1; }
    fi
    tip="$(git -C "$wt" rev-parse HEAD)" || { echo ERROR > "$tdir/status"; return 1; }
    git -C "$REPO" branch "$sub_br" "$tip" >/dev/null 2>&1 \
      || { echo ERROR > "$tdir/status"; note "$id: could not create $sub_br for its subtasks"; return 1; }
    printf '%s\n' "$tip" > "$sub/integration.base"
  fi
  [ -s "$sub/waves.tsv" ] || forge_sub_prepare "$sub" \
    || { echo ERROR > "$tdir/status"; note "$id: its sub-plan has no valid task order (see $sub/tasks.tsv)"; return 1; }
  # The run's own budgets and bound apply to the subtasks too. A nested run gets a shorter grace on
  # an interrupt than the run above it, so it has ended before that one stops waiting for it.
  args=(--no-forecast --max-parallel "${FORGE_TREE_CAP:-$(cat "$PLAN/max_parallel" 2>/dev/null || echo 3)}")
  [ "${FORGE_RUN_RETRY_FAILED:-0}" -gt 0 ] 2>/dev/null && args+=(--retry-failed "$FORGE_RUN_RETRY_FAILED")
  [ "${FORGE_RUN_INFRA_RETRIES:-0}" -gt 0 ] 2>/dev/null && args+=(--infra-retries "$FORGE_RUN_INFRA_RETRIES")
  grace="$(awk -v g="${FORGE_INTERRUPT_GRACE:-10}" 'BEGIN { g += 0; if (g <= 0) g = 10; printf "%.1f", g * 0.6 }')"
  note "$id: running its $(all_ids "$sub/tasks.tsv" | grep -c .) subtasks (their plan: $sub)"
  FORGE_INTERRUPT_GRACE="$grace" FORGE_PLAN_LOCK="" FORGE_DATASET_TRIGGER="" \
    /bin/bash "$SELF" run "$sub" "${args[@]}" > "$tdir/sub.out" 2>&1 </dev/null || rc=$?
  left="$(forge_decompose_py waiting "$PLAN" "$id" 2>/dev/null)"
  if [ "$rc" = 130 ] || [ "$rc" -gt 128 ]; then
    echo INTERRUPTED > "$tdir/status"
    [ "$attempt" -gt 0 ] && echo $((attempt - 1)) > "$tdir/attempt"
    note "$id: interrupted while its subtasks ran"; return 1
  fi
  if [ -n "$left" ] || { [ "$rc" != 0 ] && [ "$rc" != 5 ]; }; then
    # Not a failure of this task: its subtasks have not all merged. Nothing was judged, so no
    # attempt is spent; the next run enters here again and the sub-plan resumes where it is.
    [ -n "$left" ] || left="its sub-plan run ended with exit $rc (see $tdir/sub.out)"
    printf '%s\n' "$left" > "$tdir/sub.waiting"
    [ "$rc" != 8 ] || : > "$tdir/sub.infra"
    [ "$attempt" -gt 0 ] && echo $((attempt - 1)) > "$tdir/attempt"
    echo BLOCKED > "$tdir/status"
    if [ "$rc" = 8 ]; then
      note "$id: its subtasks paused on an infrastructure failure ($(printf '%s' "$left" | tr '\n' ',' | sed 's/,$//; s/,/, /g')) — no attempt spent; re-run to resume"
    else
      note "$id: waiting for its subtasks ($(printf '%s' "$left" | tr '\n' ',' | sed 's/,$//; s/,/, /g')) — see $sub and $tdir/sub.out"
    fi
    return 1
  fi
  # Every subtask merged. Usually a fast-forward: the sub-plan's branch started at this task's tip.
  if ! git -C "$wt" -c user.name=forge -c user.email=forge@local merge --no-edit -q "$sub_br" >"$tdir/sub.merge.out" 2>&1; then
    git -C "$wt" merge --abort >/dev/null 2>&1 || true
    echo ERROR > "$tdir/status"
    note "$id: its subtasks' work ($sub_br) did not merge into its branch — see $tdir/sub.merge.out"; return 1
  fi
  git -C "$REPO" rev-parse "$sub_br" > "$tdir/sub.merged.tmp" && mv "$tdir/sub.merged.tmp" "$tdir/sub.merged" \
    || { rm -f "$tdir/sub.merged.tmp"; echo ERROR > "$tdir/status"; return 1; }
  # The sub-plan's integration worktree has done its job; its branch stays, so each subtask's work
  # can still be read.
  local sub_wt; sub_wt="$(cat "$sub/wt_root" 2>/dev/null)/_integration"
  if [ -d "$sub_wt" ] && (cd "$REPO" && git worktree remove --force "$sub_wt" >/dev/null 2>&1); then
    forge_worktree_event removed "$sub" "$REPO" "$sub_wt" "$sub_br" integration
  fi
  note "$id: its subtasks merged into its branch; reviewing the whole task"
  forge_tree_slot "$tdir"
  return 0
}

# --- the split of a task -----------------------------------------------------------------------

# One planner dispatch about one task: split it, or keep it whole. 0 when its reply was parsed into
# tasks/<id>/split.proposal.json, 8 on an infrastructure stop, 4 when it failed or was refused.
forge_split_ask_planner() { # forge_split_ask_planner <plan> <id> <keep|fresh> <spec>
  local PLAN="$1" id="$2" start="$3" spec="$4" tdir="$1/tasks/$2" where rc=0 tier out
  where="$(cat "$PLAN/wt_root" 2>/dev/null)/$id"
  # The planner reads the task's work so far when the subtasks will build on it.
  if [ "$start" = fresh ] || [ ! -d "$where" ]; then where="$(cat "$PLAN/repo")"; fi
  tier="$(forge_fast_tier "$PLAN" planner)"
  python3 "$SKILL_DIR/scripts/forge-planner.py" node-prompt --plan "$PLAN" --task "$id" --start "$start" >/dev/null || return 4
  mkdir -p "$tdir/split" && cp "$tdir/split.prompt" "$tdir/split/planner.prompt" || return 4
  note "$id: asking the planner ($spec) whether to split it"
  /bin/bash "$DISPATCH" planner "$spec" --repo "$where" --run-dir "$tdir/split" --prompt-file "$tdir/split/planner.prompt" \
    --output summary ${tier:+$tier} > "$tdir/split/planner.out" 2>&1 || rc=$?
  if [ "$rc" = 8 ]; then
    note "$id: the planner stopped on an infrastructure failure (see $tdir/split/planner.infra); the task is as it was"
    return 8
  fi
  [ "$rc" = 0 ] || { note "$id: the planner failed (exit $rc; see $tdir/split/planner.out); the task is as it was"; return 4; }
  out="$(python3 "$SKILL_DIR/scripts/forge-planner.py" node-parse --plan "$PLAN" --task "$id" --reply "$tdir/split/planner.last" 2>&1)" \
    || { note "$id: the planner's ${out:-reply was refused}; the task is as it was"; return 4; }
}

# The split of a task, decided by a person (`retry --decide split`, by `user`) or by a run with
# --auto-split (by `auto`). The subtasks come from <file> (a person or an orchestrator wrote them),
# from the planner <spec>, or from a proposal already stored and checked (neither given). On 0 the
# sub-plan exists, the decision is recorded, and the task's next entry runs it; the caller clears
# the status. Any other return leaves the task as it was: 2 usage, 3 not split (kept whole, at a
# limit, or refused; a limit and a kept-whole answer are recorded on the node), 8 the planner
# stopped on infrastructure.
retry_split() { # retry_split <plan> <id> <split|split=fresh> <reason> <file|""> <spec|""> <user|auto>
  local PLAN="$1" id="$2" decide="$3" reason="$4" file="$5" spec="$6" by="${7:-user}"
  local tdir="$1/tasks/$2" start=keep out rc=0 attempt ctx="" n
  case "$decide" in split=fresh) start=fresh ;; esac
  if forge_is_split "$PLAN" "$id"; then
    note "$id is already split into subtasks ($tdir/sub): they are kept, never made twice. forge-parallel.sh run $PLAN runs what is left of them"
    return 3
  fi
  if [ -n "$file" ]; then
    [ -f "$file" ] && [ -r "$file" ] || { note "retry: --subtasks '$file' is not a readable file"; return 2; }
    out="$(python3 "$SKILL_DIR/scripts/forge-planner.py" node-parse --plan "$PLAN" --task "$id" --reply "$file" 2>&1)" \
      || { note "$id: the ${out:-subtasks were refused}; nothing was changed"; return 3; }
  elif [ -n "$spec" ]; then
    forge_split_ask_planner "$PLAN" "$id" "$start" "$spec" || return $?
  elif [ ! -s "$tdir/split.proposal.json" ]; then
    note "retry: --decide split needs the subtasks: --subtasks FILE (a forge-plan block or its JSON, as forge-planner.py node-prompt describes it), or --planner SPEC to ask a planner"
    return 2
  fi
  if [ "$start" = keep ] && [ -s "$tdir/qa.last" ] && grep -q 'FORGE_VERDICT' "$tdir/qa.last" 2>/dev/null; then
    ctx="$tdir/.split.context"
    {
      echo "## The work so far, and its last review"
      echo
      echo "The parent task was attempted before it was split. That work is on the branch you start from: build on"
      echo "it, and do not redo what is there and right. Its last review said:"
      echo
      tail -c 6000 "$tdir/qa.last"
    } > "$ctx"
  fi
  out="$(forge_decompose_py split "$PLAN" "$id" --proposal "$tdir/split.proposal.json" --reason "$reason" --by "$by" \
    --start "$start" ${ctx:+--context-file "$ctx"} 2>&1)" || rc=$?
  rm -f "$tdir/split.proposal.json" "$tdir/.split.context"
  case "$rc" in
    0) ;;
    4) note "$id: not split — ${out#bounded: }. The node is recorded as bounded and the task stays as it is"; return 3 ;;
    5) note "$id: kept whole — the answer was one task, not a split. That is recorded with its reason, and the task stays as it is"; return 3 ;;
    *) note "$id: the split was refused — ${out#refused: }; nothing was changed"; return 3 ;;
  esac
  forge_sub_prepare_all "$PLAN" || { note "$id: its sub-plan has no valid task order (see $tdir/sub/tasks.tsv)"; }
  # The decision, as every decision is recorded. It grants the one attempt that reviews the whole
  # task once its subtasks have merged (retry=1 is what the attempt cap reads).
  attempt="$(cat "$tdir/attempt" 2>/dev/null || echo 0)"
  reason="$(printf '%s' "$reason" | tr '\n\r\t' '   ' | sed 's/^ *//; s/ *$//')"
  printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$attempt" "retry=1 $decide" "$reason" >> "$tdir/decisions.tsv" \
    || note "$id: could not record the decision in decisions.tsv"
  if [ "$start" = fresh ]; then
    rm -f "$tdir/retry_findings.md" "$tdir/retry_findings.commit" "$tdir/retry_findings.source" "$tdir/retry_findings.session" \
          "$tdir/retry_findings.resolved" "$tdir/retry_findings.dwarf.session" "$tdir/retry_findings.dwarf.resolved"
  elif grep -q 'FORGE_VERDICT' "$tdir/qa.last" 2>/dev/null; then
    carry_findings "$tdir"   # the review of the whole task is told what the last one found
  fi
  rm -f "$tdir/escalation.stop" "$tdir/split.suggested" "$tdir/escalation.next" "$tdir/decided.left" "$tdir/noretry" \
        "$tdir/decision.md" "$tdir/decision.from" "$tdir/resume" "$tdir/sub.waiting" "$tdir/sub.infra"
  forge_dataset_event decision --dir "$PLAN" --task "$id" --set "choice=$decide" --set "reason=$reason" \
    --set "attempt=$attempt" --set "by=$by"
  forge_decompose_snapshot "$PLAN"
  n="$(all_ids "$tdir/sub/tasks.tsv" | grep -c .)"
  note "$id: split into $n subtasks ($(all_ids "$tdir/sub/tasks.tsv" | tr '\n' ' ' | sed 's/ $//')); their plan is $tdir/sub"
  return 0
}

# run --auto-split (#153): a task that just stopped for a decision with `split` among its reasons is
# split by the planner, within the limits, without waiting. 0 when it was: the caller enters the
# task again, which now runs its subtasks. Asked once per task: a task the planner kept whole, or a
# split a limit refused, waits for the user like any other stop.
forge_auto_split() { # forge_auto_split <plan> <id>
  local PLAN="$1" id="$2" tdir="$1/tasks/$2" reasons spec rc=0
  [ -f "$PLAN/auto_split" ] || return 1
  [ "$(task_status "$PLAN" "$id")" = NEEDS_DECISION ] || return 1
  reasons="$(sed -n '1s/^reasons: //p' "$tdir/decision.md" 2>/dev/null)"
  case ", $reasons," in *", split,"*) ;; *) return 1 ;; esac
  forge_is_split "$PLAN" "$id" && return 1
  [ ! -f "$tdir/auto_split.asked" ] || return 1
  spec="$(forge_split_planner "$PLAN")"
  [ -n "$spec" ] || { note "$id: --auto-split has no planner to ask (run --split-planner SPEC); the task waits for your decision"; return 1; }
  : > "$tdir/auto_split.asked"
  retry_split "$PLAN" "$id" split "auto-split: the task stopped on split ($reasons)" "" "$spec" auto || rc=$?
  if [ "$rc" != 0 ]; then
    # An infrastructure stop of the planner decided nothing: the next run asks again.
    [ "$rc" != 8 ] || rm -f "$tdir/auto_split.asked"
    return 1
  fi
  rm -f "$tdir/status"
  return 0
}

# --- goal -> plans -----------------------------------------------------------------------------

# next <plan> [<new-plan-dir>] [--planner SPEC] [plan flags...]: the plan of the next stage of a
# larger goal. <plan> must have merged. The new plan starts from its integration branch (--after),
# takes its routing pools, its options and its limits, and is planned by the planner from the code
# as it is now. It stops at the table: every plan of a chain is approved on its own.
do_next() {
  local PLAN="${1:?next needs a plan dir}"; shift
  local NEW="" PLANNER="" EXTRA=() out new repo f key value pools=()
  if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then NEW="$1"; shift; fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --planner) PLANNER="${2:?--planner needs a spec}"; shift 2 ;;
      --approved) die "next plans the stage and stops at its table: each plan of a chain is approved on its own, then run with forge-parallel.sh run" 2 ;;
      *) EXTRA+=("$1"); shift ;;
    esac
  done
  [ -s "$PLAN/tasks.tsv" ] || die "no tasks.tsv in '$PLAN'"
  forge_refuse_fractal_run "$PLAN" next
  [ -n "$PLANNER" ] || PLANNER="$(head -1 "$PLAN/planner" 2>/dev/null || true)"
  [ -n "$PLANNER" ] || die "next needs a planner for the new plan: --planner SPEC" 2
  repo="$(cat "$PLAN/repo" 2>/dev/null)"
  [ -n "$repo" ] && [ -d "$repo" ] || die "'$PLAN' has no repository to plan in" 3
  out="$(forge_decompose_py next "$PLAN" ${NEW:+--dir "$NEW"} 2>&1)" || die "next: ${out#refused: }" 3
  new="$out"
  # What the plan before it was made with applies to this one: options, pools, limits.
  for f in no_ripwire no_memory yolo_dwarf yolo_qa reviewer_session implementer_session fast pre_review_tests \
           pre_review_parallel fail_first qa_threshold timeout verify_retries verify_cmd verify_parallel setup_cmd setup_before_verify final_qa \
           explore escalate_after retry_per_model max_attempts profile.json retro_mode summaries summaries_model \
           auto_split split_planner; do
    [ ! -f "$PLAN/$f" ] || cp -p "$PLAN/$f" "$new/$f" || die "could not write $new/$f" 3
  done
  [ ! -d "$PLAN/verify.d" ] || cp -Rp "$PLAN/verify.d" "$new/verify.d" || die "could not write $new/verify.d" 3
  while IFS="$(printf '\t')" read -r key value; do
    [ -n "$key" ] && [ -n "$value" ] || continue
    pools+=("$key" "$value")
  done <<FORGE_POOLS
$(python3 - "$(forge_pools_file "$PLAN")" "$PLAN/qa-routing.json" "$PLAN/decomposition.json" <<'PY'
import json, sys
def load(path):
    try:
        data = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
for role, path in (('dwarf', sys.argv[1]), ('qa', sys.argv[2])):
    for tier, flag in (('any', '--' + role), ('low', '--%s-low' % role), ('medium', '--%s-medium' % role), ('high', '--%s-high' % role)):
        specs = load(path).get(tier)
        if isinstance(specs, list) and specs and all(isinstance(s, str) and s and '\t' not in s for s in specs):
            print('%s\t%s' % (flag, ','.join(specs)))
limits = load(sys.argv[3]).get('limits')
if isinstance(limits, dict):
    for key in ('depth', 'children', 'nodes'):
        if type(limits.get(key)) is int:
            print('--split-%s\t%d' % (key, limits[key]))
PY
)
FORGE_POOLS
  note "next: planning stage $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("stage", "?"))' "$new/chain.json" 2>/dev/null) in $new, from the integration branch of $PLAN"
  /bin/bash "$SELF" auto "$new" --repo "$repo" --goal-file "$new/request.md" --plan always --planner "$PLANNER" \
    --after "$PLAN" ${pools[@]+"${pools[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}
}
