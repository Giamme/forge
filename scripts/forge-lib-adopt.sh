#!/usr/bin/env bash
# `adopt` for forge-parallel.sh (#79): a commit made outside forge comes into a run's integration
# branch, reviewed on its own by the run's QA. Sourced; functions only.
#
#   forge-parallel.sh adopt <plan-dir> <commit|a..b> --reason "<why>" [--qa <spec>] [--approved]
#   forge-parallel.sh adopt <plan-dir> --head        --reason "<why>" [--qa <spec>] [--approved]
#
# The first cherry-picks onto the integration branch. --head takes what is already on it past the
# revision the run accepted, the case `review` refuses. Either way the reviewer sees only that range.
# PASS moves accepted.integration. A FAIL or an unknown verdict puts the branch back unless --approved
# overrides it, which is recorded; no verdict at all (an infrastructure stop) always puts it back.
# adopt-<epoch>-<pid>/ keeps the review; adoptions.tsv holds one row per adopted range: epoch, base,
# tip, verdict, approved (0|1), spec, the review's directory name, reason. There is no task row: nothing here
# was a model's implementation, and a final review reads the whole range anyway.

adopt_rows() { # adopt_rows <plan>: the adopted ranges, as lines for a prompt
  [ -s "$1/adoptions.tsv" ] || return 0
  echo "Commits adopted from outside this run, each reviewed alone (they are in the diff below):"
  awk -F'\t' '{ printf "  - %s..%s (%s%s): %s\n", substr($2, 1, 12), substr($3, 1, 12), $4, ($5 == 1 ? ", approved over it" : ""), $8 }' "$1/adoptions.tsv"
  echo
}

do_adopt() {
  local PLAN="${1:?adopt needs a plan dir}"; shift
  local target="" head=0 reason="" approved=0 spec=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --head) head=1; shift ;;
      --reason) [ $# -ge 2 ] || die "adopt: --reason needs a text"; reason="$2"; shift 2 ;;
      --qa) [ $# -ge 2 ] || die "adopt: --qa needs a spec"; spec="$2"; shift 2 ;;
      --approved) approved=1; shift ;;
      -*) die "adopt: unknown option '$1'" ;;
      *) [ -z "$target" ] || die "adopt: unexpected argument '$1'"; target="$1"; shift ;;
    esac
  done
  { [ -n "$target" ] && [ "$head" = 0 ]; } || { [ -z "$target" ] && [ "$head" = 1 ]; } \
    || die "usage: forge-parallel.sh adopt <plan-dir> <commit|a..b> | --head  --reason \"<why>\" [--qa <spec>] [--approved]"
  reason="$(printf '%s' "$reason" | tr '\n\r\t' '   ' | sed 's/^ *//; s/ *$//')"
  [ -n "$reason" ] || die "adopt needs --reason \"<why this commit belongs in the run>\": the reviewer judges against it, and it is recorded"

  local T="$PLAN/tasks.tsv" int_wt base now tip commits=() n
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'" 3
  [ -s "$PLAN/wt_root" ] && [ -s "$PLAN/run_id" ] && [ -s "$PLAN/repo" ] && [ -s "$PLAN/accepted.integration" ] \
    || die "this plan has never been run, so it has no integration branch to adopt into" 3
  int_wt="$(cat "$PLAN/wt_root")/_integration"
  [ -e "$int_wt/.git" ] || die "the integration worktree $int_wt is missing" 3
  [ -z "$(git -C "$int_wt" status --porcelain -- . ':(exclude).forge' 2>/dev/null)" ] \
    || die "the integration worktree $int_wt has uncommitted changes; commit them, or clean it, first" 3
  base="$(cat "$PLAN/accepted.integration")"
  now="$(git -C "$int_wt" rev-parse HEAD)" || die "cannot read the integration worktree $int_wt" 3

  if [ "$head" = 1 ]; then
    [ "$now" != "$base" ] || die "the integration branch is at the revision this run accepted; there is nothing to adopt" 3
    git -C "$int_wt" merge-base --is-ancestor "$base" "$now" \
      || die "the integration branch no longer contains the revision this run accepted ($(printf '%.12s' "$base")); it was rewritten, and adopt only takes commits on top of it" 3
  else
    [ "$now" = "$base" ] || die "the integration branch has moved past the revision this run accepted; adopt that first with --head" 3
    local range
    case "$target" in
      *..*) range="$target" ;;
      *) git -C "$int_wt" rev-parse --verify --quiet "$target^{commit}" >/dev/null || die "adopt: '$target' is not a commit here" 3
         range="$target^!" ;;
    esac
    # shellcheck disable=SC2207  # commit ids are single words
    commits=($(git -C "$int_wt" rev-list --reverse "$range" 2>/dev/null)) || die "adopt: '$target' is not a commit range here" 3
    [ "${#commits[@]}" -gt 0 ] || die "adopt: '$target' holds no commits" 3
    [ -z "$(git -C "$int_wt" rev-list --min-parents=2 "$range" 2>/dev/null)" ] \
      || die "adopt: '$target' includes a merge commit; adopt the commits it merged" 3
    for n in "${commits[@]}"; do
      git -C "$int_wt" merge-base --is-ancestor "$n" "$base" && die "adopt: $(printf '%.12s' "$n") is already on the integration branch" 3
    done
  fi

  # Everything that can refuse without a model goes first, so a bad spec moves nothing.
  [ -n "$spec" ] || spec="$(cat "$PLAN/final_qa" 2>/dev/null)"
  [ -n "$spec" ] || spec="$(field "$T" "$(all_ids "$T" | head -1)" qa)"
  [ -n "$spec" ] || die "adopt: no QA spec; name one with --qa" 3
  local out ft rc=0
  out="$PLAN/adopt-$(date +%s)-$$"
  mkdir -p "$out" && out="$(cd "$out" && pwd)" || die "adopt: cannot create $out" 3
  ft="$(forge_fast_tier "$PLAN" review)"
  FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$PLAN/capabilities}" \
    /bin/bash "$DISPATCH" doctor --spec "$spec" --role qa $ft > "$out/doctor.out" 2>&1 \
    || { cat "$out/doctor.out" >&2; die "adopt: QA spec '$spec' cannot be used (see above); nothing was applied" 3; }

  if [ "$head" = 0 ]; then
    if ! git -C "$int_wt" cherry-pick "${commits[@]}" > "$out/apply.log" 2>&1; then
      git -C "$int_wt" cherry-pick --abort >/dev/null 2>&1
      git -C "$int_wt" reset -q --hard "$base"
      die "adopt: '$target' does not apply cleanly on the integration branch (see $out/apply.log); nothing was adopted" 6
    fi
  fi
  tip="$(git -C "$int_wt" rev-parse HEAD)"

  # Back to the accepted revision, for every outcome that does not adopt. A range that was only on
  # this branch (--head) keeps a ref: the branch was its one home.
  adopt_rollback() {
    git -C "$int_wt" reset -q --hard "$base" || note "adopt: could not put the integration branch back at $base"
    if [ "$head" = 1 ]; then
      git -C "$int_wt" update-ref "refs/forge/adopt-refused/$(basename "$out")" "$tip" 2>/dev/null
      echo "the integration branch is back at $(printf '%.12s' "$base"); your commits are at $tip (ref refs/forge/adopt-refused/$(basename "$out"))"
    else
      echo "the integration branch is back at $(printf '%.12s' "$base")"
    fi
  }

  local review="$out/review" thr source_fp review_fp verdict gate_line decision kept
  forge_review_snapshot "$int_wt" "$base" "$tip" "$review" > "$out/snapshot.log" 2>&1 \
    || { adopt_rollback; die "adopt: cannot build the review snapshot (see $out/snapshot.log)" 3; }
  forge_review_deps "$int_wt" "$review" "$out/review.deps"
  source_fp="$(forge_fingerprint "$int_wt")" && review_fp="$(forge_fingerprint "$review" --tracked)" \
    || { adopt_rollback; die "adopt: cannot fingerprint the checkouts" 3; }
  thr="$(qa_threshold_for "$PLAN" __adopt__)"
  {
    if [ -s "$PLAN/goal.txt" ]; then echo "## The run's goal"; echo; cat "$PLAN/goal.txt"; echo; fi
    echo "## Why this is being adopted"; echo; printf '%s\n' "$reason"; echo
    echo "## The commits"; echo; git -C "$int_wt" log --format='%h %an: %s' "$base..$tip"; echo
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part head --kind adopt
    echo
    cat "$out/review.deps"
    echo "Forge ran no tests of its own on this range, so nothing here stands in for yours."
    echo
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part tail --kind adopt --threshold "$thr"
    echo
    echo '```diff'; git -C "$int_wt" diff --binary --irreversible-delete "$base" "$tip" -- . ':(exclude).forge'; echo '```'
  } > "$out/qa.input"

  FORGE_DATASET_TRIGGER=human FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$PLAN/capabilities}" \
    /bin/bash "$DISPATCH" qa "$spec" --repo "$review" --run-dir "$out" --review-base "$(cat "$out/review.base")" \
      --prompt-file "$out/qa.input" $ft --output "${FORGE_OUTPUT:-summary}" > "$out/qa.out" 2>&1
  rc=$?
  case "$rc" in
    0) ;;
    8) note "adopt: the reviewer hit an infrastructure failure; nothing was judged"
       review_infra "$out"; adopt_rollback; return 8 ;;
    2|3) note "adopt: the QA dispatch could not start (exit $rc); see $out/qa.out"
       adopt_rollback; return 3 ;;
    *) note "adopt: the QA dispatch failed (exit $rc); see $out/qa.out" ;;
  esac
  if [ "$(forge_fingerprint "$int_wt")" != "$source_fp" ] || [ "$(forge_fingerprint "$review" --tracked)" != "$review_fp" ]; then
    note "adopt: a checkout changed during the review, so its verdict says nothing"
    adopt_rollback; return 5
  fi

  verdict="$(forge_verdict "$out/qa.last" 2>/dev/null)"; verdict="${verdict:-UNKNOWN}"
  gate_line="$(python3 "$SKILL_DIR/scripts/forge-contract.py" gate --verdict "$verdict" --qa-last "$out/qa.last" \
      --threshold "$thr" --known-issues "$out/known_issues.md" 2>/dev/null)" || gate_line=""
  decision="$(printf '%s' "$gate_line" | awk -F'\t' '{print $1}')"
  [ -z "$gate_line" ] || printf '%s\t%s\n' "$thr" "$gate_line" > "$out/qa.gate"
  case "$decision" in accept) verdict=PASS ;; fail) verdict=FAIL ;; esac
  printf '%s\n' "$verdict" > "$out/verdict"

  kept=0
  if [ "$verdict" != PASS ]; then
    echo
    sed -n '1,200p' "$out/qa.last" 2>/dev/null
    echo
    if [ "$approved" = 1 ]; then
      kept=1; echo "adopt: the reviewer said $verdict; --approved keeps it, and that is recorded"
    else
      echo "adopt: the reviewer said $verdict, so nothing was adopted (see $out/qa.last)."
      adopt_rollback
      forge_dataset_event adopt --dir "$PLAN" --set base="$base" --set tip="$tip" --set verdict="$verdict" \
        --set approved=0 --set adopted=0 --set spec="$spec" --set reason="$reason"
      write_results "$PLAN"
      return 5
    fi
  fi

  printf '%s\n' "$tip" > "$PLAN/.accepted.integration.tmp" && mv "$PLAN/.accepted.integration.tmp" "$PLAN/accepted.integration" \
    || { adopt_rollback; die "adopt: could not record the accepted revision" 1; }
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "$base" "$tip" "$verdict" "$kept" "$spec" "$(basename "$out")" "$reason" \
    >> "$PLAN/adoptions.tsv"
  python3 - "$PLAN/control.log" "$base" "$tip" "$verdict" "$kept" "$spec" "$reason" "${USER:-${LOGNAME:-unknown}}" <<'PY' \
    || note "adopt: could not add this to control.log"
import json, sys, time
log, base, tip, verdict, kept, spec, reason, who = sys.argv[1:]
with open(log, 'a') as handle:
    handle.write(json.dumps(dict(at=time.time(), actor='cli', action='adopt', task=None, key='integration', value=tip,
                                 previous=base, verdict=verdict, approved=kept == '1', spec=spec, reason=reason, by=who)) + '\n')
PY
  forge_dataset_event adopt --dir "$PLAN" --set base="$base" --set tip="$tip" --set verdict="$verdict" \
    --set approved="$kept" --set adopted=1 --set spec="$spec" --set reason="$reason" \
    --set authors="$(git -C "$int_wt" log --format='%an' "$base..$tip" | sort -u | awk 'NR > 1 { printf "," } { printf "%s", $0 }')"
  echo "adopted $(printf '%.12s' "$base")..$(printf '%.12s' "$tip") ($verdict) into the integration branch"

  # As a retry does: the verification follows the merge, and never undoes it.
  local left="" lid
  for lid in $(all_ids "$T"); do
    case "$(task_status "$PLAN" "$lid")" in PENDING|BLOCKED) left="${left:+$left }$lid" ;; esac
  done
  write_results "$PLAN"
  if [ -n "$left" ]; then
    note "verification deferred: tasks still to run ($left); the next run, or integrate, verifies the integration branch"
  else
    verify_result "$PLAN" "$int_wt" "$PLAN" || { write_results "$PLAN"; run_report_extras "$PLAN"; return 5; }
  fi
  write_results "$PLAN"
  return 0
}
