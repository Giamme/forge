#!/usr/bin/env bash
# The tests in the given files only, for .forge/verify-subset (a failed verification's first rerun,
# plan --pre-review-tests and --fail-first). Python test files run through run_tests.py, the ui's
# node tests through node --test; any other path is skipped.
set -u
cd "$(dirname "$0")/.."
py=() js=() rc=0
for f in "$@"; do
  case "$f" in
    tests/test_*.py) m="${f#tests/}"; py+=("forge:${m%.py}.") ;;
    ui/tests/test_*.py) m="${f#ui/tests/}"; py+=("ui:${m%.py}.") ;;
    ui/tests/js/*.test.js) js+=("$f") ;;
  esac
done
[ "${#py[@]}" -eq 0 ] || python3 tests/run_tests.py "${py[@]}" || rc=1
[ "${#js[@]}" -eq 0 ] || node --test "${js[@]}" || rc=1
exit "$rc"
