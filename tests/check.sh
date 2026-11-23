#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/forge-lib-env.sh
forge_ensure_python3
bash -n forge
for script in scripts/*.sh tests/*.sh; do bash -n "$script"; done
# Both suites, every test class in a process of its own, in parallel (FORGE_TEST_JOBS=1 runs
# them one after another); see tests/run_tests.py. Arguments go to it, e.g. --shard 1/4 in CI.
python3 tests/run_tests.py "$@"
if command -v node >/dev/null 2>&1; then
  node --check bin/forge.js
  for f in ui/forge_ui/static/*.js; do node --check "$f"; done
fi
