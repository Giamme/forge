#!/usr/bin/env bash
# Update the canonical checkout, then refresh each harness through the installer.
set -uo pipefail

main() {
  local source_dir dry=0 local_only=0 upstream remote branch before after changes
  source_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || return 2
  while [ $# -gt 0 ]; do
    case "$1" in
      --source) source_dir="${2:?--source needs a Forge checkout}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      --local) local_only=1; shift ;;
      --help|-h)
        echo 'Usage: forge-update.sh [--source <checkout>] [--local] [--dry-run]'
        echo 'Default: fast-forward the tracked upstream, then refresh harness installations.'
        echo '--local: refresh installations from the current files without fetching.'
        return 0 ;;
      *) echo "forge: unknown update option: $1" >&2; return 2 ;;
    esac
  done
  source_dir="$(cd -P "$source_dir" && pwd)" || return 2
  if [ ! -f "$source_dir/SKILL.md" ] || [ ! -f "$source_dir/scripts/forge-install.sh" ]; then
    echo "forge: not a Forge source directory: $source_dir" >&2; return 2
  fi
  echo "forge: update source: $source_dir"
  if [ "$local_only" = 0 ]; then
    if [ "$(git -C "$source_dir" rev-parse --show-toplevel 2>/dev/null)" != "$source_dir" ]; then
      echo 'forge: update needs the original Git checkout; use --source <checkout> or --local' >&2; return 2
    fi
    changes="$(git -C "$source_dir" status --porcelain --untracked-files=all)" || return $?
    if [ -n "$changes" ]; then
      echo 'forge: checkout has local changes; commit or stash them, or use --local to refresh current files' >&2; return 2
    fi
    branch="$(git -C "$source_dir" symbolic-ref --quiet --short HEAD)" || {
      echo 'forge: detached HEAD; check out a tracking branch or use --local' >&2; return 2;
    }
    upstream="$(git -C "$source_dir" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || {
      echo 'forge: branch has no upstream; configure tracking or use --local' >&2; return 2;
    }
    remote="$(git -C "$source_dir" config --get "branch.$branch.remote")" || return 2
    if [ "$dry" = 1 ]; then
      echo "forge: would fetch $remote and fast-forward $branch to $upstream (no network in dry run)"
    else
      before="$(git -C "$source_dir" rev-parse HEAD)" || return 2
      git -C "$source_dir" fetch -- "$remote" || return $?
      git -C "$source_dir" merge --ff-only -- "$upstream" || {
        echo 'forge: could not fast-forward; resolve branch divergence before updating installations' >&2; return 1;
      }
      after="$(git -C "$source_dir" rev-parse HEAD)" || return 2
      echo "forge: source $before -> $after"
    fi
  fi
  # Updating Forge does not offer installation of optional dependencies.
  if [ "$dry" = 1 ]; then
    FORGE_RIPWIRE=off /bin/bash "$source_dir/scripts/forge-install.sh" --dry-run || return $?
    echo 'forge: preview only; remote changes and live harness availability are not verified'
  else
    FORGE_RIPWIRE=off /bin/bash "$source_dir/scripts/forge-install.sh" || {
      echo 'forge: source retained; some installations failed. Fix the reported cause and retry with --local' >&2; return 1;
    }
    echo 'forge: installations refreshed; restart active harness sessions to load the updated skill'
  fi
}

# Parse the function before fetching: the update may replace this script on disk.
main "$@"; exit $?
