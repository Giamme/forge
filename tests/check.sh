#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for script in scripts/*.sh tests/*.sh; do bash -n "$script"; done
python3 -m unittest discover -s tests -v
