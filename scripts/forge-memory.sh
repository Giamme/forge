#!/usr/bin/env bash
# forge-memory.sh — project memory that outlives a single forge run.
#
# Every forge dispatch is a clean slate, so without this the tenth run in a repo
# rediscovers what the first one learned. Two files, deliberately split:
#
#   .forge/memory.md    curated, hard-capped, injected into every prompt
#   .forge/ledger.tsv   append-only, one row per dispatch, NEVER injected
#
# That split is what makes "remembers everything" and "small" compatible. The
# ledger is unbounded but costs no context; only memory.md is paid for, on every
# dispatch of every future run, which is why it has a hard cap.
#
# Writing is automatic, so the judgment has to be free and the bookkeeping has to
# be deterministic: the model that just did the work emits its learning in-band
# (a FORGE_LEARNING line in its final message, alongside FORGE_VERDICT), and this
# script does dedup, recurrence counting, staleness and capping in bash. No model
# ever decides what to prune.
#
# Usage:
#   forge-memory.sh inject <repo> <planner|dwarf|qa>
#   forge-memory.sh note   <planner|dwarf|qa>
#   forge-memory.sh record <repo> --last <file> --role <r> [--run-id X] [--task T]
#                                 [--model M] [--verdict V]
#   forge-memory.sh show   <repo>
#   forge-memory.sh prune  <repo>
#
# Exit codes: 0 ok | 2 usage error
set -uo pipefail

die()  { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }

MAX_LINES="${FORGE_MEMORY_MAX_LINES:-40}"
MAX_BYTES="${FORGE_MEMORY_MAX_BYTES:-4096}"
TAB="$(printf '\t')"

# Writing files into someone's repo automatically needs an escape hatch that does
# not require editing the skill.
memory_off() { [ "${FORGE_MEMORY:-on}" = "off" ]; }

dir_for()    { echo "$1/.forge"; }
memory_for() { echo "$1/.forge/memory.md"; }
ledger_for() { echo "$1/.forge/ledger.tsv"; }

# Normalized dedup key: lowercase, every run of non-alphanumerics collapsed to a
# single space. Exact match after that — genuinely different phrasings will NOT
# merge. That is mitigated by a feedback loop rather than by clever matching:
# memory is in the prompt, so a reviewer that has seen "2x: route added without
# updating openapi.yaml" tends to re-emit that phrasing instead of inventing one.
norm_key() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]\{1,\}/ /g; s/^ //; s/ $//'
}

# A trailing [path] is what makes an entry's staleness detectable later.
anchor_of() {
  printf '%s' "$1" | sed -n 's/.*\[\([^][]\{1,\}\)\][[:space:]]*$/\1/p'
}
strip_anchor() {
  printf '%s' "$1" | sed 's/[[:space:]]*\[[^][]\{1,\}\][[:space:]]*$//'
}

# Tabs are the ledger's field separator and newlines are its record separator, so
# neither may survive into a field.
sanitize() { printf '%s' "$1" | tr '\t\n' '  ' | sed 's/  \{1,\}/ /g; s/^ //; s/ $//'; }

# One discursive model should not be able to spend the whole byte budget on a
# single entry. Truncate the prose but keep the [anchor], since that is what lets
# the entry be recognised as stale later.
MAX_FACT_CHARS="${FORGE_MEMORY_MAX_FACT_CHARS:-180}"
clamp_fact() {
  local text="$1" anchor body
  [ "${#text}" -le "$MAX_FACT_CHARS" ] && { printf '%s' "$text"; return; }
  anchor="$(anchor_of "$text")"
  body="$(strip_anchor "$text")"
  local room="$MAX_FACT_CHARS"
  [ -n "$anchor" ] && room=$((MAX_FACT_CHARS - ${#anchor} - 3))
  [ "$room" -lt 40 ] && room=40
  body="$(printf '%s' "$body" | cut -c1-"$room" | sed 's/[^ ]*$//; s/ $//')"
  if [ -n "$anchor" ]; then printf '%s… [%s]' "$body" "$anchor"; else printf '%s…' "$body"; fi
}

section_title() {
  case "$1" in
    verify)  echo "## Verify" ;;
    trap)    echo "## Known traps" ;;
    finding) echo "## Recurring QA findings" ;;
  esac
}

# --- rebuild ------------------------------------------------------------------
# memory.md is regenerated from the whole ledger rather than mutated in place.
# Rebuilding is idempotent and order-independent, so a crashed or concurrent run
# cannot leave memory half-updated — it can only leave the ledger short a row.
rebuild() { # rebuild <repo> -> prints "kept<TAB>pruned_stale<TAB>pruned_cap"
  local repo="$1" led mem tmp cand
  led="$(ledger_for "$repo")"; mem="$(memory_for "$repo")"
  [ -f "$led" ] || return 0
  tmp="$mem.tmp.$$"; cand="$mem.cand.$$"

  # Group by (category, key). count = number of DISTINCT runs the key appeared in,
  # which is what makes a recurrence threshold meaningful: one run emitting the
  # same learning twice is still one occurrence.
  awk -F'\t' '
    /^#/ || NF < 9 { next }
    {
      ts=$1; run=$2; cat=$7; key=$8; txt=$9
      k = cat "\034" key
      if (!((k "\034" run) in seen_run)) { seen_run[k "\034" run]=1; cnt[k]++ }
      if (ts >= lastts[k]) { lastts[k]=ts; last[k]=txt }
      catof[k]=cat
    }
    END { for (k in cnt) printf "%s\t%d\t%s\t%s\n", catof[k], cnt[k], lastts[k], last[k] }
  ' "$led" > "$cand" 2>/dev/null

  local kept=0 stale=0 capped=0
  : > "$tmp"
  local sec cat count ts text anchor line
  for sec in verify trap finding; do
    local body="$mem.sec.$$"; : > "$body"
    # Highest count first, then most recent — the ordering a human scans for a
    # systemic problem, and deterministic so tests can assert on it.
    while IFS=$'\t' read -r cat count ts text; do
      [ "$cat" = "$sec" ] || continue
      # A finding seen once is an incident, not a pattern. Promoting it would fill
      # memory with one-offs, which is exactly how this becomes a junk drawer.
      if [ "$sec" = finding ] && [ "$count" -lt 2 ]; then continue; fi
      anchor="$(anchor_of "$text")"
      # An entry anchored to a path that no longer exists is stale by construction:
      # the code it describes is gone, so the fact is at best useless and at worst
      # actively misleading. Drop it rather than wait for the cap to evict it.
      if [ -n "$anchor" ] && [ ! -e "$repo/$anchor" ]; then
        stale=$((stale+1)); continue
      fi
      if [ "$sec" = finding ]; then
        printf '%s\t- %sx: %s\n' "$count" "$count" "$text" >> "$body"
      else
        printf '%s\t- %s\n' "$count" "$text" >> "$body"
      fi
    done < <(sort -t"$TAB" -k2,2nr -k3,3r "$cand")

    if [ -s "$body" ]; then
      { section_title "$sec"; cut -f2- "$body"; echo; } >> "$tmp"
      kept=$((kept + $(wc -l < "$body" | tr -d ' ')))
    fi
    rm -f "$body"
  done
  rm -f "$cand"

  if [ ! -s "$tmp" ]; then rm -f "$tmp" "$mem"; printf '0\t%s\t0\n' "$stale"; return 0; fi

  local header="$mem.hdr.$$"
  {
    echo "# forge project memory"
    echo "<!-- Maintained by forge from what its own runs observed. Do not edit this file"
    echo "     inside a dwarf run: it is excluded from every diff and your edit would be"
    echo "     overwritten. Facts only, one per line. -->"
    echo
    cat "$tmp"
  } > "$header"

  # Cap enforcement. Everything above already dropped stale entries and unpromoted
  # findings, so what is left is real; trimming happens from the bottom, which the
  # sort above has arranged to be the lowest-count, least-recent facts.
  while [ "$(wc -l < "$header" | tr -d ' ')" -gt "$MAX_LINES" ] || \
        [ "$(wc -c < "$header" | tr -d ' ')" -gt "$MAX_BYTES" ]; do
    local last_fact
    last_fact="$(grep -n '^- ' "$header" | tail -1 | cut -d: -f1)"
    [ -n "$last_fact" ] || break
    sed "${last_fact}d" "$header" > "$header.x" && mv "$header.x" "$header"
    capped=$((capped+1)); kept=$((kept-1))
  done
  # The cap can eat a section's last fact, leaving a heading over nothing.
  drop_empty_sections "$header"

  mkdir -p "$(dir_for "$repo")"
  mv "$header" "$mem"
  rm -f "$tmp"
  printf '%s\t%s\t%s\n' "$kept" "$stale" "$capped"
}

# Remove a "## Section" line that has no "- " fact under it, which the cap can
# produce by trimming a section's last entry.
drop_empty_sections() {
  local f="$1" out="$1.de.$$"
  awk '
    { l[NR] = $0 }
    END {
      for (i = 1; i <= NR; i++) {
        if (l[i] ~ /^## /) {
          keep = 0
          for (j = i + 1; j <= NR && l[j] !~ /^## /; j++)
            if (l[j] ~ /^- /) { keep = 1; break }
          if (!keep) continue
        }
        print l[i]
      }
    }
  ' "$f" > "$out"
  # Collapse any run of blank lines to a single one.
  awk '{ if ($0 == "") blank++; else blank = 0; if (blank < 2) print }' "$out" > "$f"
  rm -f "$out"
}

# --- inject -------------------------------------------------------------------
# Slices differ by role because every injected line is paid for on every dispatch:
# QA is not building anything, so the build command is dead weight in its prompt.
do_inject() {
  local repo="${1:?inject needs a repo}" role="${2:?inject needs a role}"
  memory_off && return 0
  local mem; mem="$(memory_for "$repo")"
  [ -s "$mem" ] || return 0

  local want
  case "$role" in
    planner|dwarf) want="Verify|Known traps|Recurring QA findings" ;;
    qa)            want="Known traps|Recurring QA findings" ;;
    *) die "inject: unknown role '$role' (planner|dwarf|qa)" ;;
  esac

  local body; body="$(awk -v want="$want" '
    /^## / { keep = 0; t=$0; sub(/^## /,"",t)
             n=split(want,w,"|"); for (i=1;i<=n;i++) if (w[i]==t) keep=1
             if (keep) { print ""; print $0 } ; next }
    /^# |^<!--|^     / { next }
    keep && NF { print }
  ' "$mem")"
  [ -n "$(printf '%s' "$body" | tr -d '[:space:]')" ] || return 0

  echo "## What forge learned in this repo before"
  if [ "$role" = qa ]; then
    echo "From earlier forge runs on this project. Use it to notice a repeat bug and to"
    echo "avoid reporting a known-generated file as a defect — it is context, not a checklist."
  else
    echo "From earlier forge runs on this project. Treat it as known-good context so you do"
    echo "not rediscover it; if something here is now wrong, say so in your final message."
  fi
  printf '%s\n' "$body"
}

# --- note ---------------------------------------------------------------------
# The instruction that makes automatic memory possible at all. It asks for the
# learning in the same breath as the work, from the model that just did it — so
# extraction costs no extra dispatch and no separate model has to guess what
# mattered. Deliberately asks for nothing when nothing generalizes: a role that
# invents a learning to be helpful is precisely how this fills up with noise.
do_note() {
  local role="${1:?note needs a role}"
  memory_off && return 0
  echo "## Before you finish"
  echo "If — and only if — you learned something about THIS repository that would still be"
  echo "true and useful on an unrelated task next month, add a line for it at the very end:"
  echo
  echo "  FORGE_LEARNING: verify | <the command that actually builds or tests this project>"
  echo "  FORGE_LEARNING: trap | <a landmine: a flaky test, a generated file, a sharp edge>"
  if [ "$role" = qa ]; then
    echo "  FORGE_LEARNING: finding | <a mistake pattern likely to recur, not this one bug>"
  fi
  echo
  echo "End the text with [path/that/anchors/it] where a path makes sense; forge drops an"
  echo "entry whose path stops existing, which is how this file stays true as code moves."
  echo "One short line each, phrased as a fact. Reuse the exact wording of anything already"
  echo "listed above rather than rephrasing it, so repeats are recognised as repeats."
  echo "Most runs teach nothing durable. Emitting no line is the normal, correct outcome."
}

# --- record -------------------------------------------------------------------
do_record() {
  local repo="${1:?record needs a repo}"; shift
  local LAST="" ROLE="" RUN_ID="-" TASK="-" MODEL="-" VERDICT="-"
  while [ $# -gt 0 ]; do
    case "$1" in
      --last)    LAST="${2:?--last needs a value}"; shift 2 ;;
      --role)    ROLE="${2:?--role needs a value}"; shift 2 ;;
      --run-id)  RUN_ID="${2:?--run-id needs a value}"; shift 2 ;;
      --task)    TASK="${2:?--task needs a value}"; shift 2 ;;
      --model)   MODEL="${2:?--model needs a value}"; shift 2 ;;
      --verdict) VERDICT="${2:?--verdict needs a value}"; shift 2 ;;
      *) die "record: unknown option '$1'" ;;
    esac
  done
  memory_off && return 0
  [ -n "$ROLE" ] || die "record needs --role"
  [ -d "$repo" ] || die "record: '$repo' is not a directory"

  local led ts n=0
  led="$(ledger_for "$repo")"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$(dir_for "$repo")"
  if [ ! -f "$led" ]; then
    printf '# ts\trun_id\ttask\trole\tmodel\tverdict\tcategory\tkey\ttext\n' > "$led"
  fi

  if [ -n "$LAST" ] && [ -r "$LAST" ]; then
    local line cat text key
    while IFS= read -r line; do
      cat="$(printf '%s' "$line" | sed -n 's/^[[:space:]]*FORGE_LEARNING:[[:space:]]*\([a-zA-Z]\{1,\}\)[[:space:]]*|.*/\1/p' | tr '[:upper:]' '[:lower:]')"
      text="$(printf '%s' "$line" | sed -n 's/^[[:space:]]*FORGE_LEARNING:[[:space:]]*[a-zA-Z]\{1,\}[[:space:]]*|[[:space:]]*\(.*\)/\1/p')"
      case "$cat" in verify|trap|finding) ;; *) continue ;; esac
      text="$(clamp_fact "$(sanitize "$text")")"
      [ -n "$text" ] || continue
      key="$(norm_key "$(strip_anchor "$text")")"
      [ -n "$key" ] || continue
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$ts" "$(sanitize "$RUN_ID")" "$(sanitize "$TASK")" "$(sanitize "$ROLE")" \
        "$(sanitize "$MODEL")" "$(sanitize "$VERDICT")" "$cat" "$key" "$text" >> "$led"
      n=$((n+1))
    done < <(grep -o 'FORGE_LEARNING:.*' "$LAST" 2>/dev/null)
  fi

  # A dispatch that emitted no learning is still worth a ledger row: it is how the
  # ledger can later answer "which model has worked on this repo, and how did it do".
  if [ "$n" -eq 0 ]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ts" "$(sanitize "$RUN_ID")" "$(sanitize "$TASK")" "$(sanitize "$ROLE")" \
      "$(sanitize "$MODEL")" "$(sanitize "$VERDICT")" "-" "-" "-" >> "$led"
  fi

  local out kept stale capped
  out="$(rebuild "$repo")"
  kept="$(printf '%s' "$out" | cut -f1)"; stale="$(printf '%s' "$out" | cut -f2)"
  capped="$(printf '%s' "$out" | cut -f3)"
  printf 'memory: %s learning(s) recorded, %s fact(s) live' "$n" "${kept:-0}"
  [ "${stale:-0}" -gt 0 ] 2>/dev/null && printf ', %s stale dropped' "$stale"
  [ "${capped:-0}" -gt 0 ] 2>/dev/null && printf ', %s trimmed at cap' "$capped"
  printf '\n'
}

# --- main ---------------------------------------------------------------------
[ $# -ge 1 ] || die "usage: forge-memory.sh <inject|note|record|show|prune> [repo] [...]"
CMD="$1"; shift
case "$CMD" in
  inject) do_inject "$@" ;;
  note)   do_note "$@" ;;
  record) do_record "$@" ;;
  show)   mem="$(memory_for "${1:?show needs a repo}")"; [ -s "$mem" ] && cat "$mem" ;;
  prune)  rebuild "${1:?prune needs a repo}" >/dev/null; do_inject "${1}" dwarf ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
