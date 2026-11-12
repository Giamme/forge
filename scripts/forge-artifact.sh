#!/usr/bin/env bash
# Shared artifact operations. A private index never stages or unstages user work.
# forge_tree <dir> [--tracked]: the tree of everything in <dir> that is not ignored. --tracked
# leaves untracked files out, for a copy whose artifact is a commit: there an untracked file
# (a test run's __pycache__/ in a repository with no .gitignore) cannot reach what merges.
forge_tree() (
  cd "$1" || exit 1
  local idx tree actual_index f add=-A
  [ "${2:-}" = --tracked ] && add=-u
  idx="$(mktemp "${TMPDIR:-/tmp}/forge-index.XXXXXX")" || exit 1
  rm -f "$idx"
  trap 'rm -f "$idx"' EXIT
  actual_index="$(git rev-parse --git-path index)" || exit 1
  export GIT_INDEX_FILE="$idx"
  git read-tree HEAD && git add "$add" -- . ':(exclude).forge' || exit 1
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
  # The QA prompt shows each verdict followed by a dash and its meaning, and some
  # reviewers copy that whole line; accept exactly that dash-separated gloss.
  # CRLF replies (some harnesses on some platforms) are the same line with a stray CR.
  awk 'NF {last=$0} END {sub(/\r$/,"",last); if(last ~ /^FORGE_VERDICT: (PASS|FAIL)([ \t]+(—|–|-)( .*)?)?[ \t]*$/) {sub(/^FORGE_VERDICT: /,"",last); sub(/[ \t].*$/,"",last); print last} else print "UNKNOWN"}' "$1"
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

# A review snapshot is a full export of the reviewed tree plus a git store, as large as the repository's
# tree, and nothing reads it once its review is over: the fingerprint check that ends the review is its
# last use (#139). review.base, review.deps, the prompt, the log and the verdict sit beside it and stay.
# FORGE_KEEP_SNAPSHOTS=1 keeps it, for debugging. Only a directory named like a snapshot is removed, never
# a link (rm -rf does not follow the dependency links forge_review_deps put inside it).
forge_drop_snapshot() { # review snapshot dir; always returns 0
  local dir="${1:-}"
  [ -n "$dir" ] && [ "${FORGE_KEEP_SNAPSHOTS:-0}" != 1 ] || return 0
  case "${dir##*/}" in review|review-*) ;; *) return 0 ;; esac
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 0
  rm -rf -- "$dir" 2>/dev/null
  return 0
}

# Where package managers install into a checkout: npm and bower, Python virtualenvs, Go, PHP and Ruby
# vendor dirs, Bundler's config, CocoaPods, Mix (deps, _build), Dart, Elm and SwiftPM (#127). The same
# names as forge-fail-first.py's DEP_DIRS. Only a directory the snapshot does not hold is linked, so a
# committed one of these names is never replaced.
FORGE_DEP_DIRS='node_modules bower_components .venv venv vendor .bundle Pods deps _build .dart_tool elm-stuff .build'

_forge_dep_dirs() { # checkout: its dependency directories, relative, never one inside another
  # Five levels reach apps/group/name/node_modules.
  local name names=()
  for name in $FORGE_DEP_DIRS; do names+=(${names[0]+-o} -name "$name"); done
  (cd "$1" && find . -maxdepth 5 \( -name .git -o -name .forge \) -prune -o \
      \( "${names[@]}" \) \( -type d -o -type l \) -print -prune 2>/dev/null) |
    sed 's#^\./##'
}

forge_review_deps() { # source checkout, review snapshot, note file (#117)
  # Links the dependency directories of the checkout under review (what .forge/setup filled, so
  # they match the attempt, unlike the source repo's) into its snapshot, at the root and in
  # workspace packages, and writes what the reviewer has to the note. An untracked link counts for
  # no fingerprint, and the exclude keeps it out of the reviewer's `git status`: a `node_modules/`
  # ignore pattern does not match a link. A directory the snapshot already holds is committed: it
  # stays as it is. `ln` fails where the snapshot lacks the parent (an old run's dir in a solo
  # repo): that is no part of the change, so nothing is linked there.
  local src rel linked="" committed
  src="$(cd "$1" && pwd)" || src=""
  committed="$(_forge_dep_dirs "$2" | sort | awk 'NR > 1 { printf ", " } { printf "%s", $0 }')"
  if [ -n "$src" ]; then
    while IFS= read -r rel; do
      [ -n "$rel" ] && [ -d "$src/$rel" ] && [ ! -e "$2/$rel" ] && [ ! -L "$2/$rel" ] || continue
      ln -s "$src/$rel" "$2/$rel" 2>/dev/null || continue
      mkdir -p "$2/.git/info" && echo "/$rel" >> "$2/.git/info/exclude"
      linked="$linked${linked:+, }$rel"
    done <<< "$(_forge_dep_dirs "$src" | sort)"
  fi
  {
    if [ -n "$linked" ]; then
      echo "Dependencies linked into your checkout from the attempt's own copy: $linked. Use them to run tests, and do not write to them. forge does not check that they are complete, or look more than five levels down."
    fi
    if [ -n "$committed" ]; then
      echo "Dependency directories committed in your checkout, holding only what was committed: $committed."
    fi
    if [ -z "$linked$committed" ]; then
      echo "Dependencies in your checkout: none. It holds committed files only, with no installed packages."
    fi
  } > "$3"
}

forge_tests_note() { # QA reply: the reason, when the reviewer says its tests could not run (#117)
  grep -qs 'FORGE_TESTS:' "$1" || return 0
  python3 "$SKILL_DIR/scripts/forge-contract.py" tests --qa-last "$1" 2>/dev/null |
    awk -F'\t' '$1 == "could_not_run" { print ($2 == "" ? "no reason given" : $2) }'
}

forge_fingerprint() { # forge_fingerprint <dir> [--tracked], as forge_tree
  local tree head index
  tree="$(forge_tree "$1" "${2:-}")" || return 1
  head="$(git -C "$1" rev-parse HEAD)" || return 1
  index="$(git -C "$1" diff --cached --binary HEAD | git -C "$1" hash-object --stdin)" || return 1
  printf '%s %s %s\n' "$head" "$tree" "$index"
}

forge_pre_review() { # review snapshot, task dir, repo, plan dir: 1 when the affected tests fail (#85)
  # Runs only the tests the attempt affects, in throwaway exports of the review snapshot, so no
  # fingerprint changes. Writes <dir>/prereview.json, the reviewer's evidence to prereview.txt
  # and, when they fail, the implementer's findings to prereview.md. Anything but a red check
  # (nothing selected, too broad, a broken check) returns 0: the review runs as usual.
  local cmd="" map="" reviewed rc
  rm -f "$2/prereview.json" "$2/prereview.txt" "$2/prereview.md"
  [ -s "$3/.forge/verify-subset" ] && cmd="$(cat "$3/.forge/verify-subset")"
  [ -s "$3/.forge/test-map" ] && map="$3/.forge/test-map"
  reviewed="$(git -C "$1" rev-parse HEAD)" || return 0
  python3 "$SKILL_DIR/scripts/forge-pre-review.py" run --repo "$1" --base "$(cat "$2/review.base")" \
      --reviewed "$reviewed" --command "$cmd" ${map:+--map "$map"} --out "$2/prereview.json" \
      --report "$2/prereview.md" --deps-from "$3" --slots-dir "$4/.prereview-slots" \
      --slots "$(cat "$4/pre_review_parallel" 2>/dev/null || echo 2)" --log "$2/prereview.log" \
      > "$2/prereview.txt" 2>/dev/null
  rc=$?
  forge_dataset_event pre_review --dir "$2" --file prereview="$2/prereview.json"
  [ "$rc" = 1 ] && [ -s "$2/prereview.md" ] && return 1
  [ "$rc" = 0 ] || : > "$2/prereview.txt"
  return 0
}

forge_fail_first() { # review snapshot, run or task dir, repo: advisory fail-first evidence for QA
  # Writes <dir>/failfirst.json and the reviewer's section to <dir>/failfirst.txt; never fails
  # the run and never touches the snapshot (forge-fail-first.py exports through its own index).
  local cmd="" reviewed
  rm -f "$2/failfirst.json" "$2/failfirst.txt"
  [ -s "$3/.forge/verify-subset" ] && cmd="$(cat "$3/.forge/verify-subset")"
  reviewed="$(git -C "$1" rev-parse HEAD)" || return 0
  python3 "$SKILL_DIR/scripts/forge-fail-first.py" run --repo "$1" --base "$(cat "$2/review.base")" \
      --reviewed "$reviewed" --command "$cmd" --out "$2/failfirst.json" --deps-from "$3" \
      > "$2/failfirst.txt" 2>/dev/null || : > "$2/failfirst.txt"
  forge_dataset_event fail_first --dir "$2" --file failfirst="$2/failfirst.json"
  return 0
}
