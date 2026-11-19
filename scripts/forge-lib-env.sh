#!/usr/bin/env bash
# Shared environment helpers for cross-platform shell entrypoints.
#
# On Windows (Git Bash / MSYS2), `python3` on PATH is often the Microsoft Store
# execution alias in WindowsApps while a real Python 3.9+ lives at `python` or
# `py -3`. Prepending a small per-user wrapper directory to PATH fixes every
# downstream `python3` call (in bash scripts and Windows subprocesses) with zero
# overhead or external commands on macOS/Linux.

forge_is_windows() {
  case "${OSTYPE:-}" in
    msys*|cygwin*) return 0 ;;
    *) return 1 ;;
  esac
}

forge_posix_path() {
  local p="${1:-}"
  p="${p%$'\r'}"
  if [ -n "$p" ] && forge_is_windows && command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$p" 2>/dev/null || printf '%s\n' "$p"
  else
    printf '%s\n' "$p"
  fi
}

forge_win_path() {
  local p="${1:-}"
  p="${p%$'\r'}"
  if [ -n "$p" ] && forge_is_windows && command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$p" 2>/dev/null || printf '%s\n' "$p"
  else
    printf '%s\n' "$p"
  fi
}

forge_host_path() {
  local p="${1:-}"
  p="${p%$'\r'}"
  if [ -n "$p" ] && forge_is_windows && command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$p" 2>/dev/null || printf '%s\n' "$p"
  else
    printf '%s\n' "$p"
  fi
}

forge_ensure_python3() {
  forge_is_windows || return 0

  local current exe win_exe cand base shim_dir shim cmd_shim expected cmd_expected
  current="$(command -v python3 2>/dev/null || true)"
  case "$current" in
    '') ;;
    *[Ww]indows[Aa]pps*) ;;
    */forge/bin/python3) ;;
    *) return 0 ;;
  esac

  exe=""
  for cand in python python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    case "$(command -v "$cand" 2>/dev/null || true)" in
      *[Ww]indows[Aa]pps*) continue ;;
    esac
    exe="$("$cand" -c 'import sys; print(sys.executable if sys.version_info >= (3, 9) else "")' 2>/dev/null || true)"
    [ -n "$exe" ] && break
  done
  if [ -z "$exe" ] && command -v py >/dev/null 2>&1; then
    exe="$(py -3 -c 'import sys; print(sys.executable if sys.version_info >= (3, 9) else "")' 2>/dev/null || true)"
  fi
  [ -n "$exe" ] || return 0

  exe="${exe%$'\r'}"
  win_exe="$exe"
  if command -v cygpath >/dev/null 2>&1; then
    win_exe="$(cygpath -w "$exe" 2>/dev/null || printf '%s' "$exe")"
    exe="$(cygpath -u "$exe" 2>/dev/null || printf '%s' "$exe")"
  fi

  base="${LOCALAPPDATA:-${XDG_STATE_HOME:-${HOME:-}/.local/state}}"
  [ -n "$base" ] || return 0
  base="$(forge_posix_path "$base")"
  shim_dir="$base/forge/bin"
  ( umask 077 && mkdir -p "$shim_dir" ) 2>/dev/null || return 0

  shim="$shim_dir/python3"
  printf -v expected '#!/usr/bin/env bash\nexec %q "$@"\n' "$exe"
  if [ ! -x "$shim" ] || [ "$(cat "$shim" 2>/dev/null)" != "${expected%$'\n'}" ]; then
    printf '%s' "$expected" > "$shim" 2>/dev/null || return 0
    chmod +x "$shim" 2>/dev/null || true
  fi

  cmd_shim="$shim_dir/python3.cmd"
  printf -v cmd_expected '@echo off\r\n"%s" %%*\r\n' "$win_exe"
  if [ ! -f "$cmd_shim" ] || [ "$(cat "$cmd_shim" 2>/dev/null)" != "${cmd_expected%$'\n'}" ]; then
    printf '%s' "$cmd_expected" > "$cmd_shim" 2>/dev/null || true
  fi

  case ":$PATH:" in
    *":$shim_dir:"*) ;;
    *) export PATH="$shim_dir:$PATH" ;;
  esac
}

_FORGE_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

forge_winpid() {
  local pid="${1:-}" win=""
  [ -n "$pid" ] || return 0
  if [ -r "/proc/$pid/winpid" ]; then
    read -r win < "/proc/$pid/winpid" 2>/dev/null || win=""
    win="${win%$'\r'}"
  fi
  printf '%s\n' "${win:-$pid}"
}

forge_kill_children() {
  local pid="${1:-}" sig="${2:-TERM}" cpid winpid
  [ -n "$pid" ] || return 0
  if command -v pkill >/dev/null 2>&1; then
    pkill "-$sig" -P "$pid" 2>/dev/null || true
    return 0
  fi
  # Git Bash / MSYS2 has no pkill: signal MSYS children via ps -ef and native children via forge-guard.py.
  if [ -r "/proc/$pid/winpid" ]; then
    while read -r cpid; do
      [ -n "$cpid" ] && kill "-$sig" "$cpid" 2>/dev/null || true
    done < <(ps -ef 2>/dev/null | awk -v p="$pid" '$3 == p { print $2 }')
    winpid="$(forge_winpid "$pid")"
    if [ -r "$_FORGE_ENV_DIR/forge-guard.py" ] && [ -n "$winpid" ]; then
      python3 "$_FORGE_ENV_DIR/forge-guard.py" kill-children "$winpid" "$sig" 2>/dev/null || true
    fi
  fi
}

forge_kill_group() {
  local pid="${1:-}" sig="${2:-TERM}"
  [ -n "$pid" ] || return 0
  kill -s "$sig" -- "-$pid" 2>/dev/null || true
  if [ -r "/proc/$pid/winpid" ]; then
    forge_kill_children "$pid" "$sig"
  fi
}

# Create a live directory link (<src> -> <dest>). Uses NTFS directory junctions on Windows
# (no admin/Developer Mode required; unlike MSYS `ln -sfn`, edits remain live and `-L` works).
forge_link_dir() {
  local src="${1:-}" dest="${2:-}"
  if forge_is_windows; then
    if [ -L "$dest" ]; then
      rmdir "$dest" 2>/dev/null || rm -f "$dest" 2>/dev/null || true
    fi
    MSYS_NO_PATHCONV=1 cmd /c mklink /J "$(forge_win_path "$dest")" "$(forge_win_path "$src")" >/dev/null 2>&1
    return $?
  fi
  ln -sfn "$src" "$dest"
}
