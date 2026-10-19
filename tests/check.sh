#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for script in scripts/*.sh tests/*.sh; do bash -n "$script"; done
python3 -m unittest discover -s tests -v
(cd ui && python3 -m unittest discover -s tests -v)
if command -v node >/dev/null 2>&1; then for f in ui/forge_ui/static/*.js; do node --check "$f"; done; fi
