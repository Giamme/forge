#!/usr/bin/env bash
# forge-solo.sh — the single-task forge run: one dwarf implements in the working
# tree, one QA reviews the diff it actually produced.
#
# This exists for the same reason forge-dispatch.sh does. The pipeline is a dozen
# steps — memory injection, prompt assembly, dispatch, diff capture, review prompt,
# a second dispatch, two ledger writes — and it used to live in SKILL.md as shell
# for the orchestrating model to retype on every invocation, from whichever of five
# harnesses was driving. Several of those steps are silently wrong rather than
# loudly broken when mistyped: drop ':(exclude).forge' from the capture and forge's
# own memory goes in front of the reviewer as if the dwarf had written it.
# Decomposed runs never had this problem because forge-parallel.sh owns the
# plumbing. Solo runs should not either.
#
# What stays with the orchestrator is the judgment: it writes prompt.md, it reads
# dwarf.last and qa.last, and it reports. Only the plumbing moved.
#
# Usage:
#   forge-solo.sh <run-dir> --repo <dir> --dwarf <spec> [--qa <spec>]
#                 [--approach <file>] [--yolo-dwarf] [--yolo-qa]
#                 [--native-review] [--no-memory] [--timeout <s>] [--dry-run]
#
# Reads  <run-dir>/prompt.md   the implementation instruction (required)
#        <run-dir>/goal.txt    one line, what the user asked for (optional)
# Writes <run-dir>/dwarf.{input,last,log,resolved}
#        <run-dir>/changes.diff
#        <run-dir>/qa.{input,last,log,resolved}
#        <run-dir>/verdict      PASS | FAIL | UNKNOWN, when qa emitted one
#
# Exit codes: 0 ok | 2 usage | 3 precondition | 4 a dispatch failed
#             5 the dwarf produced no changes | 7 a dispatch timed out
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCH="$SKILL_DIR/scripts/forge-dispatch.sh"
MEMORY="$SKILL_DIR/scripts/forge-memory.sh"
source "$SKILL_DIR/scripts/forge-artifact.sh"
source "$SKILL_DIR/scripts/forge-metrics.sh"

die()  { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }
note() { printf 'forge: %s\n' "$*" >&2; }

RUN="${1:-}"; [ -n "$RUN" ] || die "usage: forge-solo.sh <run-dir> --repo <dir> --dwarf <spec> [--qa <spec>]"
case "$RUN" in -*) die "first argument must be the run directory, got '$RUN'" ;; esac
shift

OUTPUT=summary
REPO="$PWD"; DWARF=""; QA="opus"; APPROACH=""; YD=""; YQ=""; NATIVE=""; TIMEOUT=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUTPUT="${2:?}"; shift 2 ;;
    --repo)       REPO="${2:?--repo needs a value}"; shift 2 ;;
    --dwarf)      DWARF="${2:?--dwarf needs a spec}"; shift 2 ;;
    --qa)         QA="${2:?--qa needs a spec}"; shift 2 ;;
    --approach)   APPROACH="${2:?--approach needs a file}"; shift 2 ;;
    --yolo-dwarf) YD="--yolo"; shift ;;
    --yolo-qa)    YQ="--yolo"; shift ;;
    --native-review) NATIVE="--native-review"; shift ;;
    --no-ripwire) export FORGE_RIPWIRE=off; shift ;;
    --no-memory)  export FORGE_MEMORY=off; shift ;;
    --timeout)    TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --dry-run)    DRY=1; shift ;;
    *) die "unknown option '$1'" ;;
  esac
done

case "$OUTPUT" in summary|full) ;; *) die "--output must be summary or full" ;; esac

# Forge never picks the model: every dispatch spends the user's quota, and choosing
# for them is not the tool's call.
[ -n "$DWARF" ] || die "--dwarf is required"
mkdir -p "$RUN" || die "cannot create run dir '$RUN'"
[ -s "$RUN/prompt.md" ] || die "no prompt.md in '$RUN' — write the implementation instruction there first"
[ -d "$REPO" ] || die "--repo '$REPO' is not a directory"
(cd "$REPO" && git rev-parse --git-dir >/dev/null 2>&1) \
  || die "'$REPO' is not a git repository — there is no way to capture a diff to review" 3

# The run dir must live outside the working tree: anything forge writes inside the
# repo turns up in the dwarf's own diff and reaches QA as if the dwarf wrote it.
case "$(cd "$RUN" && pwd)/" in
  "$(cd "$REPO" && pwd)"/*) die "run dir '$RUN' is inside the repo; put it somewhere else or it lands in the diff" ;;
esac

RUN="$(cd "$RUN" && pwd)"
REPO="$(cd "$REPO" && pwd)"
RUN_ID="$(basename "$RUN")"
export FORGE_CAPABILITY_CACHE="$RUN/capabilities"
TFLAG=""; [ -n "$TIMEOUT" ] && TFLAG="--timeout $TIMEOUT"
GOAL="$(head -1 "$RUN/goal.txt" 2>/dev/null || true)"

[ "$DRY" = 1 ] || forge_metric_begin "$RUN" solo

# --- dwarf --------------------------------------------------------------------
/bin/bash "$MEMORY" inject "$REPO" dwarf > "$RUN/dwarf.memory"
python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$RUN/prompt.md" --approach "$APPROACH" --memory "$RUN/dwarf.memory" > "$RUN/context.md" || exit 3
{
  cat "$RUN/context.md"
  cat <<'EOF'

Edit the files in this repository directly. When you are done, run whatever tests
or checks the project already has and report the result.

You are running headless: nobody is at the other end and no one can answer a
question. If something is ambiguous, pick the most reasonable interpretation,
proceed, and state the assumption in your final message.
EOF
  echo; /bin/bash "$MEMORY" note dwarf
} > "$RUN/dwarf.input"

if [ "$DRY" = 1 ]; then
  echo "--- dry run ---"
  /bin/bash "$DISPATCH" dwarf "$DWARF" --repo "$REPO" --run-dir "$RUN" \
    --ripwire-query-file "$RUN/prompt.md" --prompt-file "$RUN/dwarf.input" $YD $TFLAG --dry-run
  echo
  echo "qa would be: $QA $NATIVE $YQ"
  echo "dwarf prompt: $RUN/dwarf.input ($(wc -l < "$RUN/dwarf.input" | tr -d ' ') lines)"
  exit 0
fi

forge_metric_phase preflight
/bin/bash "$DISPATCH" doctor --spec "$DWARF" --role dwarf $YD $TFLAG || exit $?
/bin/bash "$DISPATCH" doctor --spec "$QA" --role qa $YQ $NATIVE $TFLAG || exit $?
/bin/bash "$SKILL_DIR/scripts/forge-install-ripwire.sh" || true
forge_metric_phase snapshot
START="$(forge_tree "$REPO")" || die "cannot record starting snapshot" 3
echo "$START" > "$RUN/start.tree"
git -C "$REPO" diff --binary HEAD "$START" -- . ':(exclude).forge' > "$RUN/existing.diff"
rm -f "$RUN/verdict"
forge_metric_phase dispatch
note "dwarf $DWARF in $REPO"
/bin/bash "$DISPATCH" dwarf "$DWARF" --repo "$REPO" --run-dir "$RUN" \
  --ripwire-query-file "$RUN/prompt.md" --prompt-file "$RUN/dwarf.input" $YD $TFLAG --output "$OUTPUT"
rc=$?
dur_dwarf="$(sed -n 's/^duration_s=//p' "$RUN/dwarf.resolved" 2>/dev/null | tail -1)"
/bin/bash "$MEMORY" record "$REPO" --last "$RUN/dwarf.last" --role dwarf \
  --run-id "$RUN_ID" --model "$DWARF" --duration "${dur_dwarf:--}" >/dev/null 2>&1
[ "$rc" = 7 ] && die "the dwarf exceeded its timeout — see $RUN/dwarf.log" 7
[ "$rc" -ne 0 ] && die "the dwarf dispatch failed — see $RUN/dwarf.log" 4

forge_metric_phase snapshot
# --- the real diff ------------------------------------------------------------
# Diff complete filesystem snapshots; the private index preserves user staging.
END="$(forge_tree "$REPO")" || die "cannot capture implementation" 3
echo "$END" > "$RUN/reviewed.tree"
git -C "$REPO" diff --binary "$START" "$END" -- . ':(exclude).forge' > "$RUN/changes.diff" || die "cannot create diff" 3

if [ ! -s "$RUN/changes.diff" ]; then
  note "the dwarf produced no changes at all — see $RUN/dwarf.last for what it said"
  echo NOCHANGES > "$RUN/verdict"
  exit 5
fi

REVIEW="$RUN/review-$(date +%s)-$$"
forge_review_snapshot "$REPO" "$START" "$END" "$REVIEW" || die "cannot create review snapshot" 3
SOURCE_FP="$(forge_fingerprint "$REPO")" || exit 3
[ "$(forge_tree "$REPO")" = "$END" ] || { echo INVALIDATED > "$RUN/verdict"; die "source changed while preparing QA" 5; }
REVIEW_FP="$(forge_fingerprint "$REVIEW")" || exit 3
printf '%s\n' "$SOURCE_FP" > "$RUN/source.fingerprint"
printf '%s\n' "$REVIEW_FP" > "$RUN/review.fingerprint"
forge_metric_phase preparation
# --- qa -----------------------------------------------------------------------
/bin/bash "$MEMORY" inject "$REPO" qa > "$RUN/qa.memory"
{
  python3 "$SKILL_DIR/scripts/forge-prompt.py" --requirements "$RUN/prompt.md" --approach "$APPROACH" --goal "$RUN/goal.txt" --memory "$RUN/dwarf.memory" --memory "$RUN/qa.memory"
  cat <<'EOF'
Review the diff below for correctness bugs: logic errors, broken edge cases, wrong
behaviour versus what was asked. Also say if it solved a different problem than the
one stated, or changed files outside the goal's scope.

Report findings only — do not edit any file. For each finding give the file and
line, what breaks, and a concrete input that triggers it. Mark each CONFIRMED if
you traced it in the code, or PLAUSIBLE if you could not fully verify it. If the
diff is correct, say so plainly rather than inventing something to report.

End your reply with exactly one line:
  FORGE_VERDICT: PASS   — no confirmed correctness bug (style nits are not failures)
  FORGE_VERDICT: FAIL   — at least one CONFIRMED correctness bug
EOF
  echo
  echo '```diff'; cat "$RUN/changes.diff"; echo '```'
  echo; /bin/bash "$MEMORY" note qa
  echo "Place any learning notes before the final FORGE_VERDICT: PASS or FORGE_VERDICT: FAIL line."
} > "$RUN/qa.input"

forge_metric_phase dispatch
note "qa $QA on $(wc -l < "$RUN/changes.diff" | tr -d ' ') diff lines"
/bin/bash "$DISPATCH" qa "$QA" --repo "$REVIEW" --review-base "$(cat "$RUN/review.base")" --run-dir "$RUN" \
  --ripwire-query-file "$RUN/prompt.md" --prompt-file "$RUN/qa.input" $YQ $NATIVE $TFLAG --output "$OUTPUT"
rc=$?
forge_metric_phase verification
verdict="$(forge_verdict "$RUN/qa.last")"
[ -n "$NATIVE" ] && verdict=UNKNOWN
if [ "$(forge_fingerprint "$REPO")" != "$SOURCE_FP" ] || [ "$(forge_fingerprint "$REVIEW")" != "$REVIEW_FP" ]; then
  echo INVALIDATED > "$RUN/verdict"
  die "source or review artifact changed during QA; review invalidated" 5
fi
dur_qa="$(sed -n 's/^duration_s=//p' "$RUN/qa.resolved" 2>/dev/null | tail -1)"
/bin/bash "$MEMORY" record "$REPO" --last "$RUN/qa.last" --role qa \
  --run-id "$RUN_ID" --model "$QA" --verdict "${verdict:-UNKNOWN}" \
  --duration "${dur_qa:--}" >/dev/null 2>&1
[ "$rc" = 7 ] && die "qa exceeded its timeout — the dwarf's changes are still in the tree; see $RUN/qa.log" 7
[ "$rc" -ne 0 ] && die "the qa dispatch failed — the dwarf's changes are still in the tree; see $RUN/qa.log" 4

# --native-review is codex's own reviewer, which refuses a custom prompt alongside
# its scope flags — so it never sees the verdict instruction and cannot emit one.
echo "${verdict:-UNKNOWN}" > "$RUN/verdict"

echo
echo "dwarf:  $DWARF   ${dur_dwarf:-?}s"
echo "qa:     $QA   ${dur_qa:-?}s   verdict: ${verdict:-UNKNOWN}"
echo "diff:   $RUN/changes.diff"
echo "read:   $RUN/dwarf.last and $RUN/qa.last"
echo
echo "The changes are UNCOMMITTED in $REPO. Nothing was committed, pushed or reset."
