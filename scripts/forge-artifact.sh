#!/usr/bin/env bash
# Shared artifact operations. A private index never stages or unstages user work.
_FORGE_ARTIFACT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_forge_links_script() {
  if [ -n "${SKILL_DIR:-}" ] && [ -f "$SKILL_DIR/scripts/forge-review-links.py" ]; then
    printf '%s\n' "$SKILL_DIR/scripts/forge-review-links.py"
  else
    printf '%s\n' "$_FORGE_ARTIFACT_DIR/forge-review-links.py"
  fi
}

_forge_links_run() {
  if declare -F forge_ensure_python3 >/dev/null 2>&1; then forge_ensure_python3; fi
  local py=python3
  case "$(command -v python3 2>/dev/null || true)" in
    ''|*[Ww]indows[Aa]pps*) command -v python >/dev/null 2>&1 && py=python ;;
  esac
  "$py" "$@"
}

_forge_link_file() { # source file, dest path; hardlink on native Windows (no Admin), symlink on POSIX
  local src="$1" dest="$2" is_win=1
  if declare -F forge_is_windows >/dev/null 2>&1; then
    forge_is_windows && is_win=0
  else
    case "${OSTYPE:-}" in
      msys*|cygwin*) [ "${FORGE_WIN_MODE:-native}" != msys2 ] && is_win=0 ;;
    esac
  fi
  if [ "$is_win" = 0 ]; then
    ln "$src" "$dest" 2>/dev/null
  else
    ln -s "$src" "$dest" 2>/dev/null
  fi
}

forge_capture_ignored_before() { # checkout, out_file (#198)
  local script
  script="$(_forge_links_script)"
  [ -f "$script" ] || return 0
  _forge_links_run "$script" capture-before "$1" "$2" 2>/dev/null || true
}

forge_record_setup_generated() { # checkout, before_file, [out_file] (#198)
  local script
  script="$(_forge_links_script)"
  [ -f "$script" ] || return 0
  if [ -n "${3:-}" ]; then
    _forge_links_run "$script" capture-after "$1" "$2" --out "$3" --dep-dirs "$FORGE_DEP_DIRS" 2>/dev/null || true
  else
    _forge_links_run "$script" capture-after "$1" "$2" --dep-dirs "$FORGE_DEP_DIRS" 2>/dev/null || true
  fi
}
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

forge_review_deps() { # source checkout, review snapshot, note file, [extra paths file] (#117, #198)
  # Links the dependency directories and setup-generated (or .forge/review-links) ignored paths of
  # the checkout under review into its snapshot, and writes what the reviewer has to the note. An
  # untracked link counts for no fingerprint, and .git/info/exclude keeps it out of `git status`:
  # a `node_modules/` ignore pattern does not match a link. Paths the snapshot already holds stay
  # as they are. Secret-looking files (such as .env) are never linked.
  local src rel linked="" committed gen_linked="" gen_unlinked="" kind script plan_rows extra="${4:-}"
  src="$(cd "$1" && pwd)" || src=""
  committed="$(_forge_dep_dirs "$2" | sort | awk 'NR > 1 { printf ", " } { printf "%s", $0 }')"
  if [ -n "$src" ]; then
    while IFS= read -r rel; do
      [ -n "$rel" ] && [ -d "$src/$rel" ] && [ ! -e "$2/$rel" ] && [ ! -L "$2/$rel" ] || continue
      if declare -F forge_link_dir >/dev/null 2>&1; then
        forge_link_dir "$src/$rel" "$2/$rel" 2>/dev/null || continue
      else
        ln -s "$src/$rel" "$2/$rel" 2>/dev/null || continue
      fi
      mkdir -p "$2/.git/info" && echo "/$rel" >> "$2/.git/info/exclude"
      linked="$linked${linked:+, }$rel"
    done <<< "$(_forge_dep_dirs "$src" | sort)"

    script="$(_forge_links_script)"
    if [ -f "$script" ]; then
      if [ -n "$extra" ]; then
        plan_rows="$(_forge_links_run "$script" plan-extra "$src" "$2" --extra-file "$extra" --dep-dirs "$FORGE_DEP_DIRS" 2>/dev/null || true)"
      else
        plan_rows="$(_forge_links_run "$script" plan-extra "$src" "$2" --dep-dirs "$FORGE_DEP_DIRS" 2>/dev/null || true)"
      fi
      while IFS=$'\t' read -r kind rel; do
        rel="${rel%$'\r'}"
        [ -n "$kind" ] && [ -n "$rel" ] || continue
        case "$kind" in
          LINK_DIR)
            if declare -F forge_link_dir >/dev/null 2>&1; then
              if forge_link_dir "$src/$rel" "$2/$rel" 2>/dev/null; then
                mkdir -p "$2/.git/info" && echo "/$rel" >> "$2/.git/info/exclude"
                gen_linked="$gen_linked${gen_linked:+, }$rel"
              else
                gen_unlinked="$gen_unlinked${gen_unlinked:+, }$rel"
              fi
            elif ln -s "$src/$rel" "$2/$rel" 2>/dev/null; then
              mkdir -p "$2/.git/info" && echo "/$rel" >> "$2/.git/info/exclude"
              gen_linked="$gen_linked${gen_linked:+, }$rel"
            else
              gen_unlinked="$gen_unlinked${gen_unlinked:+, }$rel"
            fi
            ;;
          LINK_FILE)
            if _forge_link_file "$src/$rel" "$2/$rel"; then
              mkdir -p "$2/.git/info" && echo "/$rel" >> "$2/.git/info/exclude"
              gen_linked="$gen_linked${gen_linked:+, }$rel"
            else
              gen_unlinked="$gen_unlinked${gen_unlinked:+, }$rel"
            fi
            ;;
          UNLINKED)
            gen_unlinked="$gen_unlinked${gen_unlinked:+, }$rel"
            ;;
        esac
      done <<< "$plan_rows"
    fi
  fi
  {
    if [ -n "$linked" ]; then
      echo "Dependencies linked into your checkout from the attempt's own copy: $linked. Use them to run tests, and do not write to them. forge does not check that they are complete, or look more than five levels down."
    elif [ -z "$committed" ] && [ -n "$gen_linked$gen_unlinked" ]; then
      echo "Dependencies in your checkout: no dependency directories."
    fi
    if [ -n "$committed" ]; then
      echo "Dependency directories committed in your checkout, holding only what was committed: $committed."
    fi
    if [ -n "$gen_linked" ]; then
      echo "Setup-generated paths linked into your checkout from the attempt's own copy: $gen_linked. Use them to run tests, and do not write to them."
    fi
    if [ -n "$gen_unlinked" ]; then
      echo "Setup-generated paths forge could not provide in your checkout: $gen_unlinked."
    fi
    if [ -z "$linked$committed$gen_linked$gen_unlinked" ]; then
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
