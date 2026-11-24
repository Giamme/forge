#!/usr/bin/env bash
# Optional Ripwire install or update, from upstream's release installer. No network activity
# before consent.
#
#   forge-install-ripwire.sh [--yes] [--dry-run] [--upgrade] [--version TAG]
#
#   --yes          do not ask (the caller already has the user's consent)
#   --dry-run      say what would happen; never downloads
#   --upgrade      replace ~/.local/bin/ripwire, the binary forge installs. A Ripwire installed
#                  anywhere else is reported and never replaced.
#   --version TAG  install this release (e.g. v0.6.10) instead of the latest one; also
#                  FORGE_RIPWIRE_VERSION.
#
# What is verified: the release tag is resolved through the GitHub releases API (Intel Macs stop
# at v0.6.1, the last release with a binary for them), and upstream's scripts/install.sh is
# fetched AT THAT TAG over HTTPS. That installer refuses a release without a checksum, checks the
# archive's SHA-256, runs the binary's --version before installing it and again after. What is
# not verified: the installer script's own bytes. A moving version cannot be pinned by hash, so
# forge only checks it is upstream's release installer before running it.
set -uo pipefail
YES=0; DRY=0; UPGRADE=0; TAG="${FORGE_RIPWIRE_VERSION:-}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --upgrade) UPGRADE=1 ;;
    --version)
      [ $# -ge 2 ] || { echo 'forge: --version needs a release tag, e.g. v0.6.10' >&2; exit 2; }
      TAG="$2"; shift ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "forge: unknown installation option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$TAG" in
  ''|v[0-9]*.[0-9]*.[0-9]*) ;;
  [0-9]*.[0-9]*.[0-9]*) TAG="v$TAG" ;;
  *) echo "forge: --version must be a release tag such as v0.6.10, not '$TAG'" >&2; exit 2 ;;
esac
[ "${FORGE_RIPWIRE:-}" != off ] || exit 0
DEST="$HOME/.local/bin/ripwire"
FOUND="$(command -v ripwire 2>/dev/null || true)"
same_file() { [ -e "$1" ] && [ -e "$2" ] && [ "$(cd -P "$(dirname "$1")" && pwd)/$(basename "$1")" = "$(cd -P "$(dirname "$2")" && pwd)/$(basename "$2")" ]; }
have=''
if [ "$UPGRADE" = 0 ]; then
  if [ -n "$FOUND" ] || [ -e "$DEST" ] || [ -L "$DEST" ]; then
    echo 'forge: preserving existing Ripwire installation' >&2
    exit 0
  fi
else
  if [ -n "$FOUND" ] && ! same_file "$FOUND" "$DEST"; then
    # Never replace a binary forge did not install, and do not install a second one behind it.
    echo "forge: Ripwire at $FOUND was not installed by forge; update it the way you installed it" >&2
    exit 0
  fi
  if [ -L "$DEST" ]; then
    echo "forge: $DEST is a symlink, so forge did not install it; not replacing it" >&2
    exit 0
  fi
  [ -f "$DEST" ] && have="$("$DEST" --version 2>/dev/null | head -1)"
fi
if [ -n "$TAG" ]; then want="Ripwire $TAG"; else want='the latest Ripwire release'; fi
printf 'forge: %s -> %s\n' "$want" "$DEST" >&2
[ "$DRY" = 0 ] || exit 0
if [ "$YES" = 0 ]; then
  if [ -n "$have" ]; then question="Update Ripwire ($have) to $want? [Y/n] "
  else question='Ripwire is missing. Install it now? [Y/n] '; fi
  # Open the controlling terminal, never task stdin (which may be a pipe/FIFO).
  if { exec 3<> /dev/tty; } 2>/dev/null && [ -t 3 ]; then
    printf '%s' "$question" >&3
    reply=''
    if ! IFS= read -r reply <&3; then exec 3>&-; exit 0; fi
    exec 3>&-
    case "$reply" in ''|y|Y|yes|YES) ;; *) exit 0 ;; esac
  else
    printf 'forge: optional install: bash %q --yes%s\n' "${BASH_SOURCE[0]}" "$([ "$UPGRADE" = 1 ] && printf ' --upgrade')" >&2
    exit 0
  fi
fi
if [ -z "$TAG" ]; then
  TAG="$(python3 "$SCRIPTS/forge-ripwire.py" --latest)" || { echo 'forge: Ripwire installation skipped; Forge can continue' >&2; exit 1; }
fi
if [ -n "$have" ] && [ "$(python3 -c 'import re,sys
v=lambda t: tuple(map(int, re.search(r"(\d+)\.(\d+)\.(\d+)", t).groups()))
print(int(v(sys.argv[1]) >= v(sys.argv[2])))' "$have" "$TAG" 2>/dev/null)" = 1 ]; then
  echo "forge: Ripwire is already at ${have##* } (release $TAG); nothing to update" >&2
  exit 0
fi
work="$(mktemp -d "${TMPDIR:-/tmp}/forge-install-ripwire.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT
curl -fLSs --connect-timeout 10 --max-time 60 \
  "https://raw.githubusercontent.com/redhat-et/ripwire/$TAG/scripts/install.sh" \
  -o "$work/install.sh" || { echo 'forge: Ripwire installer download failed' >&2; exit 1; }
# Not a hash pin (see the header): only that this is upstream's release installer.
if ! head -1 "$work/install.sh" | grep -q '^#!/usr/bin/env bash' \
   || ! grep -q 'RIPWIRE_REPO' "$work/install.sh" || ! grep -q 'sha256' "$work/install.sh"; then
  echo 'forge: the downloaded file is not the Ripwire release installer; nothing was run' >&2
  exit 1
fi
# The upstream installer verifies archive checksums and the binary version, and replaces an
# existing binary by rename. Its skills and hooks are never activated.
RIPWIRE_REPO=redhat-et/ripwire RIPWIRE_VERSION="$TAG" \
RIPWIRE_INSTALL_PREFIX="$HOME/.local" RIPWIRE_INSTALL_YES=1 RIPWIRE_NO_ACTIVATE=1 \
  /bin/bash "$work/install.sh"
rc=$?
if [ "$rc" != 0 ]; then
  echo "forge: Ripwire installation failed (exit $rc); Forge can continue" >&2
  exit "$rc"
fi
python3 - "$DEST" <<'PYCHECK'
import re, subprocess, sys
try:
    out = subprocess.run([sys.argv[1], '--version'], capture_output=True, text=True, timeout=10).stdout
except (OSError, subprocess.SubprocessError):
    out = ''
found = re.search(r'(\d+)\.(\d+)\.(\d+)', out)
if not found or tuple(map(int, found.groups())) < (0, 6, 0):
    print('forge: the installed Ripwire (%s) is older than 0.6, so forge will not use it' % (out.strip() or '?'),
          file=sys.stderr)
PYCHECK
exit 0
