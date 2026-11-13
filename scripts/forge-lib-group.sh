#!/usr/bin/env bash
# Group review for forge-parallel.sh (#140). Sourced; functions only.
#
#   forge-parallel.sh _group <plan> <gid>
#
# Opt-in: a `review=group` group of <plan>/groups.tsv is reviewed ONCE, over the work of all its
# members together. Its members stopped at IMPLEMENTED (reviewed.commit, source.fingerprint). This builds
# a candidate (the integration worktree's HEAD plus each member's reviewed.commit, merged in member
# order), bounds its diff, gives it one QA dispatch, and attributes the findings to the members.
# It writes the group's outcome files and changes no member's state, except INVALIDATED for a member that
# moved. Settling a group and merging its members is group-settle's.
#
# Files in <plan>/groups/<gid>/ (every status write is atomic):
#   status           REVIEWING while it runs, then PASS FAIL NEEDS_DECISION UNKNOWN CONFLICT INFRA
#                    TIMEOUT ERROR INVALIDATED or WAITING
#   round            the round number, 1 for the first review
#   candidate.base   the integration HEAD the candidate starts from   candidate.commit  its tip
#   members.tsv      round, task, reviewed commit, attempt: one row per member per round that built a candidate
#   fingerprint      sha256 of the sorted task=commit lines and a final tree=<candidate tree> line
#   conflict.files   the files a conflicting member merge left unmerged (status CONFLICT)
#   combined.diff    the candidate's whole diff; decision.md  why a round stopped for a person
#   qa.input qa.last qa.out qa.gate known_issues.md infra.txt   as a task's review files
#   attribution.json the findings attributed to members, with the round   members.paths  task<TAB>path
#   qa.round-<n>.last the reply of round n, which the next round's prompt quotes from
#   rounds.tsv       round, epoch, status, implicated (comma list or -), qa spec
# The candidate worktree is <wt_root>/_group-<gid>; it stays (a later merge uses candidate.commit).

group_set_status() { # group_set_status <group dir> <status>: atomic
  local tmp="$1/.status.$$.tmp"
  printf '%s\n' "$2" > "$tmp" && mv -f "$tmp" "$1/status"
}

# The round's status, and the row that says so. Every way out of a round after its status was set goes
# through here, so rounds.tsv has one line per round.
group_end() { # group_end <group dir> <round> <status> <qa spec> [implicated comma list]
  local gdir="$1" round="$2" status="$3" qa="$4" implicated="${5:-}"
  group_set_status "$gdir" "$status"
  printf '%s\t%s\t%s\t%s\t%s\n' "$round" "$(date +%s)" "$status" "${implicated:--}" "$qa" >> "$gdir/rounds.tsv"
}

group_size_limit() { # group_size_limit <plan> -> the diff size bound in KB
  local kb
  kb="$(head -1 "$1/group_max_diff_kb" 2>/dev/null | tr -dc '0-9')"
  [ -n "$kb" ] && [ "$kb" -gt 0 ] 2>/dev/null || kb=256
  printf '%s\n' "$kb"
}

group_candidate_remove() { # group_candidate_remove <plan> <repo> <path>: any earlier candidate, registered or not
  local plan="$1" repo="$2" path="$3"
  git -C "$repo" worktree prune >/dev/null 2>&1
  [ -e "$path" ] || [ -L "$path" ] || return 0
  if git -C "$repo" worktree remove --force "$path" >/dev/null 2>&1; then
    forge_worktree_event removed "$plan" "$repo" "$path" - candidate
  fi
  rm -rf "$path"
  git -C "$repo" worktree prune >/dev/null 2>&1
}

do_group() (
  local PLAN="${1:?usage: forge-parallel.sh _group <plan> <gid>}" gid="${2:?usage: forge-parallel.sh _group <plan> <gid>}"
  [ -d "$PLAN" ] || die "$PLAN is not a plan directory" 2
  local G="$PLAN/groups/$gid" row review qa members
  row="$(forge_groups_py show "$PLAN" 2>/dev/null | awk -F'\t' -v g="$gid" '$1 == g { print; exit }')"
  [ -n "$row" ] || die "$gid is not a group of $PLAN" 2
  IFS=$'\t' read -r _ review qa members _ <<< "$row"
  [ "$review" = group ] || die "group $gid has review=$review: only a review=group group is reviewed as one" 2

  # 1. Status and round.
  mkdir -p "$G" || die "could not create $G" 3
  local round REPO run_id wt_root
  round="$(tr -dc '0-9' < "$G/round" 2>/dev/null)"
  round=$(( ${round:-0} + 1 ))
  printf '%s\n' "$round" > "$G/round.tmp" && mv -f "$G/round.tmp" "$G/round"
  # What an earlier round left that this one would otherwise be read as. attribution.json and
  # qa.round-<n>.last stay: the next prompt quotes them.
  rm -f "$G/qa.last" "$G/qa.input" "$G/qa.out" "$G/qa.gate" "$G/known_issues.md" "$G/infra.txt" "$G/decision.md" \
        "$G/conflict.files" "$G/candidate.base" "$G/candidate.commit" "$G/fingerprint" "$G/combined.diff" \
        "$G/merge.out" "$G/members.paths" "$G/review.base"
  group_set_status "$G" REVIEWING
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"; wt_root="$(cat "$PLAN/wt_root")"
  # --timeout given to run/retry is kept in the plan and wins over the environment, as for a task.
  [ -s "$PLAN/timeout" ] && export FORGE_TIMEOUT="$(cat "$PLAN/timeout")"
  local ids=() id tdir st waiting=0 reviewed moved=0
  IFS=, read -ra ids <<< "$members"

  # 2. The members: each one IMPLEMENTED, still at the commit and the fingerprint it was judged at.
  for id in "${ids[@]}"; do
    st="$(task_status "$PLAN" "$id")"
    if [ "$st" != IMPLEMENTED ]; then
      note "group $gid: $id is $st, not IMPLEMENTED — waiting"
      waiting=1
    fi
  done
  if [ "$waiting" = 1 ]; then group_end "$G" "$round" WAITING "$qa"; return 0; fi
  for id in "${ids[@]}"; do
    tdir="$PLAN/tasks/$id"
    reviewed="$(cat "$tdir/reviewed.commit" 2>/dev/null)"
    if [ -z "$reviewed" ] \
       || [ "$(git -C "$REPO" rev-parse --verify --quiet "forge/$run_id/$id" 2>/dev/null)" != "$reviewed" ] \
       || [ "$(forge_fingerprint "$wt_root/$id" 2>/dev/null)" != "$(cat "$tdir/source.fingerprint" 2>/dev/null)" ]; then
      echo group-candidate > "$tdir/invalidated.reason"
      echo INVALIDATED > "$tdir/status"
      note "group $gid: $id moved since it was implemented — invalidated"
      moved=1
    fi
  done
  if [ "$moved" = 1 ]; then group_end "$G" "$round" WAITING "$qa"; return 0; fi

  # 3. The candidate: the integration HEAD plus every member's reviewed commit, merged in member order.
  local cand="$wt_root/_group-$gid" base commit
  base="$(git -C "$wt_root/_integration" rev-parse HEAD 2>/dev/null)" || base=""
  if [ -z "$base" ]; then
    note "group $gid: the integration worktree has no HEAD"
    group_end "$G" "$round" ERROR "$qa"; return 0
  fi
  group_candidate_remove "$PLAN" "$REPO" "$cand"
  if ! git -C "$REPO" worktree add --detach "$cand" "$base" > "$G/merge.out" 2>&1; then
    note "group $gid: could not create the candidate worktree — see $G/merge.out"
    group_end "$G" "$round" ERROR "$qa"; return 0
  fi
  forge_worktree_event created "$PLAN" "$REPO" "$cand" - candidate
  printf '%s\n' "$base" > "$G/candidate.base"
  for id in "${ids[@]}"; do
    if ! git -C "$cand" -c user.name=forge -c user.email=forge@local \
         merge --no-ff -m "forge: group $gid candidate: $id" "$(cat "$PLAN/tasks/$id/reviewed.commit")" >> "$G/merge.out" 2>&1; then
      git -C "$cand" diff --name-only --diff-filter=U > "$G/conflict.files" 2>/dev/null
      git -C "$cand" merge --abort >/dev/null 2>&1
      if [ -s "$G/conflict.files" ]; then
        note "group $gid: $id conflicts with the members before it: $(tr '\n' ' ' < "$G/conflict.files")"
        group_end "$G" "$round" CONFLICT "$qa"
      else
        rm -f "$G/conflict.files"
        note "group $gid: merging $id failed — see $G/merge.out"
        group_end "$G" "$round" ERROR "$qa"
      fi
      return 0
    fi
  done
  commit="$(git -C "$cand" rev-parse HEAD)"
  printf '%s\n' "$commit" > "$G/candidate.commit"
  for id in "${ids[@]}"; do
    printf '%s\t%s\t%s\t%s\n' "$round" "$id" "$(cat "$PLAN/tasks/$id/reviewed.commit")" "$(cat "$PLAN/tasks/$id/attempt" 2>/dev/null || echo 0)"
  done >> "$G/members.tsv"
  {
    for id in "${ids[@]}"; do printf '%s=%s\n' "$id" "$(cat "$PLAN/tasks/$id/reviewed.commit")"; done | LC_ALL=C sort
    printf 'tree=%s\n' "$(git -C "$cand" rev-parse "$commit^{tree}")"
  } | python3 -I -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())' > "$G/fingerprint"

  # 4. The size bound. A group over it is not reviewed, cut down or sent elsewhere: a person decides.
  git -C "$cand" diff --binary --irreversible-delete "$base" "$commit" -- . ':(exclude).forge' > "$G/combined.diff" 2>/dev/null \
    || { note "group $gid: could not read the candidate's diff"; group_end "$G" "$round" ERROR "$qa"; return 0; }
  local diff_bytes limit
  diff_bytes="$(wc -c < "$G/combined.diff" | tr -d ' ')"
  limit="$(group_size_limit "$PLAN")"
  if [ "$diff_bytes" -gt $(( limit * 1024 )) ]; then
    python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" decision "$PLAN" "$gid" oversized --size "$diff_bytes" --limit "$limit" \
        > "$G/decision.md.tmp" && mv -f "$G/decision.md.tmp" "$G/decision.md" \
      || { note "group $gid: could not write decision.md"; group_end "$G" "$round" ERROR "$qa"; return 0; }
    note "group $gid: the combined diff is $diff_bytes bytes, over the $limit KB limit — stopped for your decision, see $G/decision.md"
    group_end "$G" "$round" NEEDS_DECISION "$qa"; return 0
  fi

  # 5. The review snapshot, removed on every way out.
  local snap="$G/review-$round-$$" stale cand_fp snap_fp thr
  for stale in "$G"/review-*; do forge_drop_snapshot "$stale"; done
  trap 'forge_drop_snapshot "$snap"' EXIT
  trap 'exit 143' TERM INT
  if ! forge_review_snapshot "$cand" "$base" "$commit" "$snap"; then
    note "group $gid: could not make the review snapshot"
    group_end "$G" "$round" ERROR "$qa"; return 0
  fi
  cand_fp="$(forge_fingerprint "$cand")" || { group_end "$G" "$round" ERROR "$qa"; return 0; }
  # A commit: an untracked file the reviewer leaves behind merges nothing, so only tracked content counts (#95).
  snap_fp="$(forge_fingerprint "$snap" --tracked)" || { group_end "$G" "$round" ERROR "$qa"; return 0; }

  # 6. The prompt.
  thr="$(qa_threshold_for "$PLAN" "")"
  if ! python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" prompt "$PLAN" "$gid" --threshold "$thr" > "$G/qa.input.tmp" \
     || ! mv -f "$G/qa.input.tmp" "$G/qa.input"; then
    rm -f "$G/qa.input.tmp"
    note "group $gid: could not build the review prompt"
    group_end "$G" "$round" ERROR "$qa"; return 0
  fi
  local prompt_bytes
  prompt_bytes="$(wc -c < "$G/qa.input" | tr -d ' ')"
  if [ -z "$qa" ] || [ "$qa" = - ]; then
    note "group $gid: no qa spec in groups.tsv; run plan again"
    group_end "$G" "$round" ERROR "$qa"; return 0
  fi

  # 7. One QA dispatch, as a task's.
  local yq="" ft_qa rc
  [ -f "$PLAN/yolo_qa" ] && yq="--yolo"
  ft_qa="$(forge_fast_tier "$PLAN" qa)"
  /bin/bash "$DISPATCH" qa "$qa" --repo "$snap" --run-dir "$G" --review-base "$(cat "$G/review.base")" \
      --prompt-file "$G/qa.input" ${yq:+"$yq"} $ft_qa --output "${FORGE_OUTPUT:-summary}" > "$G/qa.out" 2>&1
  rc=$?
  if [ "${FORGE_OUTPUT:-summary}" = full ]; then cat "$G/qa.out"; fi
  if [ "$rc" -ne 0 ]; then
    case "$rc" in
      7) note "group $gid: qa hit the dispatch timeout — see $G/qa.out"; group_end "$G" "$round" TIMEOUT "$qa" ;;
      8) { cp "$G/qa.infra" "$G/infra.txt" 2>/dev/null || : > "$G/infra.txt"; printf 'stage=qa\n' >> "$G/infra.txt"; }
         note "group $gid: qa hit an infrastructure failure ($(sed -n 's/^class=//p' "$G/infra.txt" | head -1)) — see $G/infra.txt"
         group_end "$G" "$round" INFRA "$qa" ;;
      *) note "group $gid: qa dispatch failed — see $G/qa.out"; group_end "$G" "$round" ERROR "$qa" ;;
    esac
    return 0
  fi

  # 8. A tracked change to the candidate or to the snapshot during the review means the reply may not
  # describe the commit under review.
  if [ "$(forge_fingerprint "$cand")" != "$cand_fp" ] || [ "$(forge_fingerprint "$snap" --tracked)" != "$snap_fp" ]; then
    note "group $gid: the candidate changed during its review"
    group_end "$G" "$round" INVALIDATED "$qa"; return 0
  fi

  # 9. The verdict, and the gate at the plan's threshold: it can accept a FAIL made only of tolerated
  # findings, and fail a PASS that lists a blocking one. No verdict is UNKNOWN, never a pass.
  local verdict line decision="" final gated=0
  verdict="$(forge_verdict "$G/qa.last" 2>/dev/null)"
  [ -n "$verdict" ] || verdict=UNKNOWN
  cp "$G/qa.last" "$G/qa.round-$round.last" 2>/dev/null || true
  line="$(python3 "$SKILL_DIR/scripts/forge-contract.py" gate --verdict "$verdict" --qa-last "$G/qa.last" \
      --threshold "$thr" --known-issues "$G/known_issues.md" 2>/dev/null)" || line=""
  if [ -n "$line" ]; then
    printf '%s\t%s\n' "$thr" "$line" > "$G/qa.gate"
    decision="${line%%$'\t'*}"
  fi
  case "$decision" in accept|fail) gated=1 ;; esac
  if [ "$verdict" = UNKNOWN ]; then final=UNKNOWN
  elif [ "$decision" = accept ] || { [ "$verdict" = PASS ] && [ "$decision" != fail ]; }; then final=PASS
  else final=FAIL; fi

  # 10. Attribution: each finding to the members whose diff changed the file it names.
  local implicated="-" ambiguous="-" findings=0 facts
  for id in "${ids[@]}"; do
    git -C "$REPO" -c core.quotepath=off diff --name-only "$(cat "$PLAN/tasks/$id/base_ref")" \
        "$(cat "$PLAN/tasks/$id/reviewed.commit")" -- . ':(exclude).forge' | awk -v t="$id" '{ print t "\t" $0 }'
  done > "$G/members.paths"
  if [ -f "$G/qa.last" ]; then
    if python3 -I "$SKILL_DIR/scripts/forge_groups/attribute.py" --qa-last "$G/qa.last" --members "$G/members.paths" \
         --threshold "$thr" > "$G/attribution.json.tmp" 2> "$G/attribution.err" \
       && python3 -I -c 'import json, sys; d = json.load(sys.stdin); d["round"] = int(sys.argv[1]); print(json.dumps(d, indent=2, ensure_ascii=False))' \
         "$round" < "$G/attribution.json.tmp" > "$G/attribution.json.new" \
       && mv -f "$G/attribution.json.new" "$G/attribution.json"; then
      rm -f "$G/attribution.json.tmp" "$G/attribution.err"
      facts="$(python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" facts "$G/attribution.json")" || facts=""
      IFS=$'\t' read -r implicated ambiguous findings _ <<< "$facts"
      : "${implicated:=-}" "${ambiguous:=-}" "${findings:=0}"
    else
      rm -f "$G/attribution.json.tmp" "$G/attribution.json.new"
      note "group $gid: could not attribute the findings — see $G/attribution.err"
      group_end "$G" "$round" ERROR "$qa"; return 0
    fi
  fi
  if [ "$final" = FAIL ] && [ "$ambiguous" != - ]; then
    if python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" decision "$PLAN" "$gid" ambiguous --attribution "$G/attribution.json" \
         > "$G/decision.md.tmp" && mv -f "$G/decision.md.tmp" "$G/decision.md"; then
      final=NEEDS_DECISION
      note "group $gid: a blocking finding names no member task — stopped for your decision, see $G/decision.md"
    else
      rm -f "$G/decision.md.tmp"
      note "group $gid: could not write decision.md; the group stays FAIL"
    fi
  fi

  # 11. The round's records.
  group_end "$G" "$round" "$final" "$qa" "$([ "$implicated" = - ] || printf '%s' "$implicated")"
  forge_dataset_event group_review --dir "$PLAN" --set group="$gid" --set round="$round" \
      --set members="$members" --set reviewer="$qa" --set verdict="$verdict" --set gated="$gated" \
      --set implicated="$([ "$implicated" = - ] || printf '%s' "$implicated")" \
      --set ambiguous="$([ "$ambiguous" = - ] || printf '%s' "$ambiguous")" \
      --set findings="$findings" --set diff_bytes="$diff_bytes" --set prompt_bytes="$prompt_bytes"
  return 0
)
