#!/usr/bin/env bash
# Integration attribution for forge-parallel.sh (#45). Sourced; functions only.
#
# When a run's combined verification fails, every task passed its own review and the merge as a
# whole is broken. This searches the merge order for a BREAKING MERGE: a merge k whose prefix
# k-1 verified PASS and whose prefix k verified FAIL, both actually verified. Prefix 0 is the
# integration base, prefix n the tip. Verification is an arbitrary command, so failure need not
# be monotonic over the merges (1 breaks it, 2 fixes it, 3 breaks it again): a binary search finds
# *a* verified pass->fail boundary, not the earliest failing merge, and nothing here says "first".
#
#   forge-parallel.sh attribute <plan> [--force]
#   forge-parallel.sh run <plan> --attribute        (the same search after a failed verification)
#
# Every step verifies a prefix in a scratch worktree with the verification the run used (its
# verification.lanes.d/, or its verification.command), never today's config and never discovery.
# Nothing is merged, reverted or accepted; no task status changes.
#
# State of one search, in files under $PLAN/attribute/ (the flag file is $PLAN/attribute_on):
#   merges.tsv   k, commit, task id        steps.tsv   prefix, commit, status, seconds, source
# Each verified step writes into $PLAN/attribute/<prefix>-<short sha>/.

# The prefix's commit: the base, or the k-th first-parent merge commit. The last merge commit is
# the tip unless something follows it (a `forge: combine` commit, a hand commit); the run's
# failure belongs to the tip, so it only stands for prefix n when the two are the same commit.
attribute_commit() { # attribute_commit <k>
  if [ "$1" = 0 ]; then printf '%s\n' "$ATTR_BASE"
  else awk -F'\t' -v k="$1" '$1 == k { print $2; exit }' "$ATTR_DIR/merges.tsv"
  fi
}

# The scratch worktree is this search's own: a path no task owns and nothing else uses. A task id
# may be any name, so `_attribute` can be a task's worktree; that one is never touched. A detached
# worktree at the path is a leftover of an earlier search and is reused.
attribute_scratch_pick() { # attribute_scratch_pick <wt_root>
  local root="$1" name="_attribute" i=0
  git -C "$ATTR_REPO" worktree prune >/dev/null 2>&1
  while :; do
    if ! { [ -s "$ATTR_PLAN/tasks.tsv" ] && all_ids "$ATTR_PLAN/tasks.tsv" | grep -qxF -- "$name"; } \
       && { [ ! -e "$root/$name" ] || { [ -e "$root/$name/.git" ] && ! git -C "$root/$name" symbolic-ref -q HEAD >/dev/null 2>&1; }; }; then
      break
    fi
    i=$((i + 1)); name="_attribute-$i"
  done
  ATTR_SCRATCH="$root/$name"
  ATTR_SCRATCH_OWNED=1
}

attribute_scratch_remove() {
  [ "${ATTR_SCRATCH_OWNED:-0}" = 1 ] && [ -n "${ATTR_SCRATCH:-}" ] || return 0
  git -C "$ATTR_REPO" worktree remove --force "$ATTR_SCRATCH" >/dev/null 2>&1
  rm -rf "$ATTR_SCRATCH"
  git -C "$ATTR_REPO" worktree prune >/dev/null 2>&1
  return 0
}

attribute_record() { # attribute_record <prefix> <commit> <status> <seconds> <source>
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$ATTR_DIR/steps.tsv"
}

# Verify prefix k once. The result is cached: a second call answers from steps.tsv. Sets
# ATTR_STATUS (PASS, FAIL, UNVERIFIED, INTERRUPTED or SETUP_FAILED) and, when it is not a
# verified result, ATTR_REASON. Returns 0 verified, 1 inconclusive, 2 the step could not run.
attribute_verify() { # attribute_verify <k>
  local k="$1" commit dir t0 rc=0 status setup_cmd
  ATTR_REASON=""
  status="$(awk -F'\t' -v k="$k" '$1 == k && ($3 == "PASS" || $3 == "FAIL") { print $3; exit }' "$ATTR_DIR/steps.tsv" 2>/dev/null)"
  if [ -n "$status" ]; then ATTR_STATUS="$status"; return 0; fi
  commit="$(attribute_commit "$k")"
  dir="$ATTR_DIR/$k-${commit:0:12}"
  rm -rf "$dir"; mkdir -p "$dir" || { ATTR_REASON="could not create $dir"; return 2; }
  attribute_scratch_remove
  if ! git -C "$ATTR_REPO" worktree add --detach "$ATTR_SCRATCH" "$commit" > "$dir/worktree.out" 2>&1; then
    ATTR_REASON="could not create the scratch worktree for prefix $k: $(tail -1 "$dir/worktree.out")"
    return 2
  fi
  note "attribute: verifying prefix $k of $ATTR_N (${commit:0:12})"
  populate_submodules "$ATTR_REPO" "$ATTR_SCRATCH" "$dir" "prefix $k"
  setup_cmd="$(worktree_setup_command "$ATTR_PLAN" "$ATTR_REPO")"
  if [ -n "$setup_cmd" ]; then
    # Stoppable like the verification: a stop reaches the setup command's whole group.
    ( FORGE_RUN_STOPPABLE=1 FORGE_VERIFY_STOP=0 FORGE_VERIFY_GROUP="" FORGE_VERIFY_TIMER=""
      trap verify_signal TERM INT
      verify_exec "$ATTR_SCRATCH" "$setup_cmd" "$dir/setup.out" ) <&0 &
    ATTR_CHILD=$!
    [ "$ATTR_STOP" = 1 ] && kill -TERM "$ATTR_CHILD" 2>/dev/null
    run_wait "$ATTR_CHILD"; rc=$run_rc
    ATTR_CHILD=""
    if [ "$rc" = 130 ] || [ "$ATTR_STOP" = 1 ]; then
      ATTR_STATUS=INTERRUPTED; ATTR_REASON="interrupted during the worktree setup of prefix $k"
      attribute_record "$k" "$commit" SETUP_INTERRUPTED 0 search
      attribute_scratch_remove
      return 1
    elif [ "$rc" != 0 ]; then
      attribute_record "$k" "$commit" SETUP_FAILED 0 search
      ATTR_STATUS=SETUP_FAILED
      ATTR_REASON="the worktree setup failed at prefix $k (see $dir/setup.out)"
      attribute_scratch_remove
      return 1
    fi
  fi
  t0="$(date +%s)"
  # In the background, as `run` does: a stop reaches this shell, which passes it on, and the
  # verification stops its command's whole group.
  ( FORGE_RUN_STOPPABLE=1; verify_result_body "$ATTR_PLAN" "$ATTR_SCRATCH" "$dir" "$ATTR_SOURCE" ) <&0 &
  ATTR_CHILD=$!
  [ "$ATTR_STOP" = 1 ] && kill -TERM "$ATTR_CHILD" 2>/dev/null
  run_wait "$ATTR_CHILD"; rc=$run_rc
  ATTR_CHILD=""
  status="$(cat "$dir/verification.status" 2>/dev/null)"
  if [ "$rc" = 130 ] || [ "$ATTR_STOP" = 1 ] || [ "$status" = INTERRUPTED ]; then
    ATTR_STATUS=INTERRUPTED; ATTR_REASON="interrupted while verifying prefix $k"
  else
    case "$status" in
      PASS|FAIL) ATTR_STATUS="$status" ;;
      *) ATTR_STATUS=UNVERIFIED; ATTR_REASON="prefix $k could not be verified (see $dir)" ;;
    esac
  fi
  attribute_record "$k" "$commit" "$ATTR_STATUS" "$(( $(date +%s) - t0 ))" search
  attribute_scratch_remove
  case "$ATTR_STATUS" in PASS|FAIL) return 0 ;; esac
  return 1
}

# Prefix lo is known PASS and prefix hi known FAIL: halve the interval until hi = lo + 1. Merge
# hi is then a breaking merge (ATTR_LO, ATTR_HI). Returns as attribute_verify does.
attribute_bisect() { # attribute_bisect <lo> <hi>
  local lo="$1" hi="$2" mid rc
  while [ $((hi - lo)) -gt 1 ]; do
    mid=$(( (lo + hi) / 2 ))
    attribute_verify "$mid"; rc=$?
    [ "$rc" = 0 ] || return "$rc"
    if [ "$ATTR_STATUS" = PASS ]; then lo="$mid"; else hi="$mid"; fi
  done
  ATTR_LO="$lo"; ATTR_HI="$hi"
  return 0
}

# attribution.json (atomically), from merges.tsv and steps.tsv.
attribute_write_json() { # attribute_write_json <culprit prefix or empty> <base_fails 0|1> <inconclusive text>
  python3 - "$ATTR_PLAN" "$ATTR_DIR" "$1" "$2" "$3" <<'PY'
import json, os, sys
plan, adir, culprit_k, base_fails, inconclusive = sys.argv[1:6]

def rows(name):
    try:
        with open(os.path.join(adir, name), encoding='utf-8') as fh:
            return [line.rstrip('\n').split('\t') for line in fh if line.strip()]
    except OSError:
        return []

merges = [{'id': r[2], 'commit': r[1]} for r in rows('merges.tsv')]
steps = []
for r in rows('steps.tsv'):
    steps.append({'prefix': int(r[0]), 'commit': r[1], 'status': r[2],
                  'seconds': int(r[3]) if r[3] else None, 'source': r[4]})
doc = {
    'culprit': merges[int(culprit_k) - 1]['id'] if culprit_k else None,
    'base_fails': base_fails == '1',
    'inconclusive': inconclusive or None,
    'merges': merges,
    'steps': steps,
    'verification_runs': sum(1 for s in steps if s['source'] == 'search' and not s['status'].startswith('SETUP_')),
    'gate': None,
}
tmp = os.path.join(plan, '.attribution.json.tmp')
with open(tmp, 'w', encoding='utf-8') as fh:
    json.dump(doc, fh, indent=2)
    fh.write('\n')
os.replace(tmp, os.path.join(plan, 'attribution.json'))
PY
}

# tasks/<culprit>/verification.md: the failing step's log, its last 200 lines, under a header.
attribute_write_log() { # attribute_write_log <prefix k>
  local k="$1" commit id dir log tmp
  commit="$(attribute_commit "$k")"
  id="$(awk -F'\t' -v k="$k" '$1 == k { print $3; exit }' "$ATTR_DIR/merges.tsv")"
  dir="$ATTR_DIR/$k-${commit:0:12}"
  log="$dir/verification.log"
  [ -f "$log" ] || log="$ATTR_PLAN/verification.log"       # the tip, known FAIL from the run itself
  mkdir -p "$ATTR_PLAN/tasks/$id" || return 0
  tmp="$ATTR_PLAN/tasks/$id/.verification.md.$$"
  {
    printf '# Verification failed at prefix %s (%s)\n\n' "$k" "$commit"
    printf 'Merge %s is a merge that breaks verification: prefix %s passed and prefix %s failed.\n' "$id" "$((k - 1))" "$k"
    printf 'The last 200 lines of the failing verification log follow.\n\n```\n'
    tail -n 200 "$log" 2>/dev/null
    printf '```\n'
  } > "$tmp" && mv -f "$tmp" "$ATTR_PLAN/tasks/$id/verification.md" || rm -f "$tmp"
  return 0
}

# What the search says and records once it has stopped: attribution.json, the culprit's log, the
# dataset event and the summary line.
attribute_conclude() { # attribute_conclude <culprit prefix or empty> <base_fails 0|1> <inconclusive text>
  local runs
  attribute_write_json "$1" "$2" "$3" || return 1
  [ -z "$1" ] || attribute_write_log "$1"
  forge_dataset_event attribution --dir "$ATTR_PLAN" --file attribution="$ATTR_PLAN/attribution.json"
  runs="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["verification_runs"])' "$ATTR_PLAN/attribution.json" 2>/dev/null)"
  if [ -n "$1" ]; then
    printf 'integration failure: merge %s breaks verification (prefix %s passes, %s fails; %s verification runs)\n' \
      "$(awk -F'\t' -v k="$1" '$1 == k { print $3; exit }' "$ATTR_DIR/merges.tsv")" "$(( $1 - 1 ))" "$1" "$runs"
  elif [ "$2" = 1 ]; then
    printf 'integration failure: the base already fails verification (%s runs)\n' "$runs"
  else
    printf 'integration failure: attribution inconclusive — %s (%s runs)\n' "$3" "$runs"
  fi
}

# Everything the search needs, checked before anything runs. Each refusal is exit 2.
attribute_evidence() { # attribute_evidence <plan> <force>
  local plan="$1" force="$2" repo run_id
  [ -s "$plan/wt_root" ] && [ -s "$plan/repo" ] || die "this plan has never been run: there is nothing to attribute"
  repo="$(cat "$plan/repo")"; run_id="$(cat "$plan/run_id" 2>/dev/null)"
  if [ "$force" != 1 ]; then
    [ "$(cat "$plan/verification.status" 2>/dev/null)" = FAIL ] \
      || die "the run's verification did not fail (verification.status is '$(cat "$plan/verification.status" 2>/dev/null)'): nothing to attribute; --force verifies the tip itself"
    [ -s "$plan/verification.commit" ] && [ -s "$plan/accepted.integration" ] \
      && [ "$(cat "$plan/verification.commit")" = "$(cat "$plan/accepted.integration")" ] \
      || die "the failed verification was not of the accepted integration commit: it says nothing about the tip; --force verifies the tip itself"
  fi
  [ -s "$plan/integration.base" ] || die "$plan/integration.base is missing: no base to search from"
  git -C "$repo" rev-parse --verify --quiet "$(cat "$plan/integration.base")^{commit}" >/dev/null \
    || die "the integration base $(cat "$plan/integration.base") is not a commit of $repo"
  git -C "$repo" rev-parse --verify --quiet "forge/$run_id-integration^{commit}" >/dev/null \
    || die "the integration branch forge/$run_id-integration does not exist in $repo"
  if [ "$force" != 1 ] && [ "$(git -C "$repo" rev-parse "forge/$run_id-integration")" != "$(cat "$plan/verification.commit")" ]; then
    die "the integration branch moved since the failed verification: it says nothing about the tip; --force verifies the tip itself"
  fi
  return 0
}

# The search. Runs as a subshell of its caller (`( attribute_search ... )`): it owns its scratch
# worktree, its traps and its globals. Exit 0 when it finished (a culprit or not), 1 when a step
# could not run at all, 2 on a refusal.
attribute_search() { # attribute_search <plan> <force 0|1>
  ATTR_PLAN="$1"; local force="$2" run_id rc=0 line k=0 src kind loc
  attribute_evidence "$ATTR_PLAN" "$force"
  ATTR_REPO="$(cat "$ATTR_PLAN/repo")"; run_id="$(cat "$ATTR_PLAN/run_id")"
  ATTR_DIR="$ATTR_PLAN/attribute"
  ATTR_BASE="$(git -C "$ATTR_REPO" rev-parse "$(cat "$ATTR_PLAN/integration.base")^{commit}")"
  ATTR_TIP="$(git -C "$ATTR_REPO" rev-parse "forge/$run_id-integration^{commit}")"
  ATTR_SCRATCH="" ATTR_SCRATCH_OWNED=0
  ATTR_STOP=0 ATTR_CHILD="" ATTR_STATUS="" ATTR_REASON="" ATTR_LO="" ATTR_HI="" ATTR_SOURCE=""
  trap 'ATTR_STOP=1; [ -n "$ATTR_CHILD" ] && kill -TERM "$ATTR_CHILD" 2>/dev/null' TERM INT
  trap 'attribute_scratch_remove' EXIT
  attribute_scratch_pick "$(cat "$ATTR_PLAN/wt_root")"
  rm -rf "$ATTR_DIR" "$ATTR_PLAN/attribution.json"
  mkdir -p "$ATTR_DIR" || die "could not create $ATTR_DIR" 1
  : > "$ATTR_DIR/steps.tsv"

  # The merges, oldest first, as forge-sweep.py merged_since reads the subjects.
  git -C "$ATTR_REPO" log --first-parent --reverse --format='%H %s' "$ATTR_BASE..$ATTR_TIP" 2>/dev/null \
    | awk '{ if (match($0, /^[0-9a-f]+ forge: merge [^ ]+ *$/)) { n++; printf "%d\t%s\t%s\n", n, $1, $4 } }' > "$ATTR_DIR/merges.tsv"
  ATTR_N="$(wc -l < "$ATTR_DIR/merges.tsv" | tr -d ' ')"

  # The run's own predicate. Lanes win: a lanes verification leaves a verification.command that
  # only describes them.
  if verify_lanes_present "$ATTR_PLAN/verification.lanes.d"; then ATTR_SOURCE="lanes:$ATTR_PLAN/verification.lanes.d"
  elif [ -s "$ATTR_PLAN/verification.command" ]; then ATTR_SOURCE="command:$ATTR_PLAN/verification.command"
  elif [ "$force" = 1 ]; then
    src="$(verify_source_for "$ATTR_PLAN" "$ATTR_REPO")"; kind="${src%%$'\t'*}"; loc="${src#*$'\t'}"
    case "$kind" in
      lanes:*) ATTR_SOURCE="lanes:$loc" ;;
      command:*) ATTR_SOURCE="command:$loc" ;;
    esac
  fi
  if [ -z "$ATTR_SOURCE" ]; then
    if [ "$force" = 1 ]; then attribute_conclude "" 0 "no verification is configured"
    else attribute_conclude "" 0 "the run's verification was not recorded"; fi
    return 0
  fi
  if [ "$ATTR_N" = 0 ]; then
    attribute_conclude "" 0 "the integration branch has no merge after its base"
    return 0
  fi

  # 1. the base
  attribute_verify 0; rc=$?
  [ "$rc" != 2 ] || { note "attribute: $ATTR_REASON"; return 1; }
  if [ "$rc" = 1 ]; then attribute_conclude "" 0 "$ATTR_REASON"; return 0; fi
  if [ "$ATTR_STATUS" = FAIL ]; then attribute_conclude "" 1 ""; return 0; fi
  # 2. the tip: FAIL from the run's own verification when it is the last merge commit itself;
  # otherwise (a commit follows the last merge, or --force) prefix n is verified here, so a
  # boundary is always between two verified merge commits.
  if [ "$force" = 1 ] || [ "$(attribute_commit "$ATTR_N")" != "$ATTR_TIP" ]; then
    attribute_verify "$ATTR_N"; rc=$?
    [ "$rc" != 2 ] || { note "attribute: $ATTR_REASON"; return 1; }
    if [ "$rc" = 1 ]; then attribute_conclude "" 0 "$ATTR_REASON"; return 0; fi
    if [ "$ATTR_STATUS" = PASS ]; then
      if [ "$(attribute_commit "$ATTR_N")" = "$ATTR_TIP" ]; then attribute_conclude "" 0 "the tip passes"
      else attribute_conclude "" 0 "the last merge passes: the failure comes from a commit after it, not from a merge"; fi
      return 0
    fi
  else
    attribute_record "$ATTR_N" "$ATTR_TIP" FAIL "" run
  fi
  # 3. the boundary between the passing base and the failing tip
  attribute_bisect 0 "$ATTR_N"; rc=$?
  [ "$rc" != 2 ] || { note "attribute: $ATTR_REASON"; return 1; }
  if [ "$rc" = 1 ]; then attribute_conclude "" 0 "$ATTR_REASON"; return 0; fi
  attribute_conclude "$ATTR_HI" 0 ""
  return 0
}

# forge-parallel.sh attribute <plan> [--force]
do_attribute() {
  local PLAN="${1:?attribute needs a plan dir}"; shift
  local force=0 job="" rc=0 stopped=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=1; shift ;;
      *) die "attribute: unknown option '$1'" ;;
    esac
  done
  # A job this shell can stop: a TERM here reaches the search, which stops its verification.
  trap 'stopped=1; [ -n "$job" ] && kill -TERM "$job" 2>/dev/null' TERM INT
  ( attribute_search "$PLAN" "$force" ) <&0 &
  job=$!
  [ "$stopped" = 1 ] && kill -TERM "$job" 2>/dev/null
  run_wait "$job"; rc=$run_rc
  trap - TERM INT
  [ "$stopped" = 1 ] && [ "$rc" -gt 128 ] && rc=130
  return "$rc"
}
