#!/usr/bin/env bash
# Shared artifact operations. A private index never stages or unstages user work.
forge_tree() (
  cd "$1" || exit 1
  local idx tree actual_index f
  idx="$(mktemp "${TMPDIR:-/tmp}/forge-index.XXXXXX")" || exit 1
  rm -f "$idx"
  trap 'rm -f "$idx"' EXIT
  actual_index="$(git rev-parse --git-path index)" || exit 1
  export GIT_INDEX_FILE="$idx"
  git read-tree HEAD && git add -A -- . ':(exclude).forge' || exit 1
  # Force-added ignored files are tracked in the real index, but not in HEAD yet.
  GIT_INDEX_FILE="$actual_index" git diff --cached --name-only --diff-filter=A -z HEAD |
    while IFS= read -r -d '' f; do
      case "$f" in .forge|.forge/*) continue ;; esac
      if [ -e "$f" ] || [ -L "$f" ]; then git add -f -- "$f" || exit 1; fi
    done || exit 1
  tree="$(git write-tree)" || exit 1
  printf '%s\n' "$tree"
)

forge_verdict() {
  # Only a standalone final line is authoritative; prose/quoted markers are not.
  awk 'NF {last=$0} END {if(last ~ /^FORGE_VERDICT: (PASS|FAIL)$/) {sub(/^FORGE_VERDICT: /,"",last); print last} else print "UNKNOWN"}' "$1"
}

forge_export_tree() ( # source repository, tree, destination; no archive attributes
  local idx
  idx="$(mktemp "${TMPDIR:-/tmp}/forge-export.XXXXXX")" || exit 1
  rm -f "$idx"; trap 'rm -f "$idx"' EXIT
  export GIT_INDEX_FILE="$idx"
  git -C "$1" read-tree "$2" && git -C "$1" checkout-index --all --force --prefix="$3/"
)

forge_review_snapshot() ( # source, baseline tree, final tree, destination
  local src="$1" base="$2" tree="$3" dest="$4"
  [ ! -e "$dest" ] || exit 1
  mkdir -p "$dest" || exit 1
  git -C "$dest" init -q || exit 1
  base="$(git -C "$src" rev-parse "$base^{tree}")" || exit 1
  tree="$(git -C "$src" rev-parse "$tree^{tree}")" || exit 1
  printf '%s\n%s\n' "$base" "$tree" |
    git -C "$src" pack-objects --revs --stdout |
    git -C "$dest" index-pack --stdin >/dev/null || exit 1
  local baseline final
  baseline="$(echo baseline | git -C "$dest" -c user.name=forge -c user.email=forge@local commit-tree "$base")" || exit 1
  final="$(echo artifact | git -C "$dest" -c user.name=forge -c user.email=forge@local commit-tree "$tree" -p "$baseline")" || exit 1
  [ "$(git -C "$dest" rev-parse "$baseline^{tree}")" = "$base" ] || exit 1
  echo "$baseline" > "$dest/../review.base"
  git -C "$dest" reset --hard "$final" >/dev/null || exit 1
  [ "$(git -C "$dest" rev-parse HEAD^{tree})" = "$tree" ]

)

forge_fingerprint() {
  local tree head index
  tree="$(forge_tree "$1")" || return 1
  head="$(git -C "$1" rev-parse HEAD)" || return 1
  index="$(git -C "$1" diff --cached --binary HEAD | git -C "$1" hash-object --stdin)" || return 1
  printf '%s %s %s\n' "$head" "$tree" "$index"
}
