#!/usr/bin/env bash
# Verification helpers for forge-parallel.sh. Sourced; functions only.

# A lanes directory counts once it holds a lane file (dot files and backups are skipped,
# as forge-verify.py skips them).
verify_lanes_present() { # verify_lanes_present <dir>
  local f
  [ -d "$1" ] || return 1
  for f in "$1"/*; do
    [ -e "$f" ] || continue
    case "${f##*/}" in *~) continue ;; esac
    return 0
  done
  return 1
}

# Where a plan's verification comes from, in this order: the plan's lanes (--verify-lane,
# $PLAN/verify.d/), the plan's --verify, the repository's lanes (.forge/verify.d/), the
# repository's .forge/verify. Prints "<kind><TAB><location>" with kind lanes:plan,
# command:plan, lanes:repo or command:repo, or nothing when none is configured.
verify_source_for() { # verify_source_for <plan> <repo>
  if verify_lanes_present "$1/verify.d"; then printf 'lanes:plan\t%s\n' "$1/verify.d"
  elif [ -s "$1/verify_cmd" ]; then printf 'command:plan\t%s\n' "$1/verify_cmd"
  elif verify_lanes_present "$2/.forge/verify.d"; then printf 'lanes:repo\t%s\n' "$2/.forge/verify.d"
  elif [ -s "$2/.forge/verify" ]; then printf 'command:repo\t%s\n' "$2/.forge/verify"
  fi
  return 0
}

# The verify command a plan would run, as text: the command itself, or for lanes every
# lane's command (its #forge: option lines left out), one after another. Prints nothing
# when nothing is configured.
verify_command_for() { # verify_command_for <plan> <repo>
  local src loc f
  src="$(verify_source_for "$1" "$2")"
  loc="${src#*$'\t'}"
  case "$src" in
    command:*) cat "$loc" ;;
    lanes:*)
      for f in "$loc"/*; do
        [ -f "$f" ] || continue
        case "${f##*/}" in *~) continue ;; esac
        awk 'body || !/^#forge:/ { body = 1; print }' "$f"
      done ;;
  esac
  return 0
}

# Lanes are checked before any model is paid for: the runner's own validation, against the
# checkout the lanes will run in (baseline= files exist, reports= and baseline= stay inside).
# Dies with exit 2 and the runner's messages on an invalid source; a single command, or
# nothing, passes.
verify_lanes_check() { # verify_lanes_check <plan> <repo> <checkout>
  local src loc out
  src="$(verify_source_for "$1" "$2")"
  case "$src" in lanes:*) ;; *) return 0 ;; esac
  loc="${src#*$'\t'}"
  if [ "${src%%$'\t'*}" = lanes:repo ] && [ -s "$2/.forge/verify" ]; then
    note ".forge/verify is ignored: the repository declares verification lanes in .forge/verify.d/"
  fi
  out="$(python3 "$SKILL_DIR/scripts/forge-verify.py" lanes --lanes-dir "$loc" --checkout "$3" 2>&1 >/dev/null)" \
    || die "invalid verification lanes in $loc:
$out"
  return 0
}

# "Verify is whatever you hand it": a gate that skips a suite the project defines lets a
# regression in that suite reach the branch with no warning. Read the project's own test
# configuration and say which suites the command does not run. Advisory by design — it
# prints and records, it never changes an outcome, and a failure of the checker is silent.
verify_coverage_report() { # verify_coverage_report <checkout> <command> <out-file>
  local dir="$1" cmd="$2" out="$3" line file
  [ -f "$SKILL_DIR/scripts/forge-verify-coverage.py" ] || return 0
  rm -f "$out"
  if [[ "$cmd" == *$'\n'* ]]; then
    # Several lines (every lane's command, or a multi-line script): through a file.
    file="$(mktemp "${TMPDIR:-/tmp}/forge-verify-cmd.XXXXXX")" || return 0
    printf '%s\n' "$cmd" > "$file"
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --command-file "$file" --out "$out" >/dev/null 2>&1
    rm -f "$file"
  elif [ -n "$cmd" ]; then
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --command="$cmd" --out "$out" >/dev/null 2>&1
  else
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --none --out "$out" >/dev/null 2>&1
  fi
  if [ -s "$out" ]; then
    while IFS= read -r line; do note "$line"; done < "$out"
  fi
  return 0
}
