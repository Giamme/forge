#!/usr/bin/env bash
# forge-win.sh — sourced first by forge's entry scripts. Does nothing except on Windows in
# MSYS2 mode.
#
# On Windows forge runs natively by default: Git Bash with Windows' own Python. MSYS2 mode is
# the alternative for POSIX behaviour as on Linux: forge runs inside MSYS2, whose bash, git,
# python3 and ps agree on process ids, process groups, signals, flock and symlinks. It is opt-in:
#
#   FORGE_WIN_MODE=msys2   an entry script started from Git Bash re-runs itself inside MSYS2
#   an MSYS2 shell         forge started there runs there, whatever FORGE_WIN_MODE says
#
#   pacman -S python git procps-ng diffutils     in the MSYS2 at FORGE_MSYS2_ROOT (default C:/msys64)
#
# Symlinks are made as real Windows symlinks (Developer Mode must be on). A link to a target
# that does not exist yet falls back to an MSYS-only link, which native programs see as a file.
#
# FORGE_WIN_AGENT_PATH is the PATH Git Bash had. forge-dispatch.sh gives it back to the agent
# CLIs, so they see what they would outside forge rather than MSYS2's tools first.
#
# This file is sourced on macOS too, so it keeps to Bash 3.2: no mapfile, no ${var,,}.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  # Run, not sourced: the landing side of the hop below. The original command line arrives in
  # FORGE_WIN_ARGV, NUL-separated and base64-encoded, because a Windows command line cannot
  # carry every argument unchanged (a double quote inside one is lost).
  [ -n "${FORGE_WIN_ARGV:-}" ] || { echo "forge-win.sh is sourced by forge's entry scripts; it is not a command" >&2; exit 2; }
  _forge_argv=()
  while IFS= read -r -d '' _forge_arg; do _forge_argv+=("$_forge_arg"); done \
    < <(printf '%s' "$FORGE_WIN_ARGV" | /usr/bin/base64 -d)
  unset FORGE_WIN_ARGV _forge_arg
  exec /usr/bin/bash "${_forge_argv[@]}"
fi
case "${OSTYPE:-}" in msys*|cygwin*) ;; *) return 0 ;; esac
case "${FORGE_WIN_MODE:-native}" in
  native|msys2) ;;
  *) echo "forge: FORGE_WIN_MODE is '$FORGE_WIN_MODE'; use native (the default) or msys2" >&2; exit 2 ;;
esac

# MSYS2 has pacman; Git for Windows has git-bash.exe at its root (its SDK, which has both, is
# Git Bash too). Only file tests here: native mode must not pay for a single extra process.
if [ ! -x /usr/bin/pacman ] || [ -e /git-bash.exe ]; then
  [ "${FORGE_WIN_MODE:-native}" = msys2 ] || return 0      # native mode: Git Bash runs forge itself
  _forge_msys="${FORGE_MSYS2_ROOT:-C:/msys64}"
  _forge_msys="${_forge_msys%/}"
  if [ ! -x "$_forge_msys/usr/bin/bash.exe" ]; then
    echo "forge: FORGE_WIN_MODE=msys2, but MSYS2 was not found at $_forge_msys (set FORGE_MSYS2_ROOT)" >&2
    exit 127
  fi
  # One hop only: landing anywhere but an MSYS2 would otherwise re-run the script for ever.
  if [ -n "${FORGE_WIN_REEXEC:-}" ]; then
    echo "forge: re-ran inside $_forge_msys, which does not look like MSYS2 (no /usr/bin/pacman)" >&2
    exit 127
  fi
  # The hop goes through a native program. Started directly, MSYS2's bash would get almost none of
  # this environment: two copies of the MSYS runtime cannot hand it to each other.
  _forge_native=""
  for _forge_tool in python3 python py; do
    if "$_forge_tool" -c 'import sys; sys.exit(sys.platform != "win32")' 2>/dev/null; then _forge_native="$_forge_tool"; break; fi
  done
  if [ -z "$_forge_native" ]; then
    echo "forge: MSYS2 mode needs a native Windows Python on the Git Bash PATH to hand over to MSYS2; or start forge from an MSYS2 shell" >&2
    exit 127
  fi
  FORGE_WIN_ARGV="$(printf '%s\0' "$(/usr/bin/cygpath -m "$0")" "$@" | /usr/bin/base64 -w0)"
  export FORGE_WIN_ARGV FORGE_WIN_REEXEC=1 FORGE_WIN_MSYSTEM="${MSYSTEM-}"
  # Ctrl-C reaches MSYS2's bash as well, and forge's own handlers clean up (worktrees, agents).
  # The hand-over only waits for it: subprocess.call would kill it on KeyboardInterrupt.
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' exec "$_forge_native" -c '
import subprocess, sys
child = subprocess.Popen(sys.argv[1:])
while True:
    try:
        sys.exit(child.wait())
    except KeyboardInterrupt:
        pass
' "$_forge_msys/usr/bin/bash.exe" "$(/usr/bin/cygpath -m "${BASH_SOURCE[0]}")"
fi

# Inside MSYS2: started from an MSYS2 shell, or just arrived from Git Bash.
_forge_msys="$(/usr/bin/cygpath -m /)"
_forge_msys="${_forge_msys%/}"
for _forge_tool in python3 git procps cmp diff; do
  if [ ! -x "/usr/bin/$_forge_tool" ]; then
    echo "forge: the MSYS2 at $_forge_msys has no $_forge_tool; run there: pacman -S python git procps-ng diffutils" >&2
    exit 127
  fi
done
if [ -n "${FORGE_WIN_REEXEC:-}" ]; then
  # Just arrived from Git Bash. Its PATH is what an agent CLI should see; forge itself needs
  # MSYS2's python3 and git ahead of the Windows ones.
  unset FORGE_WIN_REEXEC MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
  export FORGE_WIN_AGENT_PATH="$PATH" PATH="/usr/bin:$PATH"
fi
export MSYSTEM=MSYS MSYS=winsymlinks:native
# Worktree links must be relative: Git for Windows, which the native agent CLIs use, cannot follow
# the /c/... paths MSYS2's git writes. The setting travels in the environment, but the first
# worktree made with it adds extensions.relativeWorktrees to the repository's config, and from
# then on Git older than 2.48 (and libgit2- or JGit-based tools) refuses to open the repository.
# So both gits must be 2.48 or newer; children of this script inherit the result.
if [ -z "${FORGE_WIN_GIT_CHECKED:-}" ]; then
  _forge_git_too_old() { # _forge_git_too_old <git> <how to update>: says why and succeeds when < 2.48
    local version major minor
    version="$("$1" --version 2>/dev/null)"
    version="${version#git version }"
    major="${version%%.*}"
    minor="${version#*.}"
    minor="${minor%%.*}"
    case "$major.$minor" in
      [0-9]*.[0-9]*) ;;
      *) echo "forge: cannot read the version of $1 ('$version'); MSYS2 mode needs Git 2.48 or newer" >&2; return 0 ;;
    esac
    if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 48 ]; }; then return 1; fi
    echo "forge: MSYS2 mode needs Git 2.48 or newer for relative worktree links; $1 is $version: $2" >&2
    return 0
  }
  if _forge_git_too_old /usr/bin/git 'update MSYS2 with pacman -Syu'; then exit 127; fi
  _forge_agent_git="$(PATH="${FORGE_WIN_AGENT_PATH:-$PATH}" command -v git)"
  if [ -n "$_forge_agent_git" ] && [ "$_forge_agent_git" != /usr/bin/git ] \
     && _forge_git_too_old "$_forge_agent_git" 'update Git for Windows'; then exit 127; fi
  unset -f _forge_git_too_old
  export FORGE_WIN_GIT_CHECKED=1
fi
case " ${GIT_CONFIG_KEY_0-} ${GIT_CONFIG_KEY_1-} ${GIT_CONFIG_KEY_2-} ${GIT_CONFIG_KEY_3-} " in
  *" worktree.useRelativePaths "*) ;;
  *) _forge_n="${GIT_CONFIG_COUNT:-0}"
     export "GIT_CONFIG_KEY_$_forge_n=worktree.useRelativePaths" "GIT_CONFIG_VALUE_$_forge_n=true"
     export GIT_CONFIG_COUNT="$((_forge_n + 1))" ;;
esac
# procps' pkill reads every /proc/<pid>/stat, which Cygwin fills by asking Windows: about half a
# second per process. forge only runs `pkill [-SIGNAL] -P <parent>`, which /proc/<pid>/ppid answers
# at once. Anything else goes to the real pkill. Scripts sourced after this file (forge-metrics.sh)
# get the function too.
pkill() {
  local sig=TERM parent="" arg dir ppid found=1
  for arg in "$@"; do
    case "$arg" in -P) ;; -[A-Z]*|-[0-9]*) sig="${arg#-}" ;; [0-9]*) parent="$arg" ;; *) command pkill "$@"; return ;; esac
  done
  [ -n "$parent" ] || { command pkill "$@"; return; }
  for dir in /proc/[0-9]*; do
    read -r ppid < "$dir/ppid" 2>/dev/null || continue
    [ "$ppid" = "$parent" ] && kill -s "$sig" "${dir#/proc/}" 2>/dev/null && found=0
  done
  return "$found"
}
unset _forge_msys _forge_tool _forge_n _forge_native _forge_agent_git
