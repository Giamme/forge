#!/usr/bin/env bash
# Per-task helpers for forge-parallel.sh: attempt accounting, infrastructure stops,
# QA-only resume, retry preparation, implementer guards, the severity gate and the
# report blocks printed under the results table.
#
# Sourced by forge-parallel.sh, which provides die/note/field/all_ids/task_status and
# SKILL_DIR. Functions only; nothing runs at source time.

# --- attempt log ----------------------------------------------------------------
# One row per do_task entry: epoch, attempt number, resulting status, infra class.
# `attempt` itself only counts attempts that were really spent (an infrastructure stop
# refunds it), so this file is the audit trail of what happened, including the refunds.
record_attempt() { # record_attempt <plan> <id>
  local tdir="$1/tasks/$2" cls=""
  [ "$(task_status "$1" "$2")" = BLOCKED ] && return 0   # never started: nothing to audit
  [ "$(task_status "$1" "$2")" = INFRA ] && cls="$(sed -n 's/^class=//p' "$tdir/infra.txt" 2>/dev/null | head -1)"
  printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$(cat "$tdir/attempt" 2>/dev/null || echo 0)" \
    "$(task_status "$1" "$2")" "$cls" >> "$tdir/attempts.tsv"
}

# --- infrastructure stops -------------------------------------------------------
# A dispatch that failed because of quota, auth, a rate limit, the network or empty
# output says nothing about the work. The task is marked INFRA and the attempt is not
# spent. A dwarf-stage stop refunds the counter; a QA-stage stop leaves the dwarf's
# committed work intact, so a `resume` marker makes the next entry re-run QA only.
infra_stop() { # infra_stop <tdir> <id> <role> <attempt>
  local tdir="$1" id="$2" role="$3" attempt="$4" cls
  cp "$tdir/$role.infra" "$tdir/infra.txt" 2>/dev/null || : > "$tdir/infra.txt"
  printf 'stage=%s\n' "$role" >> "$tdir/infra.txt"
  cls="$(sed -n 's/^class=//p' "$tdir/infra.txt" | head -1)"
  echo INFRA > "$tdir/status"
  if [ "$role" = dwarf ]; then
    [ "$attempt" -gt 0 ] && echo $((attempt - 1)) > "$tdir/attempt"
    rm -f "$tdir/resume"
  else
    echo qa > "$tdir/resume"
  fi
  note "$id: $role dispatch hit an infrastructure failure (${cls:-unknown}) — no attempt spent; re-run to resume"
}

# A QA-only resume is trusted only while the worktree is still exactly what was
# reviewed. Anything else clears the marker and the full path runs.
resume_valid() { # resume_valid <tdir> <wt>
  local tdir="$1" wt="$2"
  [ "$(cat "$tdir/resume" 2>/dev/null)" = qa ] || return 1
  [ -s "$tdir/reviewed.commit" ] && [ -s "$tdir/source.fingerprint" ] && [ -s "$tdir/changes.diff" ] || return 1
  [ -e "$wt/.git" ] || return 1
  [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null)" = "$(cat "$tdir/reviewed.commit")" ] || return 1
  [ "$(forge_fingerprint "$wt")" = "$(cat "$tdir/source.fingerprint")" ]
}

# --- retry preparation ----------------------------------------------------------
# Shared by `retry` (a human asked) and the scheduler's automatic retries. The findings
# are the whole point of retrying rather than re-running: the dwarf is told what was
# wrong with its own previous attempt.
prepare_retry() { # prepare_retry <plan> <id> [new-dwarf]
  local PLAN="$1" id="$2" newdwarf="${3:-}" T="$1/tasks.tsv" tdir="$1/tasks/$2" st verdict
  st="$(task_status "$PLAN" "$id")"
  verdict="$(forge_verdict "$tdir/qa.last" 2>/dev/null)"
  if [ "$st" = INFRA ]; then
    # Nothing was judged, so there is nothing new to carry forward; the stop already
    # recorded where to resume (a `resume` marker for a QA-stage stop).
    note "$id: resuming after an infrastructure stop"
  elif [ "$st" = UNKNOWN ] && [ -s "$tdir/reviewed.commit" ]; then
    # The reviewer never reached a verdict, so the code was not rejected. Re-asking QA is
    # cheaper and more honest than paying an implementer to rewrite unjudged work; the
    # marker is revalidated against the worktree before it is believed.
    echo qa > "$tdir/resume"
    note "$id: the reviewer gave no verdict — re-running QA on the unchanged work"
  # Only a real review replaces the findings. The runner also writes explanatory text
  # into qa.last when it fails a task without dispatching QA ("produced no changes at
  # all"), and copying that over would throw away the actual findings the previous
  # reviewer gave — the one thing a retry exists to carry forward.
  elif [ "$verdict" = PASS ] || [ "$verdict" = FAIL ]; then
    cp "$tdir/qa.last" "$tdir/retry_findings.md"
  elif grep -q 'FORGE_VERDICT' "$tdir/qa.last" 2>/dev/null; then
    cp "$tdir/qa.last" "$tdir/retry_findings.md"
  elif [ -s "$tdir/retry_findings.md" ]; then
    note "$id: no new review since the last retry — reusing the previous findings"
  else
    note "$id: no previous QA findings to pass on (last status was $st)"
    : > "$tdir/retry_findings.md"
  fi
  if [ -n "$newdwarf" ]; then
    # Written into tasks.tsv rather than kept in a side file: the table then shows the
    # model that will actually be spent, and plan's rule that an explicit column value
    # survives a re-plan protects the escalation for free.
    awk -F'\t' -v OFS='\t' -v id="$id" -v dw="$newdwarf" \
      '$0 !~ /^#/ && $1==id { $5=dw } { print }' "$T" > "$PLAN/.tasks.tsv.retry" \
      && mv "$PLAN/.tasks.tsv.retry" "$T"
  fi
  rm -f "$tdir/noretry"
}

# The scheduler's `_prepare_retry` target: prepare, then clear the status so the task
# reads as PENDING again. `retry` clears it itself, after its own preflight.
retry_for_scheduler() { # retry_for_scheduler <plan> <id>
  prepare_retry "$1" "$2" || return $?
  rm -f "$1/tasks/$2/status"
}

# --- implementer guards ---------------------------------------------------------
# Observed facts, never self-reports. They warn and are recorded in guard.txt; they do
# not change a task's status, because the diff and the reviewer still decide that.
guard_before_dwarf() { # guard_before_dwarf <wt> <tdir>
  rm -f "$2/guard.txt"
  git -C "$1" rev-parse HEAD > "$2/dwarf.head.before" 2>/dev/null || rm -f "$2/dwarf.head.before"
  git -C "$1" symbolic-ref -q HEAD > "$2/dwarf.ref.before" 2>/dev/null || echo detached > "$2/dwarf.ref.before"
}

guard_after_dwarf() { # guard_after_dwarf <wt> <tdir> <id>
  local wt="$1" tdir="$2" id="$3" h0 h1 r0 r1 n g="$2/guard.txt" line
  : > "$g"
  h0="$(cat "$tdir/dwarf.head.before" 2>/dev/null)"; h1="$(git -C "$wt" rev-parse HEAD 2>/dev/null)"
  r0="$(cat "$tdir/dwarf.ref.before" 2>/dev/null)"; r1="$(git -C "$wt" symbolic-ref -q HEAD 2>/dev/null || echo detached)"
  if [ -n "$h0" ] && [ -n "$h1" ] && [ "$h0" != "$h1" ]; then
    n="$(git -C "$wt" rev-list --count "$h0..$h1" 2>/dev/null || echo '?')"
    printf 'self-commit: the implementer committed (%s new commit(s)) although it was told not to; the cumulative diff against the task base is still what gets reviewed\n' "$n" >> "$g"
  fi
  if [ -n "$r0" ] && [ "$r0" != "$r1" ]; then
    printf 'branch switch: the implementer changed the checked-out ref from %s to %s; the work is rejected as INVALIDATED unless it ends on the task branch\n' "$r0" "$r1" >> "$g"
  fi
  if [ -s "$tdir/dwarf.orphans" ]; then
    printf 'orphan processes: %s process(es) the implementer left running were stopped (listed in %s)\n' \
      "$(wc -l < "$tdir/dwarf.orphans" | tr -d ' ')" "$tdir/dwarf.orphans" >> "$g"
  fi
  if [ -s "$tdir/dwarf.last" ]; then
    python3 "$SKILL_DIR/scripts/forge-guard.py" promises "$tdir/dwarf.last" 2>/dev/null |
      while IFS= read -r line; do
        printf 'promised later work: %s\n' "$line"
      done >> "$g"
  fi
  if [ -s "$g" ]; then
    while IFS= read -r line; do note "$id: guard — $line"; done < "$g"
  else
    rm -f "$g"
  fi
  return 0
}

# --- QA severity gate -----------------------------------------------------------
qa_threshold_for() { # per-task file wins over the plan-level default; prints nothing if unset
  local f
  for f in "$1/tasks/$2/qa_threshold" "$1/qa_threshold"; do
    [ -s "$f" ] && { head -1 "$f" | tr -d ' \r'; return 0; }
  done
  return 0
}

# With an explicit threshold, a reviewer's FAIL whose confirmed findings are all labelled
# and less severe than the threshold is accepted as PASS-with-known-issues. The literal
# status stays PASS, so the merge path, the scheduler and Fractal need no new vocabulary.
# Anything unlabelled stays blocking: a finding the gate cannot place is not tolerated.
qa_gate() { # qa_gate <plan> <id> <tdir> <verdict>
  local plan="$1" id="$2" tdir="$3" verdict="$4" thr line
  rm -f "$tdir/qa.gate" "$tdir/known_issues.md"
  thr="$(qa_threshold_for "$plan" "$id")"
  [ -n "$thr" ] || return 0
  line="$(python3 "$SKILL_DIR/scripts/forge-contract.py" gate --verdict "${verdict:-UNKNOWN}" \
      --qa-last "$tdir/qa.last" --threshold "$thr" --known-issues "$tdir/known_issues.md" 2>/dev/null)" || return 0
  [ -n "$line" ] || return 0
  printf '%s\t%s\n' "$thr" "$line" > "$tdir/qa.gate"
  if [ "$(printf '%s' "$line" | awk -F'\t' '{print $1}')" = accept ]; then
    echo PASS > "$tdir/status"
    note "$id: the reviewer said FAIL, but every confirmed finding is less severe than the $thr threshold — accepted with known issues ($tdir/known_issues.md)"
  fi
  return 0
}

# --- report blocks ----------------------------------------------------------------
report_infra() { # report_infra <plan>
  local plan="$1" id n=0 cls
  for id in $(all_ids "$plan/tasks.tsv"); do
    [ "$(task_status "$plan" "$id")" = INFRA ] || continue
    [ "$n" = 0 ] && echo "paused on an infrastructure failure (not a task failure, no attempt spent):"
    n=$((n+1))
    cls="$(sed -n 's/^class=//p' "$plan/tasks/$id/infra.txt" 2>/dev/null | head -1)"
    printf '  %-14s %s (%s stage)\n' "$id" "${cls:-unknown}" "$(sed -n 's/^stage=//p' "$plan/tasks/$id/infra.txt" 2>/dev/null | tail -1)"
  done
  if [ "$n" -gt 0 ]; then
    echo "  fix the cause (quota, login, network), then re-run: forge-parallel.sh run $plan"
    echo
  fi
  return 0
}

report_known_issues() { # report_known_issues <plan>
  local plan="$1" id n=0
  for id in $(all_ids "$plan/tasks.tsv"); do
    [ -s "$plan/tasks/$id/known_issues.md" ] || continue
    [ "$n" = 0 ] && echo "known issues accepted (findings below the QA threshold, or accepted by a human):"
    n=$((n+1))
    printf '  %-14s %s finding(s)  %s\n' "$id" "$(grep -c '^- ' "$plan/tasks/$id/known_issues.md" 2>/dev/null || echo 0)" "$plan/tasks/$id/known_issues.md"
  done
  [ "$n" -gt 0 ] && echo
  return 0
}

report_guard_notes() { # report_guard_notes <plan>
  local plan="$1" id n=0 line
  for id in $(all_ids "$plan/tasks.tsv"); do
    [ -s "$plan/tasks/$id/guard.txt" ] || continue
    [ "$n" = 0 ] && echo "implementer guard notes (observed by forge, not self-reported):"
    n=$((n+1))
    while IFS= read -r line; do printf '  %-14s %s\n' "$id" "$line"; done < "$plan/tasks/$id/guard.txt"
  done
  [ "$n" -gt 0 ] && echo
  return 0
}

report_verify_notes() { # report_verify_notes <dir holding verification.*>
  local d="$1"
  if [ -s "$d/verification.flaky" ]; then
    echo "FLAKY verification: the command failed once and passed on rerun — read $d/verification.log"
    echo "  before trusting this result; an intermittent failure can be a real bug."
    echo
  fi
  if [ -s "$d/verification.coverage.txt" ]; then
    echo "verification coverage (suites the project defines that the verify command does not run):"
    sed 's/^/  /' "$d/verification.coverage.txt"
    echo
  fi
  return 0
}

# Everything worth saying under the results table, in one call.
run_report_extras() { # run_report_extras <plan> [<verification dir>]
  report_infra "$1"
  report_known_issues "$1"
  report_guard_notes "$1"
  report_verify_notes "${2:-$1}"
}

# Integrating a partial run is allowed (a human may want what passed), but never silently:
# say which tasks are NOT in the result.
report_unmerged() { # report_unmerged <plan>
  local plan="$1" id st list=""
  for id in $(all_ids "$plan/tasks.tsv"); do
    st="$(task_status "$plan" "$id")"
    [ "$st" = MERGED ] || list="$list $id($st)"
  done
  [ -z "$list" ] || note "WARNING: not merged, so NOT part of this integration:$list"
  return 0
}
