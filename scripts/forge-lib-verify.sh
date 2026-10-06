#!/usr/bin/env bash
# Verification helpers for forge-parallel.sh. Sourced; functions only.

# The verify command a plan would run: --verify (persisted in the plan) first, then the
# repository's own .forge/verify. Prints nothing when neither is configured.
verify_command_for() { # verify_command_for <plan> <repo>
  if [ -s "$1/verify_cmd" ]; then cat "$1/verify_cmd"
  elif [ -s "$2/.forge/verify" ]; then cat "$2/.forge/verify"
  fi
  return 0
}

# "Verify is whatever you hand it": a gate that skips a suite the project defines lets a
# regression in that suite reach the branch with no warning. Read the project's own test
# configuration and say which suites the command does not run. Advisory by design — it
# prints and records, it never changes an outcome, and a failure of the checker is silent.
verify_coverage_report() { # verify_coverage_report <checkout> <command> <out-file>
  local dir="$1" cmd="$2" out="$3" line
  [ -f "$SKILL_DIR/scripts/forge-verify-coverage.py" ] || return 0
  rm -f "$out"
  if [ -n "$cmd" ]; then
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --command="$cmd" --out "$out" >/dev/null 2>&1
  else
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --none --out "$out" >/dev/null 2>&1
  fi
  if [ -s "$out" ]; then
    while IFS= read -r line; do note "$line"; done < "$out"
  fi
  return 0
}
