#!/usr/bin/env bash
# forge-dispatch.sh — resolve a forge spec (alias[:effort[:harness]]) into a real
# CLI invocation and run it. Harness-agnostic: needs only bash + coreutils, so it
# behaves identically whether the orchestrator is codex, claude, openclaude or
# opencode.
#
# Usage:
#   forge-dispatch.sh doctor          (harnesses: codex claude openclaude opencode antigravity)
#   forge-dispatch.sh dwarf <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
#   forge-dispatch.sh qa    <spec> --prompt-file <f> [--repo <dir>] [--run-dir <d>] [--yolo] [--dry-run]
#                                  [--native-review [--review-base <ref>]]
#
# Exit codes: 0 ok | 2 usage/resolution error | 3 harness missing or unusable
#             4 backend ran but failed
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="$SKILL_DIR/registry.tsv"

die()  { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }
note() { printf 'forge: %s\n' "$*" >&2; }

# --- effort ladders -----------------------------------------------------------
# codex does NOT validate model_reasoning_effort: a typo runs at some silent
# default instead of erroring. So forge validates before dispatch — a run at the
# wrong effort is expensive and invisible, which is exactly the failure worth
# spending ten lines to prevent.
ladder_for() {
  case "$1" in
    codex)       echo "low medium high xhigh max ultra" ;;
    claude)      echo "low medium high xhigh max" ;;
    openclaude)  echo "low medium high xhigh max ultracode" ;;
    antigravity) echo "low medium high" ;;
    opencode)    echo "" ;;   # --variant is provider-specific; forwarded verbatim
    *)           echo "" ;;
  esac
}

# The harness name is not always the binary name.
binary_for() {
  case "$1" in antigravity) echo agy ;; *) echo "$1" ;; esac
}

# Every effort word forge recognizes, weakest first. Used only to tell "you asked
# for more thinking than this pairing offers" (clamp, keep going) apart from "that
# is not an effort level" (stop). ultra and ultracode are each their harness's top
# tier under different names, so they rank together.
CANON="low medium high xhigh max ultra ultracode"
rank_of() {
  case "$1" in
    low) echo 0 ;; medium) echo 1 ;; high) echo 2 ;; xhigh) echo 3 ;;
    max) echo 4 ;; ultra|ultracode) echo 5 ;; *) echo -1 ;;
  esac
}

idx_in() { # idx_in <needle> <space-separated haystack> -> index or -1
  local n="$1" i=0 w
  for w in $2; do [ "$w" = "$n" ] && { echo "$i"; return; }; i=$((i+1)); done
  echo -1
}

# --- registry lookup ----------------------------------------------------------
lookup() { # lookup <alias> [harness] -> "harness<TAB>model<TAB>ceiling"
  local alias="$1" want="${2:-}"
  awk -F'\t' -v a="$alias" -v h="$want" '
    /^#/ || NF < 3 { next }
    $1 == a && (h == "" || $2 == h) { print $2 "\t" $3 "\t" ($4 == "" ? "-" : $4); exit }
  ' "$REGISTRY"
}

known_harness() {
  case "$1" in codex|claude|openclaude|opencode|antigravity) return 0 ;; *) return 1 ;; esac
}

# --- doctor -------------------------------------------------------------------
doctor() {
  local rc=0 h bin
  printf '%-12s %-10s %s\n' HARNESS STATUS DETAIL
  for h in codex claude openclaude opencode antigravity; do
    bin="$(command -v "$(binary_for "$h")" 2>/dev/null)"
    if [ -z "$bin" ]; then
      printf '%-12s %-10s %s\n' "$h" MISSING "$(binary_for "$h") not on PATH"; rc=1; continue
    fi
    printf '%-12s %-10s %s\n' "$h" "found" "$bin"
  done
  cat <<'EOT'

Binary presence is not reachability. A harness can be installed and still fail on
auth or billing (opencode in particular fails this way), and that failure only
shows up mid-dispatch. To prove a pairing end to end before spending a real run:

  forge-dispatch.sh dwarf <spec> --prompt-file /dev/stdin --repo <scratch dir> \
      <<< 'Reply with exactly: FORGE_OK'
EOT
  return $rc
}

[ $# -ge 1 ] || die "usage: forge-dispatch.sh <doctor|dwarf|qa> [spec] [options]"
ROLE="$1"; shift
[ "$ROLE" = "doctor" ] && { doctor; exit $?; }
case "$ROLE" in dwarf|qa) ;; *) die "unknown role '$ROLE' (expected dwarf, qa or doctor)" ;; esac
[ $# -ge 1 ] || die "role '$ROLE' needs a spec, e.g. sol:xhigh:openclaude"
SPEC="$1"; shift

REPO="$PWD"; RUN_DIR=""; PROMPT_FILE=""; YOLO=0; DRY=0; REVIEW_BASE=""; PROMPT_VIA_STDIN=0; NATIVE_REVIEW=0; AGY_TIMEOUT="30m"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)         REPO="${2:?--repo needs a value}"; shift 2 ;;
    --run-dir)      RUN_DIR="${2:?--run-dir needs a value}"; shift 2 ;;
    --prompt-file)  PROMPT_FILE="${2:?--prompt-file needs a value}"; shift 2 ;;
    --review-base)  REVIEW_BASE="${2:?--review-base needs a value}"; shift 2 ;;
    --native-review) NATIVE_REVIEW=1; shift ;;
    --agy-timeout)  AGY_TIMEOUT="${2:?--agy-timeout needs a value}"; shift 2 ;;
    --yolo)         YOLO=1; shift ;;
    --dry-run)      DRY=1; shift ;;
    *)              die "unknown option '$1'" ;;
  esac
done

# --- resolve spec -------------------------------------------------------------
ALIAS="${SPEC%%:*}"; REST="${SPEC#*:}"
if [ "$REST" = "$SPEC" ]; then EFFORT=""; HARNESS=""
else
  EFFORT="${REST%%:*}"; H="${REST#*:}"
  if [ "$H" = "$REST" ]; then HARNESS=""; else HARNESS="$H"; fi
fi
[ -n "$ALIAS" ] || die "empty alias in spec '$SPEC'"
[ -z "$HARNESS" ] || known_harness "$HARNESS" \
  || die "unknown harness '$HARNESS' (expected codex, claude, openclaude, opencode or antigravity)"

ROW="$(lookup "$ALIAS" "$HARNESS")"
if [ -n "$ROW" ]; then
  IFS=$'\t' read -r HARNESS MODEL CEILING <<< "$ROW"
  PASSTHRU=0
else
  # Not in the registry. Treat the alias as a literal model id — but only with an
  # explicit harness, because there is no sane default for a model forge has
  # never seen, and guessing wrong burns a real dispatch.
  [ -n "$HARNESS" ] || die "'$ALIAS' is not a known alias. Either add it to $REGISTRY or name the harness explicitly, e.g. --dwarf $ALIAS:high:opencode"
  if [ -n "$(awk -F'\t' -v a="$ALIAS" '!/^#/ && $1 == a {print; exit}' "$REGISTRY")" ]; then
    note "alias '$ALIAS' has no row for harness '$HARNESS' — using it as a literal model id"
  else
    note "'$ALIAS' is not in the registry — using it as a literal model id on $HARNESS"
  fi
  MODEL="$ALIAS"; CEILING="-"; PASSTHRU=1
fi

# --- resolve effort -----------------------------------------------------------
LADDER="$(ladder_for "$HARNESS")"
CLAMPED=""; NO_EFFORT=0

# Expand the registry's effort column into the concrete set this pairing accepts.
# The four forms exist because real backends differ in more than a ceiling: some
# models offer low and high but no medium, and some take no effort flag at all.
ALLOWED=""
case "$CEILING" in
  "-")    ALLOWED="" ;;                       # forwarded verbatim, no validation
  none)   NO_EFFORT=1 ;;
  "<="*)  top="${CEILING#<=}"
          for w in $LADDER; do ALLOWED="$ALLOWED $w"; [ "$w" = "$top" ] && break; done ;;
  *)      ALLOWED="$(printf '%s' "$CEILING" | tr ',' ' ')" ;;
esac

if [ "$NO_EFFORT" = 1 ]; then
  # Not a clamp to report: this model has no effort dial, so an effort the user
  # typed is simply inapplicable. Say so once rather than failing the dispatch.
  [ -n "$EFFORT" ] && note "$MODEL on $HARNESS takes no effort setting — ignoring ':$EFFORT'"
  EFFORT=""
else
  if [ -z "$EFFORT" ]; then
    # Build at medium, review high: a reviewer that thinks less than the
    # implementer tends to rubber-stamp, which defeats the point of a second model.
    case "$ROLE" in dwarf) EFFORT="medium" ;; qa) EFFORT="xhigh" ;; esac
    DEFAULTED=1
    [ -n "$LADDER" ] || EFFORT=""   # opencode: no ladder, let the provider choose
  else
    DEFAULTED=0
  fi

  if [ -n "$EFFORT" ] && [ -n "$ALLOWED" ]; then
    if [ "$(idx_in "$EFFORT" "$ALLOWED")" -lt 0 ]; then
      want="$(rank_of "$EFFORT")"
      [ "$want" -ge 0 ] || die "'$EFFORT' is not a recognized effort (known: $CANON)"
      # Step down to the strongest accepted effort at or below what was asked;
      # if even the weakest is stronger, take that. Asking for more thinking than
      # a model offers should cost you thinking, not the whole run.
      pick=""
      for w in $ALLOWED; do
        r="$(rank_of "$w")"
        [ "$r" -le "$want" ] && pick="$w"
      done
      if [ -z "$pick" ]; then for w in $ALLOWED; do pick="$w"; break; done; fi
      [ "$DEFAULTED" = 1 ] || CLAMPED="$EFFORT -> $pick"
      EFFORT="$pick"
    fi
  fi
fi

# --- run dir & prompt ---------------------------------------------------------
[ -d "$REPO" ] || die "--repo '$REPO' is not a directory"
REPO="$(cd "$REPO" && pwd)"
if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/forge-XXXXXX")" || die "could not create run dir"
fi
mkdir -p "$RUN_DIR" || die "could not create run dir '$RUN_DIR'"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"   # normalize, so the containment check below is real
# The run dir deliberately lives outside the repo: anything forge writes inside
# the working tree would show up in the dwarf's own diff and pollute QA's input.
case "$RUN_DIR" in "$REPO"|"$REPO"/*) die "--run-dir must be outside --repo, or it lands in the diff under review" ;; esac

[ -n "$PROMPT_FILE" ] || die "role '$ROLE' needs --prompt-file"
if [ "$PROMPT_FILE" = "-" ] || [ "$PROMPT_FILE" = "/dev/stdin" ]; then
  PROMPT_FILE="$RUN_DIR/$ROLE.prompt"; cat > "$PROMPT_FILE"
fi
[ -r "$PROMPT_FILE" ] || die "cannot read --prompt-file '$PROMPT_FILE'"
PROMPT="$(cat "$PROMPT_FILE")"
[ -n "${PROMPT//[[:space:]]/}" ] || die "prompt file '$PROMPT_FILE' is empty"
# Materialize the prompt into the run dir and read it from there from now on.
# A caller passing a process substitution or any other FIFO can only be read
# once, so the second read (stdin delivery) would otherwise hand the harness an
# empty prompt. Keeping the copy also records exactly what each role was asked.
if [ "$PROMPT_FILE" != "$RUN_DIR/$ROLE.prompt" ]; then
  printf '%s\n' "$PROMPT" > "$RUN_DIR/$ROLE.prompt"
  PROMPT_FILE="$RUN_DIR/$ROLE.prompt"
fi

LAST="$RUN_DIR/$ROLE.last"
LOG="$RUN_DIR/$ROLE.log"

# Some harnesses cannot do things the standard role prompt asks for. Telling the
# model up front beats letting it discover the wall mid-run: on antigravity a
# denied command does not degrade gracefully, it aborts the whole dispatch, so a
# dwarf that dutifully tries to run the test suite ends with an error and an
# empty diff after burning the full quota.
CONSTRAINT=""
if [ "$HARNESS" = "antigravity" ] && [ "$YOLO" != 1 ]; then
  CONSTRAINT="This environment cannot run terminal commands: permission is denied for every
shell command, and a single attempt aborts this run entirely. Do not try to run tests,
linters, git, or any other command. Verify your work by reading the code instead, and say
plainly in your final message that it is unverified by execution."
fi
if [ -n "$CONSTRAINT" ]; then
  printf '%s\n\n%s\n' "$PROMPT" "$CONSTRAINT" > "$RUN_DIR/$ROLE.prompt"
  PROMPT="$(cat "$RUN_DIR/$ROLE.prompt")"
  PROMPT_FILE="$RUN_DIR/$ROLE.prompt"
  note "antigravity without --yolo cannot run commands; told the $ROLE not to try"
fi

# --- build argv ---------------------------------------------------------------
CMD=()
case "$HARNESS" in
  codex)
    if [ "$ROLE" = "qa" ] && [ "$NATIVE_REVIEW" = 1 ]; then
      # codex's built-in reviewer. Its scope flags are mutually exclusive with a
      # custom PROMPT, so this mode trades away the goal context: it can judge
      # whether the diff is correct, but not whether it is the change that was
      # actually asked for. `codex exec review` also has no --cd, hence the
      # subshell cd below.
      CMD=(codex exec review -m "$MODEL" --skip-git-repo-check -o "$LAST")
      [ -n "$EFFORT" ] && CMD+=(-c "model_reasoning_effort=$EFFORT")
      if [ -n "$REVIEW_BASE" ]; then CMD+=(--base "$REVIEW_BASE"); else CMD+=(--uncommitted); fi
      if [ "$YOLO" = 1 ]; then CMD+=(--dangerously-bypass-approvals-and-sandbox); fi
    else
      CMD=(codex exec -m "$MODEL" -C "$REPO" --skip-git-repo-check -o "$LAST")
      [ -n "$EFFORT" ] && CMD+=(-c "model_reasoning_effort=$EFFORT")
      # --approve-for-me and --dangerously-bypass-... are mutually exclusive.
      if [ "$YOLO" = 1 ]; then CMD+=(--dangerously-bypass-approvals-and-sandbox)
      elif [ "$ROLE" = "dwarf" ]; then CMD+=(--approve-for-me)
      else CMD+=(-s read-only); fi
      CMD+=("$PROMPT")
    fi
    ;;
  claude|openclaude)
    # The run dir sits outside the repo (so it stays out of the diff), which also
    # puts it outside the harness's default reach — without this the agent
    # refuses to open the very artifacts forge just wrote for it.
    CMD=("$HARNESS" -p --model "$MODEL" --add-dir "$RUN_DIR")
    [ -n "$EFFORT" ] && CMD+=(--effort "$EFFORT")
    if [ "$YOLO" = 1 ]; then
      CMD+=(--dangerously-skip-permissions)
    else
      # `auto` rather than `acceptEdits`: acceptEdits lets the agent write files
      # but still blocks shell commands, so a dwarf under it cannot run the tests
      # that would tell it whether its own change works, and a reviewer under it
      # cannot reproduce the bug it suspects. Verified against this machine's
      # CLIs — acceptEdits and dontAsk both refuse Bash, auto allows it.
      CMD+=(--permission-mode auto)
      # QA must not be able to "fix" what it reviews — a reviewer that edits the
      # diff is no longer an independent check of it. It keeps read and shell
      # access so it can actually verify a finding before reporting it.
      [ "$ROLE" = "qa" ] && CMD+=(--disallowed-tools "Edit,Write,NotebookEdit")
    fi
    # The prompt goes on stdin, not argv. --disallowed-tools and friends are
    # variadic, so any positional after them is silently eaten as another tool
    # name — the run then dies on "Input must be provided" if you are lucky, or
    # reviews nothing at all if you are not. stdin also lifts the argv length
    # limit, which a long goal plus an inlined diff will otherwise hit.
    PROMPT_VIA_STDIN=1
    ;;
  opencode)
    CMD=(opencode run -m "$MODEL" --dir "$REPO")
    [ -n "$EFFORT" ] && CMD+=(--variant "$EFFORT")
    # opencode run exposes no sandbox tier — without --auto it blocks on a
    # permission prompt that nothing can answer headlessly, so it is always set.
    CMD+=(--auto)
    [ "$ROLE" = "qa" ] && [ "$YOLO" != 1 ] && CMD+=(--agent plan)
    CMD+=("$PROMPT")
    ;;
  antigravity)
    # `agy --print` is a Go flag with an OPTIONAL value: written detached it eats
    # whatever token follows as its prompt ("--print took --print-timeout as its
    # prompt"). The attached --print=... form is the only safe spelling, and it
    # goes last so nothing can be captured after it.
    CMD=(agy --model "$MODEL" --print-timeout "$AGY_TIMEOUT")
    [ -n "$EFFORT" ] && CMD+=(--effort "$EFFORT")
    # --add-dir on the repo is not optional. Antigravity is workspace-scoped
    # rather than cwd-scoped, so without it the agent searches the wrong tree and
    # the run dies on "Find command timed out: context deadline exceeded".
    CMD+=(--add-dir "$REPO" --add-dir "$RUN_DIR")
    if [ "$YOLO" = 1 ]; then CMD+=(--dangerously-skip-permissions)
    elif [ "$ROLE" = "qa" ]; then CMD+=(--mode plan)
    else CMD+=(--mode accept-edits); fi
    CMD+=(--print="$PROMPT")
    ;;
  *) die "no dispatch recipe for harness '$HARNESS'" ;;
esac

# --- report the resolution ----------------------------------------------------
{
  echo "role=$ROLE"
  echo "alias=$ALIAS"
  echo "harness=$HARNESS"
  echo "model=$MODEL"
  echo "effort=${EFFORT:-<harness default>}"
  echo "yolo=$([ "$YOLO" = 1 ] && echo ON || echo off)"
  echo "passthrough=$([ "$PASSTHRU" = 1 ] && echo yes || echo no)"
  [ -n "$CLAMPED" ] && echo "clamped=$CLAMPED"
  echo "repo=$REPO"
  echo "run_dir=$RUN_DIR"
} | tee "$RUN_DIR/$ROLE.resolved"

printf '%q ' "${CMD[@]}" > "$RUN_DIR/$ROLE.cmd"; echo >> "$RUN_DIR/$ROLE.cmd"
if [ "$DRY" = 1 ]; then
  echo "--- dry run, command not executed ---"
  cat "$RUN_DIR/$ROLE.cmd"
  [ "$PROMPT_VIA_STDIN" = 1 ] && echo "  (prompt delivered on stdin from $PROMPT_FILE)"
  exit 0
fi

if [ "$YOLO" = 1 ]; then
  note "YOLO: $HARNESS runs with sandbox and approvals disabled in $REPO"
fi
[ -n "$(command -v "$(binary_for "$HARNESS")")" ] || die "harness '$HARNESS' is not installed ($(binary_for "$HARNESS") not on PATH)" 3

# Harnesses that take the prompt on argv get stdin redirected from /dev/null:
# codex exec otherwise blocks waiting to append piped input to the prompt, which
# headless reads as a silent hang rather than an error.
if [ "$PROMPT_VIA_STDIN" = 1 ]; then
  ( cd "$REPO" && "${CMD[@]}" < "$PROMPT_FILE" ) 2>&1 | tee "$LOG"
else
  ( cd "$REPO" && "${CMD[@]}" </dev/null ) 2>&1 | tee "$LOG"
fi
rc=${PIPESTATUS[0]}
[ -s "$LAST" ] || { [ -s "$LOG" ] && cp "$LOG" "$LAST"; }
if [ "$rc" -ne 0 ]; then
  note "$HARNESS exited $rc — see $LOG"
  exit 4
fi
echo "forge: $ROLE done -> $LAST" >&2
