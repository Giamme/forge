#!/usr/bin/env bash
# Optional pinned upstream installer. No network activity before consent.
set -uo pipefail
YES=0; DRY=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "forge: unknown installation option: $arg" >&2; exit 2 ;;
  esac
done
[ "${FORGE_RIPWIRE:-}" != off ] || exit 0
DEST="$HOME/.local/bin/ripwire"
if command -v ripwire >/dev/null 2>&1 || [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo 'forge: preserving existing Ripwire installation' >&2
  exit 0
fi
printf 'forge: Ripwire v0.4.0 -> %s\n' "$DEST" >&2
[ "$DRY" = 0 ] || exit 0
if [ "$YES" = 0 ]; then
  # Open the controlling terminal, never task stdin (which may be a pipe/FIFO).
  if { exec 3<> /dev/tty; } 2>/dev/null && [ -t 3 ]; then
    printf 'Ripwire is missing. Install it now? [Y/n] ' >&3
    reply=''
    if ! IFS= read -r reply <&3; then exec 3>&-; exit 0; fi
    exec 3>&-
    case "$reply" in ''|y|Y|yes|YES) ;; *) exit 0 ;; esac
  else
    printf 'forge: optional install: bash %q --yes\n' "${BASH_SOURCE[0]}" >&2
    exit 0
  fi
fi
work="$(mktemp -d "${TMPDIR:-/tmp}/forge-install-ripwire.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT
curl -fLSs --connect-timeout 10 --max-time 60 \
  https://raw.githubusercontent.com/redhat-et/ripwire/v0.4.0/scripts/install.sh \
  -o "$work/install.sh" || { echo 'forge: Ripwire installer download failed' >&2; exit 1; }
# Pin the installer bytes as well as the release; never execute a changed tag.
python3 - "$work/install.sh" <<'PYHASH'
import hashlib, pathlib, sys
expected = '7290f90775d333798ec9d7f1193c5950516dd0f1fea6ce5faea3923b1f60ff01'
if hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() != expected:
    sys.exit('forge: Ripwire installer checksum mismatch')
PYHASH
[ "$?" = 0 ] || exit 1
# The upstream installer verifies archive checksums and the binary version.
RIPWIRE_REPO=redhat-et/ripwire RIPWIRE_VERSION=v0.4.0 \
RIPWIRE_INSTALL_PREFIX="$HOME/.local" RIPWIRE_INSTALL_YES=1 RIPWIRE_NO_ACTIVATE=1 \
  /bin/bash "$work/install.sh"
rc=$?
[ "$rc" = 0 ] || echo "forge: Ripwire installation failed (exit $rc); Forge can continue" >&2
exit "$rc"
