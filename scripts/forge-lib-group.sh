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
# moved. What the outcome does to the members is the second half of this file:
#
#   forge-parallel.sh _group_settle <plan> <gid> [--tasks a,b]   a FAIL sends its implicated members back
#   forge-parallel.sh _merge_group <plan> <gid>                  a PASS merges every member, or none
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
# Written by _group_settle: settled.round (the round it settled), settle.tasks (round<TAB>ids a person named,
# kept until the round is settled), decision.<round>.md (decision.md once a person answered).
# Written by _merge_group: merged (the group's), invalidated.reason (integration-moved, integration-dirty,
# candidate-moved), conflict.files.

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
        "$G/merge.out" "$G/members.paths" "$G/review.base" "$G/invalidated.reason"
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
      group_set_status "$tdir" INVALIDATED
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

# --- settling a group's outcome (#140) ------------------------------------------------------------

# A member's qa.last for one group round: a header, then the findings attributed to it (and, on a decision,
# the ambiguous ones a person gave it), then a verdict line. The blocks are the parser's own texts, in the
# group's order, so every one of them is a block of the group's qa.last. A finding that names other members
# too is preceded by `Also involves: <the others>` on a line of its own, which no finding block starts at.
group_member_report() { # group_member_report <group dir> <id> <members comma list> <verdict> [assigned ns [named ids]]
  python3 -I - "$SKILL_DIR/scripts/forge_groups" "$@" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import attribute

gdir, me, members, verdict = Path(sys.argv[2]), sys.argv[3], [m for m in sys.argv[4].split(',') if m], sys.argv[5]
assigned = {int(n) for n in (sys.argv[6] if len(sys.argv) > 6 else '').split(',') if n.strip().isdigit()}
named = [t for t in (sys.argv[7] if len(sys.argv) > 7 else '').split(',') if t]
round_no = int((gdir / 'round').read_text().strip() or 0)
text = (gdir / 'qa.last').read_text(encoding='utf-8', errors='replace')
rows = None
try:
    data = json.loads((gdir / 'attribution.json').read_text(encoding='utf-8'))
    if data.get('round') == round_no:
        rows = data['findings']
except (OSError, ValueError, KeyError):
    pass
blocks = attribute._contract().parse_findings(text)
if rows is None or len(rows) != len(blocks):
    sys.stderr.write('the attribution does not describe the review of round %d\n' % round_no)
    sys.exit(3)
out = ['Group %s, round %d: one review of %s. The findings below are the ones attributed to %s.'
       % (gdir.name, round_no, ', '.join(members), me)]
count = 0
for row, finding in zip(rows, blocks):
    tasks = row.get('tasks') or []
    note = None
    if me in tasks:
        others = [t for t in tasks if t != me]
        if others:
            note = 'Also involves: ' + ', '.join(others)
    elif row.get('n') in assigned and me in named:
        others = [t for t in named if t != me]
        note = ('Also involves: ' + ', '.join(others)) if others else \
            'Not attributed to a member task by the review; assigned to %s by a decision.' % me
    else:
        continue
    count += 1
    out.append('')
    if note:
        out.append(note)
    out.append(finding.text)
if not count:
    out += ['', 'No finding is attributed to %s.' % me]
out += ['', 'FORGE_VERDICT: ' + verdict]
sys.stdout.write('\n'.join(out) + '\n')
PY
}

group_infra_class() { sed -n 's/^class=//p' "$1/infra.txt" 2>/dev/null | head -1; }

# Which of this group's rounds a status leaves to a person or to the scheduler: the line a status that
# settles nothing prints.
group_status_line() { # group_status_line <gid> <group dir> <status>
  local gid="$1" G="$2" st="$3"
  case "$st" in
    PASS)          echo "group $gid: PASS — nothing to send back; merge it with _merge_group" ;;
    NEEDS_DECISION) echo "group $gid: NEEDS_DECISION — see $G/decision.md; name who fixes it with _group_settle $PLAN $gid --tasks <id,id>" ;;
    UNKNOWN)       echo "group $gid: UNKNOWN — the reviewer gave no verdict; review the group again" ;;
    CONFLICT)      echo "group $gid: CONFLICT — its members conflict ($(tr '\n' ' ' 2>/dev/null < "$G/conflict.files" || true)); a member must change" ;;
    INFRA)         echo "group $gid: INFRA — infrastructure failure ($(group_infra_class "$G")); no attempt spent, review the group again" ;;
    TIMEOUT)       echo "group $gid: TIMEOUT — the review hit the dispatch timeout; review the group again" ;;
    ERROR)         echo "group $gid: ERROR — the review could not run, see $G/merge.out and $G/qa.out" ;;
    INVALIDATED)   echo "group $gid: INVALIDATED — $(head -1 "$G/invalidated.reason" 2>/dev/null || echo 'the candidate changed during its review'); review the group again" ;;
    WAITING)       echo "group $gid: WAITING — a member is not IMPLEMENTED yet" ;;
    REVIEWING)     echo "group $gid: REVIEWING — a review is running" ;;
    *)             echo "group $gid: ${st:-no status} — the group was not reviewed" ;;
  esac
}

do_group_settle() (
  local usage="usage: forge-parallel.sh _group_settle <plan> <gid> [--tasks a,b]"
  local PLAN="${1:?$usage}" gid="${2:?$usage}" named="" G row review members id st round verdict thr
  shift 2
  while [ $# -gt 0 ]; do
    case "$1" in
      --tasks) [ $# -ge 2 ] || die "--tasks needs a comma list of member tasks" 2; named="$2"; shift 2 ;;
      --tasks=*) named="${1#--tasks=}"; shift ;;
      *) die "unknown option '$1'. $usage" 2 ;;
    esac
  done
  [ -d "$PLAN" ] || die "$PLAN is not a plan directory" 2
  G="$PLAN/groups/$gid"
  row="$(forge_groups_py show "$PLAN" 2>/dev/null | awk -F'\t' -v g="$gid" '$1 == g { print; exit }')"
  [ -n "$row" ] || die "$gid is not a group of $PLAN" 2
  IFS=$'\t' read -r _ review _ members _ <<< "$row"
  [ "$review" = group ] || die "group $gid has review=$review: only a review=group group is settled as one" 2
  local ids=() want=() n
  IFS=, read -ra ids <<< "$members"
  # Who a person named: members, written once, in member order.
  if [ -n "$named" ]; then
    local given=() g
    IFS=, read -ra given <<< "${named// /}"
    [ ${#given[@]} -gt 0 ] || die "--tasks names no task" 2
    for g in "${given[@]}"; do
      [ -n "$g" ] || die "--tasks has an empty task id" 2
      case ",$members," in *",$g,"*) ;; *) die "--tasks: $g is not a member of group $gid ($members)" 2 ;; esac
    done
    for id in "${ids[@]}"; do case ",${named// /}," in *",$id,"*) want+=("$id") ;; esac; done
  fi
  round="$(tr -dc '0-9' < "$G/round" 2>/dev/null)"
  [ -n "$round" ] || die "group $gid has not been reviewed" 2
  # One settle per round, however often it is asked.
  [ "$(cat "$G/settled.round" 2>/dev/null)" = "$round" ] && return 0
  st="$(head -1 "$G/status" 2>/dev/null | tr -d '[:space:]')"

  # A person's answer to a decision: the findings that name no member go to the tasks they named.
  local pending="" assigned=""
  if [ -n "$named" ]; then
    local kept
    kept="$(awk -F'\t' -v r="$round" '$1 == r { print $2; exit }' "$G/settle.tasks" 2>/dev/null)"
    if [ "$st" = FAIL ] && [ -n "$kept" ]; then
      # An answer that was interrupted: it settles as it was given.
      IFS=, read -ra want <<< "$kept"
    elif [ "$st" != NEEDS_DECISION ]; then
      die "group $gid is $st, not NEEDS_DECISION: --tasks answers a decision" 2
    fi
  elif [ "$st" = FAIL ]; then
    kept="$(awk -F'\t' -v r="$round" '$1 == r { print $2; exit }' "$G/settle.tasks" 2>/dev/null)"
    [ -z "$kept" ] || { IFS=, read -ra want <<< "$kept"; named="$kept"; }
  fi
  if [ "$st" != FAIL ] && [ "$st" != NEEDS_DECISION ] || { [ "$st" = NEEDS_DECISION ] && [ -z "$named" ]; }; then
    note "$(group_status_line "$gid" "$G" "$st")"
    return 0
  fi
  local facts implicated ambiguous named_ids=()
  if [ -n "$named" ]; then
    facts="$(python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" facts "$G/attribution.json" 2>/dev/null)" || facts=""
    IFS=$'\t' read -r implicated ambiguous _ <<< "$facts"
    if [ "$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1])).get("round"))' "$G/attribution.json" 2>/dev/null)" != "$round" ] \
       || [ -z "${ambiguous:-}" ] || [ "$ambiguous" = - ]; then
      die "group $gid has no blocking finding that names no member in round $round: nothing for --tasks to assign" 2
    fi
    assigned="$ambiguous"
    # The members whose own blocking findings the review attributed go back too, named or not: left
    # IMPLEMENTED, the next group round would only find the same thing again.
    named_ids=("${want[@]}")
    if [ -n "${implicated:-}" ] && [ "$implicated" != - ]; then
      local union=()
      for id in "${ids[@]}"; do
        case ",${named// /},$implicated," in *",$id,"*) union+=("$id") ;; esac
      done
      want=("${union[@]}")
    fi
  else
    facts="$(python3 -I "$SKILL_DIR/scripts/forge_groups/review.py" facts "$G/attribution.json" 2>/dev/null)" || facts=""
    IFS=$'\t' read -r implicated ambiguous _ <<< "$facts"
    if [ -z "${implicated:-}" ] || [ "$implicated" = - ]; then
      note "group $gid: FAIL, but no member is implicated — nothing to send back"
      return 0
    fi
    IFS=, read -ra want <<< "$implicated"
    # Member order, whatever order the attribution listed them in.
    local ordered=()
    for id in "${ids[@]}"; do case ",$implicated," in *",$id,"*) ordered+=("$id") ;; esac; done
    if [ ${#ordered[@]} -eq 0 ]; then
      note "group $gid: FAIL, but the implicated tasks ($implicated) are not members — nothing to send back"
      return 0
    fi
    want=("${ordered[@]}")
    named=""
  fi

  # Every member's report is made before anything changes: one that cannot be made changes nothing.
  verdict="$(forge_verdict "$G/qa.last" 2>/dev/null)"; [ "$verdict" = PASS ] || [ "$verdict" = FAIL ] || verdict=FAIL
  thr="$(cut -f1 "$G/qa.gate" 2>/dev/null | head -1)"; [ -n "$thr" ] || thr="$(qa_threshold_for "$PLAN" "")"
  local named_csv=""; [ -z "$named" ] || named_csv="$(IFS=,; echo "${named_ids[*]}")"
  for id in "${want[@]}"; do
    if ! group_member_report "$G" "$id" "$members" FAIL "$assigned" "$named_csv" > "$PLAN/tasks/$id/.qa.last.settle.tmp" 2> "$G/settle.err"; then
      for n in "${want[@]}"; do rm -f "$PLAN/tasks/$n/.qa.last.settle.tmp"; done
      note "group $gid: could not write $id's findings — $(head -1 "$G/settle.err") (see $G/attribution.json)"
      return 3
    fi
  done
  rm -f "$G/settle.err"

  # A decision is answered: the status says FAIL first, and decision.md is kept as the round's.
  if [ -n "$named" ] && [ "$st" = NEEDS_DECISION ]; then
    printf '%s\t%s\n' "$round" "$named_csv" > "$G/settle.tasks.tmp" && mv -f "$G/settle.tasks.tmp" "$G/settle.tasks"
    group_set_status "$G" FAIL
    [ ! -f "$G/decision.md" ] || mv -f "$G/decision.md" "$G/decision.$round.md"
  fi
  local tdir
  for id in "${want[@]}"; do
    tdir="$PLAN/tasks/$id"
    if [ "$(cat "$tdir/settled.group" 2>/dev/null)" = "$gid $round" ]; then rm -f "$tdir/.qa.last.settle.tmp"; continue; fi
    mv -f "$tdir/.qa.last.settle.tmp" "$tdir/qa.last"
    qa_gate "$PLAN" "$id" "$tdir" "$verdict" "$thr"
    echo FAIL > "$tdir/status"
    record_judged_attempt "$PLAN" "$id" FAIL
    post_attempt "$PLAN" "$id" || true
    printf '%s %s\n' "$gid" "$round" > "$tdir/settled.group"
  done
  printf '%s\n' "$round" > "$G/settled.round.tmp" && mv -f "$G/settled.round.tmp" "$G/settled.round"
  rm -f "$G/settle.tasks"
  note "group $gid: round $round sent back to $(IFS=,; echo "${want[*]}")"
  return 0
)

# --- merging a group (#140) ------------------------------------------------------------------------

group_invalidate() { # group_invalidate <plan> <gid> <reason>: the group's status, its reason, and the dataset's row
  printf '%s\n' "$3" > "$1/groups/$2/invalidated.reason"
  group_set_status "$1/groups/$2" INVALIDATED
  forge_dataset_event group_merge --dir "$1" --set group="$2" --set status=INVALIDATED --set reason="$3"
}

# The fingerprint do_group wrote, from what is on disk now: the members' commits of the round, sorted, and
# the candidate's tree.
group_recompute_fingerprint() { # group_recompute_fingerprint <plan> <gid> <round> <repo>
  local G="$1/groups/$2" commit
  commit="$(cat "$G/candidate.commit" 2>/dev/null)"
  {
    awk -F'\t' -v r="$3" '$1 == r { print $2 "=" $3 }' "$G/members.tsv" 2>/dev/null | LC_ALL=C sort
    printf 'tree=%s\n' "$(git -C "$4" rev-parse --verify --quiet "$commit^{tree}" 2>/dev/null)"
  } | python3 -I -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

merge_group() { # merge_group <plan> <gid> -> 0 merged, 1 not
  local usage="usage: forge-parallel.sh _merge_group <plan> <gid>"
  local PLAN="${1:?$usage}" gid="${2:?$usage}" G REPO run_id int_wt row review members st round id
  [ -d "$PLAN" ] || die "$PLAN is not a plan directory" 2
  G="$PLAN/groups/$gid"
  row="$(forge_groups_py show "$PLAN" 2>/dev/null | awk -F'\t' -v g="$gid" '$1 == g { print; exit }')"
  [ -n "$row" ] || die "$gid is not a group of $PLAN" 2
  IFS=$'\t' read -r _ review _ members _ <<< "$row"
  [ "$review" = group ] || die "group $gid has review=$review: only a review=group group is merged as one" 2
  if [ -f "$G/merged" ]; then note "group $gid: already merged"; return 0; fi
  st="$(head -1 "$G/status" 2>/dev/null | tr -d '[:space:]')"
  if [ "$st" != PASS ]; then note "group $gid: ${st:-not reviewed}, not PASS — nothing to merge"; return 1; fi
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  int_wt="$(cat "$PLAN/wt_root")/_integration"
  round="$(tr -dc '0-9' < "$G/round" 2>/dev/null)"
  local ids=()
  IFS=, read -ra ids <<< "$members"

  # merge_task's guards.
  if [ -s "$PLAN/accepted.integration" ] && [ "$(git -C "$int_wt" rev-parse HEAD)" != "$(cat "$PLAN/accepted.integration")" ]; then
    group_invalidate "$PLAN" "$gid" integration-moved
    note "group $gid: the integration branch moved outside this run. To bring those commits into the run: forge-parallel.sh adopt $PLAN --head --reason \"...\""
    return 1
  fi
  if [ -n "$(git -C "$int_wt" status --porcelain -- . ':(exclude).forge')" ]; then
    group_invalidate "$PLAN" "$gid" integration-dirty
    return 1
  fi
  local dep
  for id in "${ids[@]}"; do
    for dep in $(field "$PLAN/tasks.tsv" "$id" deps | tr ',' ' '); do
      [ "$dep" = - ] && continue
      if [ "$(task_status "$PLAN" "$dep")" != MERGED ]; then
        note "group $gid: $id is BLOCKED by $dep ($(task_status "$PLAN" "$dep"))"
        return 1
      fi
    done
  done

  # The candidate and the members are what the review judged.
  local moved=() off=0 reviewed head commit
  for id in "${ids[@]}"; do
    reviewed="$(awk -F'\t' -v r="$round" -v t="$id" '$1 == r && $2 == t { c = $3 } END { print c }' "$G/members.tsv" 2>/dev/null)"
    head="$(git -C "$REPO" rev-parse --verify --quiet "forge/$run_id/$id" 2>/dev/null)"
    if [ -z "$reviewed" ] || [ "$head" != "$reviewed" ] \
       || [ "$(forge_fingerprint "$(cat "$PLAN/wt_root")/$id" 2>/dev/null)" != "$(cat "$PLAN/tasks/$id/source.fingerprint" 2>/dev/null)" ]; then
      moved+=("$id")
    elif [ "$(task_status "$PLAN" "$id")" != IMPLEMENTED ]; then
      off=1
    fi
  done
  commit="$(cat "$G/candidate.commit" 2>/dev/null)"
  if [ ${#moved[@]} -gt 0 ]; then
    for id in "${moved[@]}"; do
      echo group-candidate > "$PLAN/tasks/$id/invalidated.reason"
      echo INVALIDATED > "$PLAN/tasks/$id/status"
    done
    group_invalidate "$PLAN" "$gid" candidate-moved
    note "group $gid: ${moved[*]} moved since the review — nothing merged"
    return 1
  fi
  if [ "$off" = 1 ] || [ -z "$commit" ] \
     || [ "$(group_recompute_fingerprint "$PLAN" "$gid" "$round" "$REPO")" != "$(cat "$G/fingerprint" 2>/dev/null)" ]; then
    group_invalidate "$PLAN" "$gid" candidate-moved
    note "group $gid: the candidate is not what was reviewed — nothing merged"
    return 1
  fi

  # Each member's report, made before the merge: one that cannot be made does not leave a merge half recorded.
  local thr verdict ids_csv
  thr="$(cut -f1 "$G/qa.gate" 2>/dev/null | head -1)"; [ -n "$thr" ] || thr="$(qa_threshold_for "$PLAN" "")"
  ids_csv="$(IFS=,; echo "${ids[*]}")"
  for id in "${ids[@]}"; do
    group_member_report "$G" "$id" "$members" PASS > "$PLAN/tasks/$id/.qa.last.merge.tmp" 2>/dev/null || {
      printf 'Group %s, round %s: one review of %s. The findings below are the ones attributed to %s.\n\n' \
        "$gid" "$round" "${members//,/, }" "$id"
      printf '(The review could not be attributed; the whole of it follows.)\n\n'
      cat "$G/qa.last" 2>/dev/null
      printf '\nFORGE_VERDICT: PASS\n'
    } > "$PLAN/tasks/$id/.qa.last.merge.tmp"
  done

  if ! git -C "$int_wt" merge --no-ff -m "forge: merge group $gid ($ids_csv)" "$commit" >/dev/null 2>&1; then
    # Which files collided is kept, and the abort erases it. No member is merged: nothing is promoted partly.
    git -C "$int_wt" diff --name-only --diff-filter=U > "$G/conflict.files" 2>/dev/null
    git -C "$int_wt" merge --abort >/dev/null 2>&1
    for id in "${ids[@]}"; do rm -f "$PLAN/tasks/$id/.qa.last.merge.tmp"; echo CONFLICT > "$PLAN/tasks/$id/status"; done
    group_set_status "$G" CONFLICT
    forge_dataset_event group_merge --dir "$PLAN" --set group="$gid" --set status=CONFLICT --set commit="$commit" \
      --set conflicts="$(tr '\n' ' ' < "$G/conflict.files" 2>/dev/null)"
    note "group $gid: conflicts with the integration branch: $(tr '\n' ' ' < "$G/conflict.files" 2>/dev/null)"
    return 1
  fi
  git -C "$int_wt" rev-parse HEAD > "$PLAN/accepted.integration"
  local tdir integration
  integration="$(cat "$PLAN/accepted.integration")"
  for id in "${ids[@]}"; do
    tdir="$PLAN/tasks/$id"
    touch "$tdir/merged"
    rm -f "$tdir/conflict.files"
    mv -f "$tdir/.qa.last.merge.tmp" "$tdir/qa.last"
    qa_gate "$PLAN" "$id" "$tdir" PASS "$thr"
    echo PASS > "$tdir/status"
    record_judged_attempt "$PLAN" "$id" PASS
    [ ! -f "$tdir/sweep.json" ] || python3 "$SKILL_DIR/scripts/forge-sweep.py" settle "$PLAN" "$id" >/dev/null 2>&1 || true
  done
  rm -f "$G/conflict.files" "$G/invalidated.reason"
  touch "$G/merged"
  forge_dataset_event group_merge --dir "$PLAN" --set group="$gid" --set status=MERGED --set commit="$commit" \
    --set members="$members" --set integration="$integration"
  for id in "${ids[@]}"; do
    forge_dataset_event merge --dir "$PLAN" --task "$id" --set status=MERGED \
      --set commit="$(awk -F'\t' -v r="$round" -v t="$id" '$1 == r && $2 == t { c = $3 } END { print c }' "$G/members.tsv")" \
      --set integration="$integration" --set group="$gid"
  done
  note "group $gid: $ids_csv merged"
  # With brief on, flag each member's dependents here, for the scheduler and a direct call alike (the
  # scheduler's merge_group does not repeat it).
  for id in "${ids[@]}"; do forge_brief_stale "$PLAN" "$id"; done
  return 0
}
