#!/usr/bin/env bash
# Verification helpers for forge-parallel.sh. Sourced; functions only.

# A lanes directory counts once it holds a lane file (dot files and backups are skipped,
# as forge-verify.py skips them).
verify_lanes_present() { # verify_lanes_present <dir>
  local f
  [ -d "$1" ] || return 1
  for f in "$1"/*; do
    [ -e "$f" ] || continue
    case "${f##*/}" in *~) continue ;; esac
    return 0
  done
  return 1
}

# Where a plan's verification comes from, in this order: the plan's lanes (--verify-lane,
# $PLAN/verify.d/), the plan's --verify, the repository's lanes (.forge/verify.d/), the
# repository's .forge/verify. Prints "<kind><TAB><location>" with kind lanes:plan,
# command:plan, lanes:repo or command:repo, or nothing when none is configured.
verify_source_for() { # verify_source_for <plan> <repo>
  if verify_lanes_present "$1/verify.d"; then printf 'lanes:plan\t%s\n' "$1/verify.d"
  elif [ -s "$1/verify_cmd" ]; then printf 'command:plan\t%s\n' "$1/verify_cmd"
  elif verify_lanes_present "$2/.forge/verify.d"; then printf 'lanes:repo\t%s\n' "$2/.forge/verify.d"
  elif [ -s "$2/.forge/verify" ]; then printf 'command:repo\t%s\n' "$2/.forge/verify"
  fi
  return 0
}

# The verify command a plan would run, as text: the command itself, or for lanes every
# lane's command (its #forge: option lines left out), one after another. Prints nothing
# when nothing is configured.
verify_command_for() { # verify_command_for <plan> <repo>
  local src loc
  src="$(verify_source_for "$1" "$2")"
  loc="${src#*$'\t'}"
  case "$src" in
    command:*) cat "$loc" ;;
    lanes:*) verify_lanes_command "$loc" ;;
  esac
  return 0
}

# The commands of the lanes in one directory, as text (see verify_command_for).
verify_lanes_command() { # verify_lanes_command <lanes dir>
  local f
  for f in "$1"/*; do
    [ -f "$f" ] || continue
    case "${f##*/}" in *~) continue ;; esac
    awk 'body || !/^#forge:/ { body = 1; print }' "$f"
  done
  return 0
}

# A fixed verification source, "command:<file>" or "lanes:<dir>", as verify_source_for prints
# one ("<kind><TAB><location>"), so a re-run uses the predicate an earlier verification used
# (its verification.command, its verification.lanes.d/) and never a newer config or discovery.
# Prints nothing, and fails, on anything else.
verify_fixed_source() { # verify_fixed_source <command:file | lanes:dir>
  case "$1" in
    command:?*) printf 'command:fixed\t%s\n' "${1#command:}" ;;
    lanes:?*) printf 'lanes:fixed\t%s\n' "${1#lanes:}" ;;
    *) return 1 ;;
  esac
}

# Freeze the lane files a verification runs, so the same lanes can be run again after the
# directory changed or vanished: a plain copy at $out/verification.lanes.d/, made before the
# runner starts. A re-run that already reads that directory leaves it alone.
verify_lanes_snapshot() { # verify_lanes_snapshot <lanes dir> <out>
  local snap="$2/verification.lanes.d" tmp
  [ "$1" -ef "$snap" ] && return 0
  tmp="$(mktemp -d "$2/.verification.lanes.XXXXXX")" || return 1
  if cp -R "$1/." "$tmp/" && rm -rf "$snap" && mv "$tmp" "$snap"; then return 0; fi
  rm -rf "$tmp"
  return 1
}

# Lanes are checked before any model is paid for: the runner's own validation, against the
# checkout the lanes will run in (baseline= files exist, reports= and baseline= stay inside).
# Dies with exit 2 and the runner's messages on an invalid source; a single command, or
# nothing, passes.
verify_lanes_check() { # verify_lanes_check <plan> <repo> <checkout>
  local src loc out
  src="$(verify_source_for "$1" "$2")"
  case "$src" in lanes:*) ;; *) return 0 ;; esac
  loc="${src#*$'\t'}"
  if [ "${src%%$'\t'*}" = lanes:repo ] && [ -s "$2/.forge/verify" ]; then
    note ".forge/verify is ignored: the repository declares verification lanes in .forge/verify.d/"
  fi
  out="$(python3 "$SKILL_DIR/scripts/forge-verify.py" lanes --lanes-dir "$loc" --checkout "$3" 2>&1 >/dev/null)" \
    || die "invalid verification lanes in $loc:
$out"
  return 0
}

# "Verify is whatever you hand it": a gate that skips a suite the project defines lets a
# regression in that suite reach the branch with no warning. Read the project's own test
# configuration and say which suites the command does not run. Advisory by design — it
# prints and records, it never changes an outcome, and a failure of the checker is silent.
verify_coverage_report() { # verify_coverage_report <checkout> <command> <out-file>
  local dir="$1" cmd="$2" out="$3" line file
  [ -f "$SKILL_DIR/scripts/forge-verify-coverage.py" ] || return 0
  rm -f "$out"
  if [[ "$cmd" == *$'\n'* ]]; then
    # Several lines (every lane's command, or a multi-line script): through a file.
    file="$(mktemp "${TMPDIR:-/tmp}/forge-verify-cmd.XXXXXX")" || return 0
    printf '%s\n' "$cmd" > "$file"
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --command-file "$file" --out "$out" >/dev/null 2>&1
    rm -f "$file"
  elif [ -n "$cmd" ]; then
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --command="$cmd" --out "$out" >/dev/null 2>&1
  else
    python3 "$SKILL_DIR/scripts/forge-verify-coverage.py" check --repo "$dir" --none --out "$out" >/dev/null 2>&1
  fi
  if [ -s "$out" ]; then
    while IFS= read -r line; do note "$line"; done < "$out"
  fi
  return 0
}

# Lanes mode of verify_result: the lane runner (forge-verify.py) through the same stoppable
# path as a single command, then verification.* from what it wrote. No whole-command rerun
# and no Jev triage: the runner reruns a failed lane itself. Returns 0 pass, 1 fail, 130
# interrupted.
verify_lanes_run() { # verify_lanes_run <plan> <checkout> <out> <lanes dir> <retries>
  local plan="$1" wt="$2" out="$3" dir="$4" retries="$5" parallel rc=0 cmd fpcmd
  parallel="$(cat "$plan/verify_parallel" 2>/dev/null)"
  case "$parallel" in ''|*[!0-9]*) parallel=3 ;; esac
  [ "$retries" -le 9 ] 2>/dev/null || retries=9          # the runner takes 0 to 9
  verify_lanes_snapshot "$dir" "$out" || {
    echo FAIL > "$out/verification.status"
    note "could not copy the verification lanes to $out/verification.lanes.d"; return 1
  }
  # From here on the frozen copy is the lanes' only source: an edit to the live directory
  # after the copy must not change what runs, or the snapshot would not be its predicate.
  dir="$out/verification.lanes.d"
  python3 "$SKILL_DIR/scripts/forge-verify.py" lanes --lanes-dir "$dir" 2>/dev/null \
    | awk -F'\t' '{ print $1 ": " $3 }' > "$out/verification.command"
  verify_coverage_report "$wt" "$(verify_lanes_command "$dir")" "$out/verification.coverage.txt"
  forge_fingerprint "$wt" > "$out/verification.fingerprint" || return 1
  # The runner judges "a lane changed the checkout" by forge's own fingerprint.
  fpcmd="source $(printf %q "$SKILL_DIR/scripts/forge-artifact.sh") && forge_fingerprint ."
  cmd="exec python3 $(printf %q "$SKILL_DIR/scripts/forge-verify.py") run --checkout $(printf %q "$wt")"
  cmd="$cmd --out $(printf %q "$out") --lanes-dir $(printf %q "$dir") --parallel $parallel --retries $retries"
  cmd="$cmd --fingerprint-cmd $(printf %q "$fpcmd")"
  verify_stopped "$out" && return 130
  # The runner stops its own lanes (TERM, then KILL 5 s later; each lane is a session the
  # outer KILL cannot reach), so its grace here outlasts that.
  verify_exec "$wt" "$cmd" "$out/verification.log" 15 || rc=$?
  [ "$FORGE_VERIFY_STOP" = 1 ] && rc=130
  echo "$rc" > "$out/verification.exit"
  # verification.flaky per flaky lane; then each failed lane's last 200 lines under the summary
  python3 - "$out" <<'PY'
import json, os, sys
out = sys.argv[1]
try:
    with open(os.path.join(out, 'verification.lanes.json'), encoding='utf-8') as fh:
        doc = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
lanes = [l for l in doc.get('lanes') or [] if isinstance(l, dict) and l.get('name')]
flaky = [l for l in lanes if l.get('state') == 'flaky']
if flaky:
    with open(os.path.join(out, 'verification.flaky'), 'w', encoding='utf-8') as fh:
        fh.write('lanes=%s\n' % ','.join(l['name'] for l in flaky))
        for l in flaky:
            runs = l.get('runs') or [{}]
            fh.write('%s.first_exit=%s\n%s.retries_used=%d\n' % (l['name'], runs[0].get('exit'), l['name'], len(runs) - 1))

def tail(name, lines=200, size=256 << 10):
    try:
        with open(os.path.join(out, os.path.basename(name or '')), 'rb') as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - size))
            return fh.read().decode('utf-8', 'replace').splitlines()[-lines:]
    except OSError:
        return []

with open(os.path.join(out, 'verification.log'), 'a', encoding='utf-8') as fh:
    for l in lanes:
        if l.get('state') != 'fail':
            continue
        runs = l.get('runs') or []
        lines = tail(runs[-1].get('log') if runs else l.get('log'))
        fh.write('\n== lane %s failed (exit %s); the last %d lines of its output:\n' % (l['name'], l.get('exit'), len(lines)))
        fh.write(''.join(line + '\n' for line in lines))
PY
  case "$rc" in
    0) echo PASS > "$out/verification.status" ;;
    130) verify_mark_interrupted "$out"; return 130 ;;
    *) echo FAIL > "$out/verification.status" ;;
  esac
  local flaky; flaky="$(sed -n 's/^lanes=//p' "$out/verification.flaky" 2>/dev/null)"
  [ -z "$flaky" ] || note "FLAKY: lane ${flaky//,/, lane } failed once and passed on rerun; this is not a clean pass"
  [ "$rc" = 0 ] && return 0
  note "verification failed ($( [ "$rc" = 2 ] && echo 'the lane runner refused its configuration' || echo 'see the lanes below' )): $out/verification.log"
  return 1
}
