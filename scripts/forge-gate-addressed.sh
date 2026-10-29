#!/usr/bin/env bash
# The retry-addressed gate (#43): a retry whose diff leaves a blocking finding untouched goes
# back to the implementer without a review. Sourced by forge-lib-gates.sh; forge_pre_qa_gates
# calls it right after reviewed.commit is written, before QA is dispatched.
#
# It can only send work back: it never accepts anything, and it fails open (no key, no answer, a
# shadow gate, any error all return 0). The attempt after a bounce has retry_findings.source
# `bounce`, so this never runs twice in a row and QA always comes next.

forge_gate_addressed() { # forge_gate_addressed <plan> <id> <tdir> <wt> <attempt> <reviewed>
  local plan="$1" id="$2" tdir="$3" wt="$4" attempt="$5" reviewed="$6"
  declare -F forge_decide_active >/dev/null || return 0
  forge_decide_active retry-addressed || return 0
  [ "$attempt" -gt 1 ] 2>/dev/null || return 0
  [ "$(cat "$tdir/retry_findings.source" 2>/dev/null)" = review ] || return 0

  attempt_delta "$tdir" "$wt" "$reviewed"
  [ -s "$tdir/changes.since.diff" ] || return 0

  local rows rc=0 count row_id row_p row_finding
  rows="$(python3 "$SKILL_DIR/scripts/forge-jev.py" retry-addressed \
    --plan "$plan" --task "$id" --attempt "$attempt" --repo "$REPO" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$rows" ] || return 0

  count="$(printf '%s\n' "$rows" | grep -c .)"
  {
    echo "forge: these blocking findings look unaddressed by your change:"
    echo
    while IFS=$'\t' read -r row_id row_p row_finding; do
      printf -- '- %s\n' "$row_finding"
    done <<< "$rows"
    echo
    cat "$tdir/retry_findings.md"
  } > "$tdir/.bounce.md.tmp" && mv "$tdir/.bounce.md.tmp" "$tdir/bounce.md" || return 0
  : > "$tdir/bounced"
  rm -f "$tdir/qa.last"
  echo FAIL > "$tdir/status"
  note "$id: the retry leaves $count blocking finding(s) untouched — sent back without a review (see $tdir/bounce.md)"
  return 1
}
