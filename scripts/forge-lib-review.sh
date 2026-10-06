#!/usr/bin/env bash
# Opt-in holistic final review for forge-parallel.sh. Sourced; functions only, nothing
# runs at source time, so a plan that never asks for it never spends a model call on it.
#
# Per-task QA only ever sees one slice of the work. This reviews the COMBINED result once,
# with the goal and every task's requirements in front of the reviewer, to catch what no
# slice can show: an interface one task changed and another consumes, an ordering race
# across modules, a contract regression that only appears when the pieces run together.
#
# Two entry points share one worker, final_review:
#   review <plan> [--qa <spec>] [--yolo-qa] [--qa-threshold Pn] [--output summary|full]
#       read-only: reviews the integration branch's combined work (base..tip) and writes
#       $PLAN/final-review-<epoch>-<pid>/. Never touches the user's branch or the
#       integration branch.
#   integrate <plan> --approved --final-review <spec>
#       after verification and before the user's branch moves, reviews parent..candidate
#       (final_review_for_integrate); anything but a pass stops the integration.
#
# Exit codes (both): 0 passed (or a FAIL accepted by the --qa-threshold gate) | 2 usage
# (ambiguous QA spec, unknown option) | 3 precondition | 5 blocked (FAIL, no verdict,
# mutated during review, reviewer could not finish) | 8 the reviewer hit an infrastructure
# failure (quota, auth, network): nothing was judged.
#
# Artifacts, in the review directory (review: $PLAN/final-review-<epoch>-<pid>/, integrate:
# <integrate-dir>/final-review/): qa.input (the prompt), qa.last (the reviewer's report),
# qa.out, verdict (PASS | FAIL | UNKNOWN | INVALIDATED | ACCEPTED; absent when nothing was
# judged), qa.gate and known_issues.md (with a threshold), qa.infra (infrastructure stop),
# review.meta, context.md, notes.md, tasks.summary, combined.diff (when inlined),
# source/review.fingerprint and review-<pid>/ (the disposable checkout the reviewer ran in).
# `review` also keeps $PLAN/final-review (a link to the newest directory) and
# $PLAN/final-review.verdict (a copy of its verdict).
#
# Sourced by forge-parallel.sh, which provides die/note/field/all_ids/task_status,
# forge_pp_flag, SKILL_DIR and DISPATCH.

REVIEW_REQ_BUDGET=81920       # bytes of task requirements put in front of the reviewer
REVIEW_DIFF_LIMIT=409600      # largest combined diff placed inline in the prompt

# --- small helpers ---------------------------------------------------------------------
review_threshold() { # the plan-level blocking threshold, or nothing
  [ -s "$1/qa_threshold" ] && head -1 "$1/qa_threshold" | tr -d ' \r'
  return 0
}

review_yolo_flag() { # review_yolo_flag <plan> [force] -> "--yolo" or nothing
  if [ -n "${2:-}" ] || [ -f "$1/yolo_qa" ] || [ -f "$1/final_yolo" ]; then echo --yolo; fi
  return 0
}

# given > persisted $PLAN/final_qa > the one QA spec every task shares > ask. Only the
# first two are an explicit choice, so only a unanimous task table is trusted to fill in.
review_resolve_spec() { # review_resolve_spec <plan> [<given>] -> spec on stdout; rc 2 when ambiguous
  local plan="$1" given="${2:-}" saved specs n list
  if [ -n "$given" ]; then printf '%s\n' "$given"; return 0; fi
  saved="$(head -1 "$plan/final_qa" 2>/dev/null | tr -d ' \r')"
  if [ -n "$saved" ]; then printf '%s\n' "$saved"; return 0; fi
  specs="$(awk -F'\t' '!/^#/ && NF && $6 != "" {print $6}' "$plan/tasks.tsv" | sort -u)"
  n="$(printf '%s\n' "$specs" | awk 'NF' | wc -l | tr -d ' ')"
  if [ "$n" = 1 ]; then printf '%s\n' "$specs"; return 0; fi
  list="$(printf '%s\n' "$specs" | awk 'NF' | tr '\n' ' ')"
  if [ "$n" = 0 ]; then
    note "review: no QA spec found in $plan/tasks.tsv; choose one with --qa <spec>"
  else
    note "review: this plan's tasks use $n different QA specs: ${list% }"
    note "review: choose one for the whole-run review with --qa <spec> (it is remembered in $plan/final_qa)"
  fi
  return 2
}

review_task_table() { # id <TAB> status <TAB> title <TAB> files, one row per task
  local plan="$1" id
  for id in $(all_ids "$plan/tasks.tsv"); do
    printf '%s\t%s\t%s\t%s\n' "$id" "$(task_status "$plan" "$id")" \
      "$(field "$plan/tasks.tsv" "$id" title)" "$(field "$plan/tasks.tsv" "$id" files)"
  done
}

# Tasks that are not MERGED are not in the combined diff, so say so rather than let the
# reviewer or the user assume the whole plan was judged.
review_warn_unmerged() { # review_warn_unmerged <plan>
  local plan="$1" id st list=""
  for id in $(all_ids "$plan/tasks.tsv"); do
    st="$(task_status "$plan" "$id")"
    [ "$st" = MERGED ] || list="$list $id($st)"
  done
  [ -z "$list" ] || note "WARNING: not merged, so NOT part of the reviewed result:$list"
  return 0
}

# The prompt is assembled in two halves (before and after the contract's head) by one
# python helper: it needs byte-accurate truncation, which is miserable in bash 3.2.
#   pre    goal, task table, every task's complete requirements (bounded, visibly truncated)
#   notes  what forge observed that no per-task reviewer was shown
review_context() { # review_context <pre|notes> <plan> <tasks.summary> <verification-dir>
  python3 - "$1" "$2" "$3" "$4" "$REVIEW_REQ_BUDGET" <<'PY'
import os, re, sys

mode, plan, summary, vdir, budget = sys.argv[1:6]
BUDGET = int(budget)


def read(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read()
    except OSError:
        return None


def text(raw):
    return raw.decode('utf-8', 'replace').strip()


rows = []
raw = read(summary)
for line in (raw or b'').decode('utf-8', 'replace').splitlines():
    cells = line.split('\t')
    if cells and cells[0]:
        rows.append((cells + ['', '', '', ''])[:4])

out = []
if mode == 'pre':
    goal = read(os.path.join(plan, 'goal.txt'))
    out += ['## Overall goal', text(goal) if goal and goal.strip() else
            '(this plan recorded no goal; judge the change against the task requirements below)', '']
    out += ['## Tasks in this run',
            'Each task was implemented on its own branch by a separate implementer and reviewed on',
            'its own slice only. Only MERGED tasks are part of the combined diff you are reviewing.', '']
    width = max([len(r[0]) for r in rows] + [2])
    for tid, status, title, files in rows:
        out.append('  %-*s  %-10s %s   [files: %s]' % (width, tid, status, title, files))
    out.append('')

    entries = []
    for tid, status, title, files in rows:
        path = os.path.join(plan, 'tasks', tid, 'prompt.md')
        entries.append((tid, status, title, path, read(path)))
    lengths = [len(b) for *_, b in entries if b is not None]
    cap = None
    if sum(lengths) > BUDGET:
        # Cut only the longest requirements: find the largest per-task size that fits.
        lo, hi = 0, max(lengths)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if sum(min(n, mid) for n in lengths) <= BUDGET:
                lo = mid
            else:
                hi = mid - 1
        cap = lo
    out += ['## Complete requirements of each task (as given to its implementer)', '']
    for tid, status, title, path, body in entries:
        tag = status if status == 'MERGED' else '%s: NOT in the diff below' % status
        out.append('### Task %s: %s [%s]' % (tid, title, tag))
        if body is None:
            out += ['(no prompt.md was recorded for this task)', '']
            continue
        if cap is not None and len(body) > cap:
            kept = body[:cap].decode('utf-8', 'ignore')
            removed = len(body) - len(kept.encode('utf-8'))
            out += [kept.rstrip(),
                    '[... truncated %d bytes; full text at %s ...]' % (removed, path), '']
        else:
            out += [text(body), '']
else:
    sections = []

    def capped(raw_bytes, limit=6144):
        t = text(raw_bytes)
        if len(t.encode('utf-8')) > limit:
            t = t.encode('utf-8')[:limit].decode('utf-8', 'ignore').rstrip() + '\n[... truncated ...]'
        return t

    def per_task(name, title, intro, findings_only=False):
        found = []
        for tid, status, _t, _f in rows:
            raw_bytes = read(os.path.join(plan, 'tasks', tid, name))
            if raw_bytes and raw_bytes.strip():
                lines = capped(raw_bytes).splitlines()
                if findings_only:
                    # known_issues.md opens with a heading and a paragraph about the gate; the
                    # reviewer needs the findings, not forge's bookkeeping.
                    start = next((i for i, l in enumerate(lines) if re.match(r'\s*(?:[-*]|\d+[.)])\s', l)), 0)
                    lines = lines[start:]
                found.append('Task %s:\n%s' % (tid, '\n'.join('    ' + l for l in lines)))
        if found:
            sections.append('%s\n%s\n\n%s' % (title, intro, '\n'.join(found)))

    per_task('known_issues.md', 'Known issues already accepted for the individual tasks:',
             'These findings were recorded and tolerated; do not re-report them unless they are worse than stated.',
             findings_only=True)
    per_task('drift.txt', 'Scope drift (files a task changed without declaring them):',
             'Another task may own these files; check the combined effect.')
    per_task('guard.txt', 'Implementer guard notes (observed by forge, not claimed by the implementers):',
             'A promise of later work means a check may never have run.')
    ver = []
    status = read(os.path.join(vdir, 'verification.status'))
    if status and status.strip():
        ver.append('status: %s' % text(status))
        cmd = read(os.path.join(vdir, 'verification.command'))
        if cmd and cmd.strip():
            ver.append('command: %s' % text(cmd))
    flaky = read(os.path.join(vdir, 'verification.flaky'))
    if flaky and flaky.strip():
        ver.append('FLAKY: the command failed once and passed on rerun, so the pass is not clean:')
        ver += ['  ' + l for l in text(flaky).splitlines() if not l.startswith(('first_log=', 'retry_log='))]
    cover = read(os.path.join(vdir, 'verification.coverage.txt'))
    if cover and cover.strip():
        ver.append('suites the project defines that the verify command does not run:')
        ver += ['  ' + l for l in capped(cover).splitlines()]
    if ver:
        sections.append('Verification of the combined result (run by forge, not by you):\n'
                        + '\n'.join('  ' + l for l in ver))
    out += ['## Context the per-task reviewers did not have']
    if sections:
        out += ['Forge recorded the following about the combined run.', '']
        for s in sections:
            out += [s, '']
    out += ['Your working directory is a disposable checkout of the combined result, committed',
            'over its base. Do not modify it; a change invalidates this review.', '']
sys.stdout.buffer.write(('\n'.join(out) + '\n').encode('utf-8'))
PY
}

review_infra() { # review_infra <outdir>: what an infrastructure stop means, from qa.infra
  local out="$1" cls after
  cls="$(sed -n 's/^class=//p' "$out/qa.infra" 2>/dev/null | head -1)"
  after="$(sed -n 's/^retry_after=//p' "$out/qa.infra" 2>/dev/null | head -1)"
  echo
  echo "final review paused: the reviewer hit an infrastructure failure (${cls:-unknown}) before judging anything."
  echo "  evidence: $out/qa.infra"
  case "$after" in ''|*[!0-9]*) ;; *) echo "  the provider suggests waiting about ${after}s" ;; esac
  echo "  fix the cause (quota, login, network); no verdict was recorded."
}

# Mirror the newest result into the plan dir: $PLAN/final-review (a link to its directory)
# and $PLAN/final-review.verdict. A review that never reached the reviewer leaves both as
# they were; one that ended without a verdict (an infrastructure stop) clears the old
# verdict, because an earlier PASS must not be mistaken for the latest result.
review_publish_latest() { # review_publish_latest <plan> <outdir>
  local plan="$1" out="$2" rel="$2"
  [ -s "$out/qa.input" ] || return 0
  case "$out" in "$plan"/*) rel="${out#"$plan"/}" ;; esac
  python3 - "$plan" "$rel" <<'PY' 2>/dev/null || true
import os, sys
plan, rel = sys.argv[1:3]
link = os.path.join(plan, 'final-review')
tmp = '%s.tmp%d' % (link, os.getpid())
try:
    os.symlink(rel, tmp)
    os.replace(tmp, link)
except OSError:
    try:
        os.unlink(tmp)
    except OSError:
        pass
PY
  if [ -s "$out/verdict" ]; then cp "$out/verdict" "$plan/final-review.verdict"
  else rm -f "$plan/final-review.verdict"; fi
  return 0
}

# --- the shared worker -----------------------------------------------------------------
# final_review <plan> <base-rev> <tip-rev> <src> <outdir> <qa-spec> [<yolo-flag>]
#   src is a repository or worktree that contains both revisions; it is also the checkout
#   whose fingerprint must not change while the reviewer runs. Never uses `die`/`exit`:
#   it is called from inside do_integrate.
final_review() {
  local plan="$1" base="$2" tip="$3" src="$4" out="$5" spec="$6" yolo="${7:-}"
  local review n rc verdict thr gate_line source_fp review_fp vdir tflag="" inline=1 diff_n
  [ -n "$yolo" ] && yolo=--yolo
  [ -n "$spec" ] || { note "final review: no QA spec given"; return 3; }
  mkdir -p "$out" && out="$(cd "$out" && pwd)" || { note "final review: cannot create '$out'"; return 3; }
  vdir="$(dirname "$out")"
  [ -f "$vdir/verification.status" ] || vdir="$plan"
  thr="$(review_threshold "$plan")"
  rm -f "$out/verdict" "$out/qa.gate" "$out/known_issues.md" "$out/qa.infra"

  git -C "$src" rev-parse --verify --quiet "$base^{commit}" >/dev/null &&
    git -C "$src" rev-parse --verify --quiet "$tip^{commit}" >/dev/null ||
    { note "final review: '$base' or '$tip' is not a commit in $src"; return 3; }
  if [ "$(git -C "$src" rev-parse "$base^{tree}")" = "$(git -C "$src" rev-parse "$tip^{tree}")" ]; then
    note "final review: the combined result is identical to its base; there is nothing to review"
    return 3
  fi

  # Offline, and before anything is built or paid for: a spec that cannot resolve fails here.
  FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$plan/capabilities}" \
    /bin/bash "$DISPATCH" doctor --spec "$spec" --role qa $yolo > "$out/doctor.out" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    cat "$out/doctor.out" >&2
    note "final review: QA spec '$spec' cannot be used (see above)"
    return "$rc"
  fi

  # `forge_review_snapshot` writes review.base beside the snapshot, so it gets a directory
  # of its own under the artifact dir.
  review="$out/review-$$"; n=0
  while [ -e "$review" ]; do n=$((n+1)); review="$out/review-$$-$n"; done
  forge_review_snapshot "$src" "$base" "$tip" "$review" > "$out/snapshot.log" 2>&1 ||
    { note "final review: cannot build the review snapshot (see $out/snapshot.log)"; return 3; }
  source_fp="$(forge_fingerprint "$src")" && review_fp="$(forge_fingerprint "$review")" ||
    { note "final review: cannot fingerprint the checkouts"; return 3; }
  printf '%s\n' "$source_fp" > "$out/source.fingerprint"
  printf '%s\n' "$review_fp" > "$out/review.fingerprint"
  {
    echo "base=$base"; echo "tip=$tip"; echo "source=$src"; echo "spec=$spec"; echo "threshold=${thr:-none}"
  } > "$out/review.meta"

  review_task_table "$plan" > "$out/tasks.summary"
  review_context pre "$plan" "$out/tasks.summary" "$vdir" > "$out/context.md" &&
    review_context notes "$plan" "$out/tasks.summary" "$vdir" > "$out/notes.md" ||
    { note "final review: cannot assemble the review context"; return 3; }

  # At most the limit plus one byte is ever read, so an enormous change cannot fill the disk.
  git -C "$src" diff --binary "$base" "$tip" -- . ':(exclude).forge' 2>/dev/null |
    head -c $((REVIEW_DIFF_LIMIT + 1)) > "$out/combined.diff"
  diff_n="$(wc -c < "$out/combined.diff" | tr -d ' ')"
  if [ "$diff_n" -gt "$REVIEW_DIFF_LIMIT" ]; then inline=0; rm -f "$out/combined.diff"; fi

  {
    cat "$out/context.md"
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part head --kind final
    echo
    cat "$out/notes.md"
    python3 "$SKILL_DIR/scripts/forge-contract.py" qa --part tail --kind final ${thr:+--threshold "$thr"}
    echo
    if [ "$inline" = 1 ]; then
      echo '```diff'; cat "$out/combined.diff"; echo '```'
    else
      # The snapshot presents the work as ONE commit over the base, so HEAD~1..HEAD is it.
      echo "The combined diff is larger than $((REVIEW_DIFF_LIMIT / 1024)) KB, so it is not inlined. Read it in your"
      echo "working directory, which holds the combined result as the last commit over its base:"
      echo "  git diff HEAD~1 HEAD -- . ':(exclude).forge'"
      echo "(and 'git diff --stat HEAD~1 HEAD' for the overview)."
    fi
  } > "$out/qa.input"

  [ -s "$plan/timeout" ] && tflag="--timeout $(cat "$plan/timeout")"
  note "final review: $spec on $(git -C "$src" diff --shortstat "$base" "$tip" -- . ':(exclude).forge' 2>/dev/null | sed 's/^ *//')"
  FORGE_CAPABILITY_CACHE="${FORGE_CAPABILITY_CACHE:-$plan/capabilities}" \
    /bin/bash "$DISPATCH" qa "$spec" --repo "$review" --run-dir "$out" \
      --review-base "$(cat "$out/review.base")" --ripwire-query-file "$out/context.md" \
      --prompt-file "$out/qa.input" $yolo $tflag --output "${FORGE_OUTPUT:-summary}" > "$out/qa.out" 2>&1
  rc=$?
  if [ "${FORGE_OUTPUT:-summary}" = full ]; then cat "$out/qa.out"; fi
  case "$rc" in
    0) ;;
    8) note "final review: the reviewer hit an infrastructure failure; nothing was judged"
       review_infra "$out"
       return 8 ;;
    2|3) note "final review: the QA dispatch could not start (exit $rc); see $out/qa.out"
         return 3 ;;
    *) printf 'UNKNOWN\n' > "$out/verdict"
       echo
       if [ "$rc" = 7 ]; then
         echo "final review: the reviewer hit the dispatch timeout and was stopped; nothing was concluded (blocking)."
         echo "  see $out/qa.out; raise it with FORGE_TIMEOUT=<seconds> or 'run --timeout S' and review again"
       else
         echo "final review: the QA dispatch failed (exit $rc); nothing was concluded (blocking)."
         echo "  see $out/qa.out"
       fi
       return 5 ;;
  esac

  if [ "$(forge_fingerprint "$src")" != "$source_fp" ] || [ "$(forge_fingerprint "$review")" != "$review_fp" ]; then
    printf 'INVALIDATED\n' > "$out/verdict"
    note "final review: the checkout changed while the reviewer ran; the review is invalid"
    echo
    echo "final review ($spec): verdict INVALIDATED (blocking) — a file was modified during the review, so it cannot be trusted."
    echo "  report: $out/qa.last"
    return 5
  fi

  verdict=""
  [ -s "$out/qa.last" ] && verdict="$(forge_verdict "$out/qa.last")"
  verdict="${verdict:-UNKNOWN}"
  if [ -n "$thr" ]; then
    # Same gate as per-task QA: a FAIL made only of labelled findings below the threshold
    # is accepted with known issues; anything unlabelled stays blocking.
    gate_line="$(python3 "$SKILL_DIR/scripts/forge-contract.py" gate --verdict "$verdict" \
        --qa-last "$out/qa.last" --threshold "$thr" --known-issues "$out/known_issues.md" 2>/dev/null)" || gate_line=""
    if [ -n "$gate_line" ]; then
      printf '%s\t%s\n' "$thr" "$gate_line" > "$out/qa.gate"
      if [ "$(printf '%s' "$gate_line" | awk -F'\t' '{print $1}')" = accept ]; then verdict=ACCEPTED; fi
    fi
  fi
  printf '%s\n' "$verdict" > "$out/verdict"

  echo
  case "$verdict" in
    PASS)
      echo "final review ($spec): verdict PASS — the reviewer found no confirmed defect in the combined result."
      echo "  report: $out/qa.last" ;;
    ACCEPTED)
      echo "final review ($spec): the reviewer said FAIL, but every confirmed finding is less severe than the $thr threshold — accepted with known issues."
      echo "  known issues: $out/known_issues.md"
      echo "  report: $out/qa.last" ;;
    FAIL)
      echo "final review ($spec): verdict FAIL (blocking) — the reviewer confirmed at least one defect in the combined result."
      echo "  findings: $out/qa.last" ;;
    *)
      echo "final review ($spec): verdict UNKNOWN (blocking) — the reviewer ended without a FORGE_VERDICT line, so nothing was concluded."
      echo "  read: $out/qa.last" ;;
  esac
  case "$verdict" in PASS|ACCEPTED) return 0 ;; esac
  return 5
}

# --- integrate hook ----------------------------------------------------------------------
# Called by do_integrate after verification passed and before the user's branch can move.
final_review_for_integrate() { # <plan> <parent> <candidate_sha> <candidate> <out> <spec>
  local plan="$1" parent="$2" sha="$3" candidate="$4" out="$5" spec="${6:-}" rc
  [ -n "$spec" ] || { note "final review: --final-review needs a QA spec"; return 3; }
  printf '%s' "$spec" > "$plan/final_qa"
  note "final review of the combined candidate before it touches your branch"
  final_review "$plan" "$parent" "$sha" "$candidate" "$out/final-review" "$spec" "$(review_yolo_flag "$plan")"
  rc=$?
  review_publish_latest "$plan" "$out/final-review"
  case "$rc" in
    0) echo "  the final review allows the merge; continuing." ;;
    8) echo "  nothing was merged and your branch is untouched. Fix the cause, then run the same command again:"
       echo "    forge-parallel.sh integrate $plan --approved --final-review $spec" ;;
    5) echo
       echo "integration stopped by the final review: nothing was merged and your branch is untouched."
       echo "  The reviewer's findings are in $out/final-review/qa.last; address them, then integrate again."
       echo "  The combined candidate is still at $candidate for inspection." ;;
  esac
  return "$rc"
}

# --- the subcommand ----------------------------------------------------------------------
do_review() {
  local PLAN="${1:?review needs a plan dir}"; shift
  local qa_arg="" yolo_given="" routput=summary
  while [ $# -gt 0 ]; do
    case "$1" in
      --qa)         qa_arg="${2:?--qa needs a spec}"; shift 2 ;;
      --yolo-qa)    yolo_given=1; shift ;;
      --output)     routput="${2:?--output needs summary or full}"; shift 2 ;;
      --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
      *) if forge_pp_flag review "$PLAN" "$@"; then shift "$PP_SHIFT"; else die "review: unknown option '$1'"; fi ;;
    esac
  done
  case "$routput" in summary|full) ;; *) die "--output must be summary or full" ;; esac
  export FORGE_OUTPUT="$routput"

  local T="$PLAN/tasks.tsv" REPO run_id int_br int_wt tip base spec yolo out rc id merged=0
  [ -s "$T" ] || die "no tasks.tsv in '$PLAN'" 3
  [ -s "$PLAN/wt_root" ] || die "this plan has never been run; there is no combined result to review" 3
  REPO="$(cat "$PLAN/repo")"; run_id="$(cat "$PLAN/run_id")"
  int_br="forge/$run_id-integration"; int_wt="$(cat "$PLAN/wt_root")/_integration"
  tip="$(git -C "$REPO" rev-parse --verify --quiet "$int_br^{commit}")" ||
    die "integration branch $int_br does not exist; run the plan first" 3
  if [ ! -s "$PLAN/accepted.integration" ] || [ "$tip" != "$(cat "$PLAN/accepted.integration")" ]; then
    die "$int_br is not at the revision this run accepted (it moved outside forge); refusing to review something forge did not merge" 3
  fi
  [ -e "$int_wt/.git" ] || die "the integration worktree $int_wt is missing; nothing to take a trustworthy source fingerprint from" 3
  for id in $(all_ids "$T"); do [ "$(task_status "$PLAN" "$id")" = MERGED ] && merged=$((merged+1)); done
  [ "$merged" -gt 0 ] || die "no task has been merged, so there is no combined result to review" 3

  if [ -s "$PLAN/integration.base" ]; then
    base="$(cat "$PLAN/integration.base")"
  else
    # Plans created before integration.base existed: where the branch forked from the user's.
    base="$(git -C "$REPO" merge-base "$int_br" HEAD 2>/dev/null)"
    note "review: no integration.base recorded for this plan; using the merge-base with HEAD (${base:-none})"
  fi
  base="$(git -C "$REPO" rev-parse --verify --quiet "${base:-none}^{commit}")" ||
    die "cannot tell where $int_br started; there is no base to diff against" 3

  spec="$(review_resolve_spec "$PLAN" "$qa_arg")" || return $?
  printf '%s' "$spec" > "$PLAN/final_qa"
  [ -z "$yolo_given" ] || : > "$PLAN/final_yolo"
  yolo="$(review_yolo_flag "$PLAN" "$yolo_given")"
  review_warn_unmerged "$PLAN"

  out="$PLAN/final-review-$(date +%s)-$$"
  note "reviewing $int_br as a whole ($merged of $(all_ids "$T" | wc -l | tr -d ' ') task(s) merged); your branch and the integration branch are not touched"
  final_review "$PLAN" "$base" "$tip" "$int_wt" "$out" "$spec" "$yolo"
  rc=$?
  review_publish_latest "$PLAN" "$out"
  echo
  case "$rc" in
    0) echo "Nothing was changed. If you are satisfied, bring the result into your branch with"
       echo "  forge-parallel.sh integrate $PLAN --approved" ;;
    5) echo "Nothing was merged or changed. The integration branch is exactly as it was."
       echo "Act on the findings with a follow-up task (the reviewer's report is the brief), or"
       echo "'forge-parallel.sh retry $PLAN <task-id>' for a task that is not MERGED; then review again." ;;
    8) echo "Nothing was judged and nothing was changed. Fix the cause, then run again:"
       echo "  forge-parallel.sh review $PLAN" ;;
  esac
  return "$rc"
}
