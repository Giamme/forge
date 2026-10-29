#!/usr/bin/env bash
# Per-task helpers for forge-parallel.sh: attempt accounting, infrastructure stops,
# QA-only resume, retry preparation, implementer guards, the severity gate and the
# report blocks printed under the results table.
#
# Sourced by forge-parallel.sh, which provides die/note/field/all_ids/task_status and
# SKILL_DIR. Functions only; nothing runs at source time.

# --- attempt log ----------------------------------------------------------------
# One row per do_task entry: epoch, attempt number, resulting status, infra class, and the
# implementer spec the attempt ran (the escalation policy counts failures per model, and
# the dwarf column is rewritten before the next attempt begins).
# `attempt` itself only counts attempts that were really spent (an infrastructure stop
# refunds it), so this file is the audit trail of what happened, including the refunds.
record_attempt() { # record_attempt <plan> <id>
  local tdir="$1/tasks/$2" cls=""
  [ "$(task_status "$1" "$2")" = BLOCKED ] && return 0   # never started: nothing to audit
  [ "$(task_status "$1" "$2")" = INFRA ] && cls="$(sed -n 's/^class=//p' "$tdir/infra.txt" 2>/dev/null | head -1)"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "$(cat "$tdir/attempt" 2>/dev/null || echo 0)" \
    "$(task_status "$1" "$2")" "$cls" "$(field "$1/tasks.tsv" "$2" dwarf)" >> "$tdir/attempts.tsv"
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

# True when the worktree is still exactly what was reviewed. Reads and deletes nothing:
# the reviewed commit, the fingerprint and changes.diff are present, and the worktree's
# HEAD and fingerprint equal them.
reviewed_state_matches() { # reviewed_state_matches <tdir> <wt>
  local tdir="$1" wt="$2"
  [ -s "$tdir/reviewed.commit" ] && [ -s "$tdir/source.fingerprint" ] && [ -s "$tdir/changes.diff" ] || return 1
  [ -e "$wt/.git" ] || return 1
  [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null)" = "$(cat "$tdir/reviewed.commit")" ] || return 1
  [ "$(forge_fingerprint "$wt")" = "$(cat "$tdir/source.fingerprint")" ]
}

# A QA-only resume is trusted only while the worktree is still exactly what was
# reviewed. Anything else clears the marker and the full path runs.
resume_valid() { # resume_valid <tdir> <wt>
  [ "$(cat "$1/resume" 2>/dev/null)" = qa ] || return 1
  reviewed_state_matches "$1" "$2"
}

# The next entry's kind, decided before it starts with the test do_task_body applies
# (resume_valid): true when it only re-runs QA. A marker the worktree no longer matches is
# cleared here, so whoever prepares the entry and do_task_body agree that it is a full one.
resume_check() { # resume_check <plan> <id>
  local tdir="$1/tasks/$2" root
  [ -f "$tdir/resume" ] || return 1
  root="$(cat "$1/wt_root" 2>/dev/null)"
  [ -n "$root" ] && resume_valid "$tdir" "$root/$2" && return 0
  rm -f "$tdir/resume"
  note "$2: the saved review state no longer matches the worktree — the next attempt runs the full task"
  return 1
}

# --- escalation pin -------------------------------------------------------------
# A human's one-shot choice of the model for a task's next full retry:
# tasks/<id>/escalation.pin holds one dwarf spec (written by `forge control pin`). The
# retry claims it with an atomic rename so a writer replacing it meanwhile never races the
# read, and settles the claim once tasks.tsv and escalations.tsv are written.
forge_plan_is_fractal() { # forge_plan_is_fractal <plan>
  [ -s "$1/fractal-selection.json" ] || return 1
  python3 -c 'import json, sys
try:
    sys.exit(0 if json.load(open(sys.argv[1])).get("enabled") else 1)
except Exception:
    sys.exit(1)' "$1/fractal-selection.json"
}

# The pin's spec: the first line, trimmed, a single token (it lands in a TSV column).
# Prints nothing when the file is not a regular file or holds no usable spec.
pin_spec() { # pin_spec <file>
  local spec
  [ -f "$1" ] && [ ! -L "$1" ] || return 0
  spec="$(head -n 1 "$1" 2>/dev/null | tr -d '\r')"
  spec="${spec#"${spec%%[![:space:]]*}"}"; spec="${spec%"${spec##*[![:space:]]}"}"
  case "$spec" in ''|-|UNASSIGNED|*[[:space:][:cntrl:]]*) return 0 ;; esac
  printf '%s\n' "$spec"
}

# claim_pin <tdir> -> prints the spec and returns 0 when a pin was claimed; the file is
# then escalation.pin.applying until settle_pin. A pin with no usable spec is dropped.
# The claim is a bare rename(2), not mv: mv moves a file INTO a destination that is a
# directory or a symlink to one (BSD mv has no -T), which could carry the pin out of the
# plan. rename replaces a stale file or symlink named .applying and fails on a directory,
# and then the pin stays where it is for a later retry.
claim_pin() {
  local tdir="$1" spec
  [ -f "$tdir/escalation.pin" ] && [ ! -L "$tdir/escalation.pin" ] || return 1
  if ! python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
      "$tdir/escalation.pin" "$tdir/escalation.pin.applying" 2>/dev/null; then
    [ -e "$tdir/escalation.pin" ] && \
      note "$(basename "$tdir"): could not claim the escalation pin (escalation.pin.applying is in the way) — leaving it in place"
    return 1
  fi
  spec="$(pin_spec "$tdir/escalation.pin.applying")"
  if [ -z "$spec" ]; then
    rm -f "$tdir/escalation.pin.applying"
    note "$(basename "$tdir"): the escalation pin holds no usable spec — dropped"
    return 1
  fi
  printf '%s\n' "$spec"
}

# settle_pin <tdir> done|restore. Done deletes the claim. Restore gives the pin back unless
# a new one appeared meanwhile (hard link: it fails if the target exists, so a pin written
# during the application is never overwritten); that newer pin is the one for the next retry.
# python3, like claim_pin's rename: forge already requires it, and `ln` is one tool more.
settle_pin() {
  local tdir="$1"
  [ "$2" = restore ] && python3 -c 'import os, sys; os.link(sys.argv[1], sys.argv[2])' \
    "$tdir/escalation.pin.applying" "$tdir/escalation.pin" 2>/dev/null
  rm -f "$tdir/escalation.pin.applying"
  return 0
}

# True when the task's next retry only re-runs QA: a `resume` marker (a QA-stage infrastructure
# stop), or a verdict-less review of committed work, which prepare_retry turns into the marker
# but has not yet when the run summary is printed. Such a retry leaves a pin in place.
# Both are believed only while the worktree is still what was reviewed — the comparison
# resume_valid uses, and the marker prepare_retry is about to write gets the same test.
# This never deletes the marker; resume_check owns that.
qa_only_next() { # qa_only_next <plan> <id>
  local tdir="$1/tasks/$2" root="" wt=""
  root="$(cat "$1/wt_root" 2>/dev/null)"
  [ -n "$root" ] && wt="$root/$2"
  if [ "$(cat "$tdir/resume" 2>/dev/null)" = qa ]; then
    [ -n "$wt" ] && reviewed_state_matches "$tdir" "$wt"
    return
  fi
  case "$(task_status "$1" "$2")" in UNKNOWN|INVALIDATED) ;; *) return 1 ;; esac
  if [ -s "$tdir/reviewed.commit" ]; then
    [ -n "$wt" ] && reviewed_state_matches "$tdir" "$wt"
    return
  fi
  return 1
}

qa_only_next_flag() { # qa_only_next_flag <plan> <id> -> yes | no, for `escalation --check`
  if qa_only_next "$1" "$2"; then printf 'yes\n'; else printf 'no\n'; fi
}

# tasks/<id>/escalations.tsv, probed and appended the way forge-control writes a plan file
# (forge-route.py record loads that code): every component below the plan is a real
# directory, and the file is absent or a regular file. A symlink at any level is a refusal.
escalation_record_ok() { # escalation_record_ok <plan> <id>
  python3 "$SKILL_DIR/scripts/forge-route.py" record --plan "$1" --task "$2" --probe
}

escalation_record_line() { # escalation_record_line <plan> <id> <line>
  python3 "$SKILL_DIR/scripts/forge-route.py" record --plan "$1" --task "$2" --line "$3"
}

# Writes the task's implementer column in tasks.tsv: a temp file, then a rename.
set_dwarf() { # set_dwarf <plan> <id> <spec>
  awk -F'\t' -v OFS='\t' -v id="$2" -v dw="$3" \
    '$0 !~ /^#/ && $1==id { $5=dw } { print }' "$1/tasks.tsv" > "$1/.tasks.tsv.retry" \
    && mv "$1/.tasks.tsv.retry" "$1/tasks.tsv"
}

# --- retry preparation ----------------------------------------------------------
# Carries the last review's report forward as the findings of the next attempt, and records
# which commit that report judged (what reviewed.commit holds now, since qa.last is that
# review's). The two are written together: the commit first, so a report never sits beside a
# commit that belongs to an older one. Repeating the call writes the same bytes.
carry_findings() { # carry_findings <tdir>
  local tdir="$1"
  if [ -s "$tdir/reviewed.commit" ]; then
    cp "$tdir/reviewed.commit" "$tdir/.retry_findings.commit.tmp" \
      && mv "$tdir/.retry_findings.commit.tmp" "$tdir/retry_findings.commit"
  else
    rm -f "$tdir/retry_findings.commit"
  fi
  # The reviewer session that wrote these findings, and what it ran as, kept beside them for a
  # plan with --reviewer-session. Removed first, so an older session never sits next to newer
  # findings; a review that left no session leaves none here.
  rm -f "$tdir/retry_findings.session" "$tdir/retry_findings.resolved"
  carry_implementer_session "$tdir"
  local gthr gdec
  IFS="$(printf '\t')" read -r gthr gdec _ < "$tdir/qa.gate" 2>/dev/null || true
  if [ "${gdec:-}" = fail ]; then
    # forge, not the reviewer, failed this attempt: say so above the copied report.
    { printf "forge: the reviewer's verdict was PASS, but this report lists CONFIRMED findings at or above the \`%s\` blocking threshold, so forge failed the attempt. Fix those findings.\n\n" "$gthr"
      cat "$tdir/qa.last"; } > "$tdir/.retry_findings.md.tmp" && mv "$tdir/.retry_findings.md.tmp" "$tdir/retry_findings.md"
  else
    cp "$tdir/qa.last" "$tdir/retry_findings.md"
  fi
  [ ! -f "$tdir/qa.session" ] || cp "$tdir/qa.session" "$tdir/retry_findings.session" 2>/dev/null || true
  [ ! -f "$tdir/qa.resolved" ] || cp "$tdir/qa.resolved" "$tdir/retry_findings.resolved" 2>/dev/null || true
}

# The retry findings of an attempt that failed forge's pre-review tests: the failing tests and
# the end of their output, against the commit they ran on. No reviewer session goes with them.
carry_prereview() { # carry_prereview <tdir>
  local tdir="$1"
  if [ -s "$tdir/reviewed.commit" ]; then
    cp "$tdir/reviewed.commit" "$tdir/.retry_findings.commit.tmp" \
      && mv "$tdir/.retry_findings.commit.tmp" "$tdir/retry_findings.commit"
  else
    rm -f "$tdir/retry_findings.commit"
  fi
  rm -f "$tdir/retry_findings.session" "$tdir/retry_findings.resolved"
  carry_implementer_session "$tdir"
  cp "$tdir/prereview.md" "$tdir/.retry_findings.md.tmp" && mv "$tdir/.retry_findings.md.tmp" "$tdir/retry_findings.md"
}

# The implementer session that produced the judged commit (reviewed.dwarf.*, recorded with
# reviewed.commit), kept beside the findings for a plan with --implementer-session, as
# carry_findings keeps the reviewer's.
carry_implementer_session() { # carry_implementer_session <tdir>
  local tdir="$1" n
  for n in session resolved; do
    rm -f "$tdir/retry_findings.dwarf.$n"
    [ ! -f "$tdir/reviewed.dwarf.$n" ] || cp "$tdir/reviewed.dwarf.$n" "$tdir/retry_findings.dwarf.$n" 2>/dev/null || true
  done
}

# --- reviewer-session continuation (plan --reviewer-session, an opt-in experiment) ------------
# review_session_decide <session-file> <resolved-file> <resolution-file> [harnesses]: whether this
# round continues the earlier session. Prints `mode<TAB>reason<TAB>session`; the session is the
# id to continue, and empty for every fresh answer.
#   session-file     the id the earlier round ran under (retry_findings.session / qa.session)
#   resolved-file    that round's resolution (retry_findings.resolved / qa.resolved)
#   resolution-file  this dispatch's resolution (doctor output): harness=, model=, effort=
#   harnesses        the ones that can continue this role (default: a reviewer's, claude
#                    openclaude codex; an implementer passes claude openclaude)
# The caller has already handled the cases that need no files (first round, Fractal). Anything
# that cannot be checked is fresh; nothing here aborts.
review_session_decide() {
  local sf="$1" rf="$2" cf="$3" can="${4:-claude openclaude codex}" id="" k a b h
  [ -f "$sf" ] && id="$(head -1 "$sf" 2>/dev/null | tr -d '\r')"
  if [ -z "$id" ]; then printf 'fresh\tno-session\t\n'; return 0; fi
  case "$id" in *[!A-Za-z0-9-]*) printf 'fresh\tinvalid-session\t\n'; return 0 ;; esac
  if [ "${#id}" -lt 8 ] || [ "${#id}" -gt 128 ]; then printf 'fresh\tinvalid-session\t\n'; return 0; fi
  h="$(sed -n 's/^harness=//p' "$cf" 2>/dev/null | head -1)"
  if [ -z "$h" ]; then printf 'fresh\tharness-unsupported\t\n'; return 0; fi
  for k in harness model effort; do
    a="$(sed -n "s/^$k=//p" "$rf" 2>/dev/null | head -1)"
    b="$(sed -n "s/^$k=//p" "$cf" 2>/dev/null | head -1)"
    if [ -z "$a" ] || [ "$a" != "$b" ]; then printf 'fresh\tspec-changed\t\n'; return 0; fi
  done
  case " $can " in
    *" $h "*) printf 'continued\tsame-spec\t%s\n' "$id" ;;
    *) printf 'fresh\tharness-unsupported\t\n' ;;
  esac
  return 0
}

# Whether the task's worktree is still exactly the commit the carried findings judged: nothing
# committed or edited since (a timed-out or interrupted implementer leaves edits behind). Only
# then does the implementer session that made that commit know the work it would continue.
implementer_work_unchanged() { # implementer_work_unchanged <tdir> <worktree>
  local tdir="$1" wt="$2" head
  [ -s "$tdir/retry_findings.commit" ] || return 1
  head="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ "$head" = "$(cat "$tdir/retry_findings.commit")" ] || return 1
  [ -z "$(git -C "$wt" status --porcelain --untracked-files=all -- . ':(exclude).forge' 2>/dev/null)" ]
}

# The diff between the attempt the carried findings judged and the one under review now, as
# files a later gate can reuse (tasks/<id>/changes.since.{diff,range,stat}). All three go
# when there is no valid range: no findings, no recorded commit, a commit that is unknown or
# no ancestor of the reviewed one, or nothing changed.
attempt_delta() { # attempt_delta <tdir> <worktree> <reviewed-commit>
  local tdir="$1" wt="$2" reviewed="$3" from="" to=""
  rm -f "$tdir/.changes.since.diff" "$tdir/.changes.since.stat"
  if [ -s "$tdir/retry_findings.md" ] && [ -s "$tdir/retry_findings.commit" ] \
     && from="$(git -C "$wt" rev-parse --verify -q "$(cat "$tdir/retry_findings.commit")^{commit}")" \
     && to="$(git -C "$wt" rev-parse --verify -q "$reviewed^{commit}")" \
     && git -C "$wt" merge-base --is-ancestor "$from" "$to" \
     && git -C "$wt" diff --binary --irreversible-delete "$from" "$to" -- . ':(exclude).forge' > "$tdir/.changes.since.diff" 2>/dev/null \
     && [ -s "$tdir/.changes.since.diff" ] \
     && git -C "$wt" diff --stat "$from" "$to" -- . ':(exclude).forge' > "$tdir/.changes.since.stat" 2>/dev/null; then
    printf '%s %s\n' "$from" "$to" > "$tdir/changes.since.range"
    mv "$tdir/.changes.since.diff" "$tdir/changes.since.diff"
    mv "$tdir/.changes.since.stat" "$tdir/changes.since.stat"
    return 0
  fi
  rm -f "$tdir/.changes.since.diff" "$tdir/.changes.since.stat" \
        "$tdir/changes.since.diff" "$tdir/changes.since.range" "$tdir/changes.since.stat"
  return 0
}

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
  elif { [ "$st" = UNKNOWN ] || [ "$st" = INVALIDATED ]; } && [ -s "$tdir/reviewed.commit" ]; then
    # The reviewer never reached a verdict, or its verdict was voided because something changed
    # during the review: either way the code was not rejected. Re-asking QA is cheaper and more
    # honest than paying an implementer to rewrite unjudged work; the marker is validated
    # against the worktree below before it is believed (a changed worktree runs the full task).
    echo qa > "$tdir/resume"
  elif [ "$st" = FAIL ] && [ -f "$tdir/prereview.failed" ] && [ -s "$tdir/prereview.md" ]; then
    # No reviewer ran: forge's own pre-review tests failed (#85), and their output is the finding.
    carry_prereview "$tdir"
  # Only a real review replaces the findings. The runner also writes explanatory text
  # into qa.last when it fails a task without dispatching QA ("produced no changes at
  # all"), and copying that over would throw away the actual findings the previous
  # reviewer gave — the one thing a retry exists to carry forward.
  elif [ "$verdict" = PASS ] || [ "$verdict" = FAIL ]; then
    carry_findings "$tdir"
  elif grep -q 'FORGE_VERDICT' "$tdir/qa.last" 2>/dev/null; then
    carry_findings "$tdir"
  elif [ -s "$tdir/retry_findings.md" ]; then
    note "$id: no new review since the last retry — reusing the previous findings"
  else
    note "$id: no previous QA findings to pass on (last status was $st)"
    : > "$tdir/retry_findings.md"
    rm -f "$tdir/retry_findings.commit" "$tdir/retry_findings.session" "$tdir/retry_findings.resolved" \
          "$tdir/retry_findings.dwarf.session" "$tdir/retry_findings.dwarf.resolved"
  fi
  # A QA-only resume (an unreviewed verdict, or a QA-stage infrastructure stop) re-asks the
  # reviewer about work that is already committed; the dwarf does not run, so it is no moment
  # to change the implementer. Both branches above wrote the `resume` marker; it holds only
  # while the worktree is still what was reviewed, which do_task_body checks again.
  local qa_only=0
  if resume_check "$PLAN" "$id"; then
    qa_only=1
    [ "$st" = UNKNOWN ] && note "$id: the reviewer gave no verdict — re-running QA on the unchanged work"
    [ "$st" = INVALIDATED ] && note "$id: the last review was voided — re-running QA on the unchanged work"
  fi
  # Who decides the next implementer, in this order: an explicit --dwarf (the human's call,
  # source `retry`), a pin (the human's call, applied once, source `pin`), then the retry
  # policy (forge-route.py escalation): after escalate_after failed attempts on one model
  # (default 3) the next attempt goes a tier up in the user's own pools instead of repeating
  # the dispatch whose fourth try passes least often, or the task stops with a split
  # suggestion.
  local cur next rc esc_source="" pinned="" has_pin=0
  cur="$(field "$T" "$id" dwarf)"
  [ -f "$tdir/escalation.pin" ] && [ ! -L "$tdir/escalation.pin" ] && has_pin=1
  if [ -n "$newdwarf" ]; then
    esc_source=retry
    rm -f "$tdir/escalation.stop" "$tdir/split.suggested" "$tdir/escalation.next"
  elif [ "$has_pin" = 1 ] && [ "$qa_only" = 1 ]; then
    note "$id: the escalation pin waits for the next full retry (this one only re-runs QA)"
  elif [ "$has_pin" = 1 ] && forge_plan_is_fractal "$PLAN"; then
    note "$id: an escalation pin is set, but Fractal plans never apply pins — leaving it in place"
  elif [ "$has_pin" = 1 ] && ! escalation_record_ok "$PLAN" "$id"; then
    # Before the claim: a refused record leaves the pin unclaimed and tasks.tsv as it was.
    note "$id: could not record the escalation in escalations.tsv; nothing was retried"
    return 1
  elif [ "$has_pin" = 1 ] && pinned="$(claim_pin "$tdir")"; then
    esc_source=pin
    newdwarf="$pinned"
    note "$id: applying the pinned escalation to $pinned"
  else
    next="$(python3 "$SKILL_DIR/scripts/forge-route.py" escalation --plan "$PLAN" --task "$id")"; rc=$?
    if [ "$rc" = 3 ]; then
      note "$(cat "$tdir/split.suggested" 2>/dev/null)"
      return 3
    fi
    if [ -n "$next" ]; then
      note "$id: escalating from $cur to $next after $(cat "$PLAN/escalate_after" 2>/dev/null || echo 3) failed attempts on one model (--escalate-after 0 turns this off)"
      newdwarf="$next"
      esc_source=policy
    fi
  fi
  # The same spec again is no escalation, except a pin: a human asked for it, so the
  # request is recorded and consumed.
  if [ "$newdwarf" = "$cur" ] && [ "$esc_source" != pin ]; then newdwarf=""; fi
  if [ -n "$newdwarf" ]; then
    write_escalation "$PLAN" "$id" "$cur" "$newdwarf" "$esc_source" || return 1
  fi
  rm -f "$tdir/noretry"
}

# Puts the next implementer in place: tasks.tsv, the escalations.tsv record and the dataset
# event. Returns 1, with tasks.tsv and a claimed pin given back, when nothing was changed.
write_escalation() { # write_escalation <plan> <id> <from> <to> <retry|pin|policy>
  local PLAN="$1" id="$2" cur="$3" newdwarf="$4" esc_source="$5" tdir="$1/tasks/$2" line
  # A pin is held to its record: whether the record can be written is learned before
  # anything is changed, so a failure there leaves tasks.tsv and the pin as they were.
  if [ "$esc_source" = pin ] && ! escalation_record_ok "$PLAN" "$id"; then
    note "$id: could not record the escalation in escalations.tsv; nothing was retried"
    settle_pin "$tdir" restore
    return 1
  fi
  # Written into tasks.tsv rather than kept in a side file: the table then shows the
  # model that will actually be spent, and plan's rule that an explicit column value
  # survives a re-plan protects the escalation for free. Rewritten first, so a failed
  # rewrite is never recorded as an escalation that happened.
  if [ "$newdwarf" != "$cur" ]; then
    set_dwarf "$PLAN" "$id" "$newdwarf" || {
      note "$id: could not write $newdwarf into tasks.tsv; nothing was retried"
      [ "$esc_source" = pin ] && settle_pin "$tdir" restore
      return 1
    }
  fi
  # The rewrite loses which model failed first, and an escalation is exactly the record
  # of "this tier was too weak for this task". The last column says who decided: forge's
  # policy, a human's `retry --dwarf`, or a pin.
  line="$(printf '%s\t%s\t%s\t%s\t%s' "$(date +%s)" "$(cat "$tdir/attempt" 2>/dev/null || echo 0)" \
      "$cur" "$newdwarf" "$esc_source")"
  if ! escalation_record_line "$PLAN" "$id" "$line" && [ "$esc_source" = pin ]; then
    # The rewrite is undone with the record, so the pin given back is still a real
    # escalation next time, not a same-spec one that hides the model it replaced.
    if [ "$newdwarf" = "$cur" ] || set_dwarf "$PLAN" "$id" "$cur"; then
      note "$id: could not record the escalation in escalations.tsv; nothing was retried"
      settle_pin "$tdir" restore
      return 1
    fi
    note "$id: could not record the escalation in escalations.tsv, and tasks.tsv could not be put back to $cur; it keeps $newdwarf, so the pin is consumed and the retry goes on"
  fi
  # Recorded at the rewrite itself: it happens before the retried attempt begins, so no
  # attempt capture would ever see it as something that attempt produced.
  forge_dataset_event escalation --dir "$PLAN" --task "$id" --set "from=$cur" \
    --set "to=$newdwarf" --set "after_attempt=$(cat "$tdir/attempt" 2>/dev/null || echo 0)" \
    --set "source=$esc_source"
  # A pin, like an explicit --dwarf, settles the retry policy's verdicts about this task.
  [ "$esc_source" = pin ] && rm -f "$tdir/escalation.stop" "$tdir/split.suggested"
  rm -f "$tdir/escalation.next"
  [ "$esc_source" = pin ] && settle_pin "$tdir" done
  return 0
}

# An infrastructure stop dispatched again by the scheduler. It is no retry: no attempt or
# budget is spent, the findings carried forward stay, and the retry policy is not asked. But
# unless the stop came at QA (and the reviewed work is intact), the entry is a full
# implementer attempt, so a pending pin applies here exactly as prepare_retry applies it.
# Returns 1 when a claimed pin could not be put in place; the task is then not resumed.
prepare_resume() { # prepare_resume <plan> <id>
  local PLAN="$1" id="$2" tdir="$1/tasks/$2" pinned
  [ -f "$tdir/escalation.pin" ] && [ ! -L "$tdir/escalation.pin" ] || return 0
  if resume_check "$PLAN" "$id"; then
    note "$id: the escalation pin waits for the next full retry (this resume only re-runs QA)"
  elif forge_plan_is_fractal "$PLAN"; then
    note "$id: an escalation pin is set, but Fractal plans never apply pins — leaving it in place"
  elif ! escalation_record_ok "$PLAN" "$id"; then
    note "$id: could not record the escalation in escalations.tsv; nothing was retried"
    return 1
  elif pinned="$(claim_pin "$tdir")"; then
    note "$id: applying the pinned escalation to $pinned"
    write_escalation "$PLAN" "$id" "$(field "$PLAN/tasks.tsv" "$id" dwarf)" "$pinned" pin || return 1
  fi
  return 0
}

# --infra-fallback: a dwarf-stage capacity stop moves the task to the next healthy member of
# its own tier pool (forge-route.py fallback), recorded like an escalation with source
# `fallback`. No attempt is spent: the stop already refunded it. Returns 1, changing nothing,
# when there is no candidate, a pin is waiting (a human's choice wins) or the plan is Fractal.
prepare_fallback() { # prepare_fallback <plan> <id> <excluded harness/model,...>
  local PLAN="$1" id="$2" tdir="$1/tasks/$2" cur pick
  [ -e "$tdir/escalation.pin" ] && return 1
  forge_plan_is_fractal "$PLAN" && return 1
  cur="$(field "$PLAN/tasks.tsv" "$id" dwarf)"
  pick="$(python3 "$SKILL_DIR/scripts/forge-route.py" fallback --plan "$PLAN" --task "$id" --exclude "$3")" || return 1
  [ -n "$pick" ] || return 1
  write_escalation "$PLAN" "$id" "$cur" "$pick" fallback || return 1
  rm -f "$tdir/status" "$tdir/infra.txt"
  note "$id: $cur is at capacity; falling back to $pick (same tier)"
}

# The scheduler's `_prepare_retry` target: prepare, then clear the status so the task
# reads as PENDING again. `retry` clears it itself, after its own preflight. With --resume it
# prepares an infrastructure stop's resume instead (prepare_resume), and the status stays;
# with --dwarf <spec> the spec is the explicit new implementer; --fallback <pairs> is
# prepare_fallback.
retry_for_scheduler() { # retry_for_scheduler <plan> <id> [--resume | --dwarf <spec> | --fallback <pairs>]
  if [ "${3:-}" = --resume ]; then
    prepare_resume "$1" "$2"
    return $?
  fi
  if [ "${3:-}" = --fallback ]; then
    prepare_fallback "$1" "$2" "${4:-}"
    return $?
  fi
  # --dwarf: `forge control retry --dwarf`, the same explicit escalation as `retry --dwarf`.
  if [ "${3:-}" = --dwarf ]; then
    [ -n "${4:-}" ] || die "_prepare_retry: --dwarf needs a spec"
    prepare_retry "$1" "$2" "$4" || return $?
  else
    prepare_retry "$1" "$2" || return $?
  fi
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
# The bash mirror of forge-contract.py's DEFAULT_THRESHOLD; a test asserts the two agree.
FORGE_QA_THRESHOLD_DEFAULT=P2

# qa_threshold_walk <plan> <id> -> "<level> <set|default>": the per-task file, then the plan
# file, then the default. "none" in a file means nothing is tolerated, the same as P3 (so a
# per-task none still exempts a task from a plan-wide tolerant threshold). Anything but P0-P3
# or none is ignored: a garbage per-task value falls through to the plan file, a garbage plan
# value to the default. An empty id reads the plan level only (the approval table).
qa_threshold_walk() {
  local f v
  for f in "$1/tasks/$2/qa_threshold" "$1/qa_threshold"; do
    [ -n "$2" ] || [ "$f" = "$1/qa_threshold" ] || continue
    [ -s "$f" ] || continue
    v="$(head -1 "$f" | tr -d ' \r' | tr 'a-z' 'A-Z')"
    case "$v" in
      P0|P1|P2|P3) printf '%s set\n' "$v"; return 0 ;;
      NONE) printf 'P3 set\n'; return 0 ;;
    esac
  done
  printf '%s default\n' "$FORGE_QA_THRESHOLD_DEFAULT"
}

qa_threshold_for() { # qa_threshold_for <plan> <id> -> the effective threshold, always a value
  local v; v="$(qa_threshold_walk "$1" "$2")"; printf '%s\n' "${v%% *}"
}

qa_threshold_source() { # qa_threshold_source <plan> <id> -> set | default
  local v; v="$(qa_threshold_walk "$1" "$2")"; printf '%s\n' "${v##* }"
}

# Epoch seconds with milliseconds, for the flip log and FORGE_RUN_STARTED. Whole seconds cannot
# tell a flip from just before a run started apart from one in the same second after it.
forge_now() { python3 -c 'import time; print("%.3f" % time.time())' 2>/dev/null || date +%s; }

# The gate goes both ways, at the effective threshold. A reviewer's FAIL whose confirmed
# findings are all labelled and less severe than the threshold is accepted as
# PASS-with-known-issues; the literal status stays PASS, so the merge path, the scheduler
# and Fractal need no new vocabulary. A PASS that lists a blocking CONFIRMED finding (at the
# threshold or worse, or with no severity) becomes FAIL.
# The optional 5th argument is the threshold the caller already used for this attempt's review
# prompt; when given it is used as-is, so a change to the files between the prompt and the
# gate cannot split one attempt across two thresholds. Absent (or empty), the files are read.
qa_gate() { # qa_gate <plan> <id> <tdir> <verdict> [threshold]
  local plan="$1" id="$2" tdir="$3" verdict="$4" thr="${5:-}" line worst
  rm -f "$tdir/qa.gate" "$tdir/known_issues.md"
  [ -n "$thr" ] || thr="$(qa_threshold_for "$plan" "$id")"
  line="$(python3 "$SKILL_DIR/scripts/forge-contract.py" gate --verdict "${verdict:-UNKNOWN}" \
      --qa-last "$tdir/qa.last" --threshold "$thr" --known-issues "$tdir/known_issues.md" 2>/dev/null)" || return 0
  [ -n "$line" ] || return 0
  printf '%s\t%s\n' "$thr" "$line" > "$tdir/qa.gate"
  case "$(printf '%s' "$line" | awk -F'\t' '{print $1}')" in
    accept)
      echo PASS > "$tdir/status"
      note "$id: the reviewer said FAIL, but every confirmed finding is less severe than the $thr threshold — accepted with known issues ($tdir/known_issues.md)" ;;
    fail)
      echo FAIL > "$tdir/status"
      worst="$(printf '%s' "$line" | awk -F'\t' '{print $2}')"
      note "$id: the reviewer said PASS but listed a CONFIRMED finding at or above the $thr threshold (worst $worst) — forge made it FAIL"
      # A scheduled task's notes go to its task.out, and a later attempt replaces qa.gate: this
      # log is what lets the run's summary say forge overrode a verdict (report_gate_flips).
      printf '%s\t%s\t%s\t%s\n' "$(forge_now)" "$(cat "$tdir/attempt" 2>/dev/null || echo 0)" "$thr" "$worst" \
        >> "$tdir/qa.flips" ;;
  esac
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
    # A FAIL task's tolerated findings are on disk too (the gate writes them whatever its
    # decision), but nothing was accepted: only a passed or merged task's are.
    case "$(task_status "$plan" "$id")" in PASS|MERGED) ;; *) continue ;; esac
    [ "$n" = 0 ] && echo "known issues accepted (findings below the QA threshold, or accepted by a human):"
    n=$((n+1))
    printf '  %-14s %s finding(s)  %s\n' "$id" "$(grep -c '^- ' "$plan/tasks/$id/known_issues.md" 2>/dev/null || echo 0)" "$plan/tasks/$id/known_issues.md"
  done
  [ "$n" -gt 0 ] && echo
  return 0
}

# The PASS verdicts forge turned into FAIL, since FORGE_RUN_STARTED (set by run and retry;
# unset shows them all), including those whose task a later attempt fixed.
report_gate_flips() { # report_gate_flips <plan>
  local plan="$1" id n=0 at attempt thr worst
  for id in $(all_ids "$plan/tasks.tsv"); do
    [ -s "$plan/tasks/$id/qa.flips" ] || continue
    while IFS="$(printf '\t')" read -r at attempt thr worst; do
      awk -v at="${at:-0}" -v since="${FORGE_RUN_STARTED:-0}" 'BEGIN { exit !(at + 0 >= since + 0) }' || continue
      [ "$n" = 0 ] && echo "forge overrode the reviewer's verdict (PASS, but it listed a CONFIRMED finding at or above the threshold):"
      n=$((n+1))
      printf '  %-14s attempt %s: PASS -> FAIL at the %s threshold (worst %s)\n' "$id" "$attempt" "$thr" "$worst"
    done < "$plan/tasks/$id/qa.flips"
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
  if [ -s "$d/verification.lanes.json" ]; then
    # Lanes: per lane, from the runner's own record
    python3 - "$d" <<'PY'
import json, os, sys
d = sys.argv[1]
try:
    with open(os.path.join(d, 'verification.lanes.json'), encoding='utf-8') as fh:
        lanes = [l for l in json.load(fh).get('lanes') or [] if isinstance(l, dict) and l.get('name')]
except (OSError, ValueError, AttributeError):
    sys.exit(0)
lines = ['FLAKY: lane %s failed once and passed on rerun' % l['name'] for l in lanes if l.get('state') == 'flaky']
lines += ['known failures only: lane %s' % l['name'] for l in lanes if l.get('state') == 'known']
failed = [l['name'] for l in lanes if l.get('state') == 'fail']
if failed:
    lines.append('verification failed in lane%s %s: read %s' % ('s' if len(failed) > 1 else '', ', '.join(failed),
                                                                 os.path.join(d, 'verification.log')))
if lines:
    print('\n'.join(lines) + '\n')
PY
  elif [ -s "$d/verification.flaky" ]; then
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
# Advisory notes under the approval table: tasks large enough that their first attempt
# usually fails review, and prompts with no acceptance criteria (forge-plan-check.py).
report_size() { # report_size <plan>
  python3 "$SKILL_DIR/scripts/forge-plan-check.py" size --plan "$1" 2>/dev/null || true
}

# The retry policy's verdicts: a task that stopped with a split suggestion, or whose next
# retry would go a tier up. Decided after every attempt, so a spent budget still says it.
# A pending pin is read from the file itself: it can be set between two attempts, and it
# outranks both verdicts (the retry applies it first). The converse needs care too: a pin
# withdrawn since the last attempt leaves the `escalation.next` that `--check` wrote for it,
# so a marker found with no pin pending is decided again rather than trusted.
report_escalations() { # report_escalations <plan>
  local plan="$1" id n=0 head pin
  head="retry policy (after $(cat "$plan/escalate_after" 2>/dev/null || echo 3) failed attempts on one model):"
  for id in $(all_ids "$plan/tasks.tsv"); do
    pin="$(pin_spec "$plan/tasks/$id/escalation.pin")"
    [ -n "$pin" ] && forge_plan_is_fractal "$plan" && pin=""
    if [ -n "$pin" ] && [ "$(task_status "$plan" "$id")" != MERGED ]; then
      [ "$n" = 0 ] && echo "$head"
      n=$((n+1))
      if qa_only_next "$plan" "$id"; then
        printf '  %-14s its next retry goes to %s (pinned; after the QA-only re-run)\n' "$id" "$pin"
      else
        printf '  %-14s its next retry goes to %s (pinned)\n' "$id" "$pin"
      fi
      continue
    fi
    if [ -s "$plan/tasks/$id/escalation.next" ] && [ "$(task_status "$plan" "$id")" != MERGED ]; then
      python3 "$SKILL_DIR/scripts/forge-route.py" escalation --plan "$plan" --task "$id" --check \
        --qa-only-next "$(qa_only_next_flag "$plan" "$id")" >/dev/null 2>&1 || true
    fi
    if [ -s "$plan/tasks/$id/split.suggested" ]; then
      [ "$n" = 0 ] && echo "$head"
      n=$((n+1)); printf '  %-14s split suggested: %s\n' "$id" "$(cat "$plan/tasks/$id/split.suggested")"
    elif [ -s "$plan/tasks/$id/escalation.next" ] && [ "$(task_status "$plan" "$id")" != MERGED ]; then
      [ "$n" = 0 ] && echo "$head"
      n=$((n+1)); printf '  %-14s its next retry goes to %s\n' "$id" "$(cat "$plan/tasks/$id/escalation.next")"
    fi
  done
  [ "$n" -gt 0 ] && echo
  return 0
}

run_report_extras() { # run_report_extras <plan> [<verification dir>]
  report_infra "$1"
  report_escalations "$1"
  report_gate_flips "$1"
  report_known_issues "$1"
  python3 "$SKILL_DIR/scripts/forge-sweep.py" report "$1" 2>/dev/null || true
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
