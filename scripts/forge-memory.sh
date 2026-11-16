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
#                                 [--model M] [--verdict V] [--duration S]
#   forge-memory.sh show   <repo>
#   forge-memory.sh prune  <repo>
#   forge-memory.sh spend  <repo> [--run <run-id>]
#   forge-memory.sh learnings <repo> [--json]   every ledger learning and its state, as JSON
#   forge-memory.sh forget <repo> <category:key>... [--dry-run] [--json] [--actor ui|cli]
#   forge-memory.sh add <repo> --category <verify|trap|finding> --text TEXT [--path P]...
#                              [--actor ui|cli] [--json]
#   forge-memory.sh edit <repo> <category:key> --text TEXT [--path P]... [--actor ui|cli] [--json]
#
# Exit codes: 0 ok | 2 usage error
set -uo pipefail

# Where to log the Jev calls this script makes. Empty means nowhere, which is what it
# meant everywhere until a real run showed 51 logged calls and an unknown number of
# unlogged memory ones sitting beside them.
JEV_RUN_DIR=""

die()  { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }

MAX_LINES="${FORGE_MEMORY_MAX_LINES:-40}"
MAX_BYTES="${FORGE_MEMORY_MAX_BYTES:-4096}"
TAB="$(printf '\t')"

# Derived here rather than inherited: forge-parallel.sh sets SKILL_DIR but does not
# export it, and this script is also run directly. Getting it wrong would leave the Jev
# curation below silently disabled instead of visibly broken.
SKILL_DIR="${SKILL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Writing files into someone's repo automatically needs an escape hatch that does
# not require editing the skill.
memory_off() { [ "${FORGE_MEMORY:-on}" = "off" ]; }

# --- run dataset ----------------------------------------------------------------
# What curation decided, for the run dataset (scripts/forge_dataset): the final ledger
# rows say what was kept, but not that Jev refiled a category, merged a paraphrase or
# muted a fact, nor which facts a rebuild dropped and why. Fail-open and silent.
MEMORY_DROPS=""   # when set, rebuild appends "<reason>\t<category>\t<text>" rows here
memory_dataset_on() {
  case "${FORGE_DATASET:-on}" in off|OFF|Off|0|no|false) return 1 ;; esac
  return 0
}
memory_dataset_event() { # memory_dataset_event <name> <repo> <run-dir> [--set k=v | --file label=path]...
  local name="$1" repo="$2" dir="$3"; shift 3
  memory_dataset_on || return 0
  python3 "$SKILL_DIR/scripts/forge-dataset.py" event "$name" --repo "$repo" ${dir:+--dir "$dir"} "$@" \
    >/dev/null 2>&1 || true
}

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
# With <out>, the memory is written there instead of the repo's .forge/memory.md, and the repo is
# only read: `learnings` uses that to say what a rebuild keeps without changing anything.
# With <ledger>, that file is read instead of the repo's ledger. `forget --dry-run` passes a
# scratch copy that already has the rows removed, and a scratch <out>, so the repo is not touched.
rebuild() { # rebuild <repo> [<out> [<ledger>]] -> prints "kept<TAB>pruned_stale<TAB>pruned_cap"
  local repo="$1" led mem tmp cand jev_loaded=0 personf tmproles
  if [ "${FORGE_MEMORY_DEEP_PRUNE:-0}" = 1 ] && [ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ]; then
    source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && jev_loaded=1
  fi
  led="${3:-$(ledger_for "$repo")}"; mem="${2:-$(memory_for "$repo")}"
  [ -f "$led" ] || return 0
  tmp="$mem.tmp.$$"; cand="$mem.cand.$$"
  # Learnings a person wrote are kept on the first row and trimmed last. The cand
  # file itself stays the old four columns: sort's tie-break is the whole line, so an
  # extra column would reshuffle two facts that share a count and a timestamp.
  personf="$mem.person.$$"; tmproles="$mem.roles.$$"
  rm -f "$personf" "$tmproles"
  : > "$tmproles"

  # Group by (category, key). count = number of DISTINCT runs the key appeared in,
  # which is what makes a recurrence threshold meaningful: one run emitting the
  # same learning twice is still one occurrence. A row with role person, injectable
  # or not, marks the learning so the finding threshold below does not apply.
  FORGE_MEMORY_PERSON="$personf" awk -F'\t' '
    BEGIN { personfile = ENVIRON["FORGE_MEMORY_PERSON"] }
    /^#/ || NF < 9 { next }
    { if ($4 == "person") person[$7 "\034" $8] = 1 }
    # Column 11 is the injectable flag. Rows written before it existed have NF == 10
    # and are treated as injectable, so an existing ledger keeps behaving as it did.
    # Column 12 (paths) is only for edit; rebuild does not read it.
    NF >= 11 && $11 == "0" { next }
    {
      ts=$1; run=$2; cat=$7; key=$8; txt=$9
      k = cat "\034" key
      if (!((k "\034" run) in seen_run)) { seen_run[k "\034" run]=1; cnt[k]++ }
      if (ts >= lastts[k]) { lastts[k]=ts; last[k]=txt }
      catof[k]=cat
    }
    END {
      for (k in cnt) printf "%s\t%d\t%s\t%s\n", catof[k], cnt[k], lastts[k], last[k]
      for (k in cnt) if (person[k]) printf "%s\t%s\t%s\n", catof[k], lastts[k], last[k] > personfile
    }
  ' "$led" > "$cand" 2>/dev/null

  local kept=0 stale=0 capped=0
  : > "$tmp"
  local sec cat count ts text anchor line person browns
  for sec in verify trap finding; do
    local body="$mem.sec.$$"; : > "$body"
    browns="$body.roles"; : > "$browns"
    # Highest count first, then most recent — the ordering a human scans for a
    # systemic problem, and deterministic so tests can assert on it.
    while IFS=$'\t' read -r cat count ts text; do
      [ "$cat" = "$sec" ] || continue
      person=0
      if [ -s "$personf" ] && grep -F -x -q -- "$cat"$'\t'"$ts"$'\t'"$text" "$personf"; then
        person=1
      fi
      # A finding seen once is an incident, not a pattern. Promoting it would fill
      # memory with one-offs, which is exactly how this becomes a junk drawer.
      # A person's own learning is kept on its first row, in every category.
      if [ "$sec" = finding ] && [ "$count" -lt 2 ] && [ "$person" != 1 ]; then continue; fi
      anchor="$(anchor_of "$text")"
      # An entry anchored to a path that no longer exists is stale by construction:
      # the code it describes is gone, so the fact is at best useless and at worst
      # actively misleading. Drop it rather than wait for the cap to evict it.
      if [ -n "$anchor" ] && [ ! -e "$repo/$anchor" ]; then
        [ -z "$MEMORY_DROPS" ] || printf 'anchor_missing\t%s\t%s\n' "$cat" "$text" >> "$MEMORY_DROPS"
        stale=$((stale+1)); continue
      fi
      # The anchor still exists, but the fact about it may have quietly stopped being
      # true. Only on an explicit `prune`: rebuild runs after EVERY dispatch, and a
      # request per entry per dispatch would be the most expensive thing in a run.
      # An unreachable or undecided answer keeps the entry -- dropping a fact on a
      # failed request would erode memory over time with nothing recording why.
      if [ "${FORGE_MEMORY_DEEP_PRUNE:-0}" = 1 ] && [ -n "$anchor" ] \
         && [ "$jev_loaded" = 1 ] && forge_decide_active memory-stale; then
        if python3 "$SKILL_DIR/scripts/forge-jev.py" memory-stale \
             --text "$text" --anchor "$repo/$anchor" \
             ${JEV_RUN_DIR:+--run-dir "$JEV_RUN_DIR"} >/dev/null 2>&1; then
          [ -z "$MEMORY_DROPS" ] || printf 'jev_stale\t%s\t%s\n' "$cat" "$text" >> "$MEMORY_DROPS"
          stale=$((stale+1)); continue
        fi
      fi
      if [ "$sec" = finding ]; then
        printf '%s\t- %sx: %s\n' "$count" "$count" "$text" >> "$body"
      else
        printf '%s\t- %s\n' "$count" "$text" >> "$body"
      fi
      if [ "$person" = 1 ]; then printf 'p\n' >> "$browns"; else printf 'n\n' >> "$browns"; fi
    done < <(sort -t"$TAB" -k2,2nr -k3,3r "$cand")

    if [ -s "$body" ]; then
      { section_title "$sec"; cut -f2- "$body"; echo; } >> "$tmp"
      { printf '.\n'; cat "$browns"; printf '.\n'; } >> "$tmproles"
      kept=$((kept + $(wc -l < "$body" | tr -d ' ')))
    fi
    rm -f "$body" "$browns"
  done
  rm -f "$cand" "$personf"

  if [ ! -s "$tmp" ]; then rm -f "$tmp" "$tmproles" "$mem"; printf '0\t%s\t0\n' "$stale"; return 0; fi

  local header="$mem.hdr.$$" roles="$mem.caproles.$$"
  {
    echo "# forge project memory"
    echo "<!-- Maintained by forge from what its own runs observed. Do not edit this file"
    echo "     inside a dwarf run: it is excluded from every diff and your edit would be"
    echo "     overwritten. Facts only, one per line. -->"
    echo
    cat "$tmp"
  } > "$header"
  # One role per header line, in lockstep: n a run fact, p a person's fact, . the rest.
  # The five dots are the header lines above. With no person rows every fact is n, so the
  # trim below still deletes the last fact line and memory.md stays byte-identical.
  {
    printf '.\n.\n.\n.\n.\n'
    cat "$tmproles"
  } > "$roles"

  # Cap enforcement. Everything above already dropped stale entries and unpromoted
  # findings, so what is left is real. A run fact is trimmed from the bottom, which the
  # sort above has arranged to be the lowest-count, least-recent. A person's fact goes
  # only once no run fact is left.
  while [ "$(wc -l < "$header" | tr -d ' ')" -gt "$MAX_LINES" ] || \
        [ "$(wc -c < "$header" | tr -d ' ')" -gt "$MAX_BYTES" ]; do
    local last_fact
    last_fact="$(awk '
      { role[NR] = $0 }
      END {
        last_n = 0; last_any = 0
        for (i = 1; i <= NR; i++) {
          if (role[i] == "n") last_n = i
          if (role[i] == "n" || role[i] == "p") last_any = i
        }
        if (last_n) print last_n
        else if (last_any) print last_any
      }
    ' "$roles")"
    [ -n "$last_fact" ] || break
    [ -z "$MEMORY_DROPS" ] || printf 'cap\t-\t%s\n' "$(sed -n "${last_fact}p" "$header")" >> "$MEMORY_DROPS"
    sed "${last_fact}d" "$header" > "$header.x" && mv "$header.x" "$header"
    sed "${last_fact}d" "$roles" > "$roles.x" && mv "$roles.x" "$roles"
    capped=$((capped+1)); kept=$((kept-1))
  done
  # The cap can eat a section's last fact, leaving a heading over nothing.
  drop_empty_sections "$header"

  [ -n "${2:-}" ] || mkdir -p "$(dir_for "$repo")"
  mv "$header" "$mem"
  rm -f "$tmp" "$tmproles" "$roles"
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
  shift 2 2>/dev/null || true
  local TASKFILE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --task) TASKFILE="${2:-}"; shift 2 ;;
      --run-dir) JEV_RUN_DIR="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done
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

  # Narrow to the facts that bear on THIS task. The role slice and the 40-line/4KB cap
  # above remain the outer bound; this only removes lines, never adds or reorders them,
  # and any failure leaves `body` exactly as it was. Every line here is paid for on
  # every dispatch of every run, which is what makes narrowing worth a request -- and
  # also why the fallback has to be the full slice rather than an empty one.
  if [ -n "$TASKFILE" ] && [ -r "$TASKFILE" ]; then
    local jev_loaded=0
    if [ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ]; then
      source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && jev_loaded=1
    fi
    if [ "$jev_loaded" = 1 ] && forge_decide_active memory-slice; then
      local facts sliced
      facts="$(printf '%s\n' "$body" | grep '^- ' || true)"
      if [ -n "$facts" ]; then
        local ftmp="${TMPDIR:-/tmp}/forge-mem-$$.txt"
        printf '%s\n' "$facts" > "$ftmp"
        local ktmp="${TMPDIR:-/tmp}/forge-mem-keep-$$.txt"
        if python3 "$SKILL_DIR/scripts/forge-jev.py" memory-slice \
             --task "$TASKFILE" --lines "$ftmp" \
             ${JEV_RUN_DIR:+--run-dir "$JEV_RUN_DIR"} > "$ktmp" 2>/dev/null && [ -s "$ktmp" ]; then
          # Two files, not -v: awk's -v processes escapes and cannot carry a literal
          # newline, so passing the kept lines that way silently truncated the set at the
          # first one. Read them as a first file instead.
          sliced="$(printf '%s\n' "$body" | awk '
            NR == FNR { want[$0] = 1; next }
            /^## / { header = $0; printed = 0; next }
            /^- / { if ($0 in want) { if (!printed) { print ""; print header; printed = 1 } print $0 } next }
            { print }
          ' "$ktmp" -)"
          # Only adopt a narrowed body that actually kept something. An awk that fails,
          # or a slice that matched nothing, must leave the full slice in place -- an
          # empty memory section is a worse outcome than an unnarrowed one.
          if [ -n "$(printf '%s' "$sliced" | grep -c '^- ' | tr -d ' ')" ] \
             && printf '%s' "$sliced" | grep -q '^- '; then
            body="$sliced"
          fi
        fi
        rm -f "$ftmp" "$ktmp" 2>/dev/null || true
      fi
    fi
  fi

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
  if [ "$role" = qa ]; then
    # The reviewer's verdict line must stay the very last line of the reply, so a learning
    # goes just above it; asking for "the very end" produced replies the parser rejected.
    echo "true and useful on an unrelated task next month, add a line for it just above your"
    echo "final FORGE_VERDICT line (the verdict stays the last line of your reply):"
  else
    echo "true and useful on an unrelated task next month, add a line for it at the very end:"
  fi
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
  local LAST="" ROLE="" RUN_ID="-" TASK="-" MODEL="-" VERDICT="-" DURATION="-"
  while [ $# -gt 0 ]; do
    case "$1" in
      --last)    LAST="${2:?--last needs a value}"; shift 2 ;;
      --role)    ROLE="${2:?--role needs a value}"; shift 2 ;;
      --run-id)  RUN_ID="${2:?--run-id needs a value}"; shift 2 ;;
      --task)    TASK="${2:?--task needs a value}"; shift 2 ;;
      --run-dir) JEV_RUN_DIR="${2:?--run-dir needs a value}"; shift 2 ;;
      --model)   MODEL="${2:?--model needs a value}"; shift 2 ;;
      --verdict) VERDICT="${2:?--verdict needs a value}"; shift 2 ;;
      --duration) DURATION="${2:?--duration needs a value}"; shift 2 ;;
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
    printf '# ts\trun_id\ttask\trole\tmodel\tverdict\tcategory\tkey\ttext\tduration_s\tinjectable\n' > "$led"
  fi

  local jev_loaded=0 cur=""
  if [ -f "$SKILL_DIR/scripts/forge-jev-options.sh" ]; then
    source "$SKILL_DIR/scripts/forge-jev-options.sh" 2>/dev/null && jev_loaded=1
  fi
  # Scratch files for the dataset event, outside the repo: .forge/ is the user's tree.
  if memory_dataset_on; then
    cur="$(mktemp "${TMPDIR:-/tmp}/forge-memory-cur.XXXXXX" 2>/dev/null)" || cur=""
    MEMORY_DROPS="$(mktemp "${TMPDIR:-/tmp}/forge-memory-drops.XXXXXX" 2>/dev/null)" || MEMORY_DROPS=""
  fi
  if [ -n "$LAST" ] && [ -r "$LAST" ]; then
    local line cat text key inject curated curated_rc jcat jinject jmatch jkey ecat
    while IFS= read -r line; do
      cat="$(printf '%s' "$line" | sed -n 's/^[[:space:]]*FORGE_LEARNING:[[:space:]]*\([a-zA-Z]\{1,\}\)[[:space:]]*|.*/\1/p' | tr '[:upper:]' '[:lower:]')"
      text="$(printf '%s' "$line" | sed -n 's/^[[:space:]]*FORGE_LEARNING:[[:space:]]*[a-zA-Z]\{1,\}[[:space:]]*|[[:space:]]*\(.*\)/\1/p')"
      case "$cat" in verify|trap|finding) ;; *) continue ;; esac
      text="$(clamp_fact "$(sanitize "$text")")"
      [ -n "$text" ] || continue
      key="$(norm_key "$(strip_anchor "$text")")"
      [ -n "$key" ] || continue
      # Jev may refile the category, merge a paraphrase onto an existing key, and mark
      # a one-off as not worth injecting. Every one of those is a suggestion: any
      # failure, any missing key, any disabled capability leaves the three values below
      # exactly as the model emitted them. The row is written either way -- the ledger
      # is append-only and never injected, so this gates injection, never recording.
      inject=1; ecat="$cat"; jcat=""; jinject=""; jmatch=""
      if [ "$jev_loaded" = 1 ] && forge_decide_active memory-curate; then
        curated="$(python3 "$SKILL_DIR/scripts/forge-jev.py" memory-curate \
            --repo "$repo" --category "$cat" --text "$text" \
            ${JEV_RUN_DIR:+--run-dir "$JEV_RUN_DIR"} 2>/dev/null)"
        curated_rc=$?
        if [ "$curated_rc" -eq 0 ] && [ -n "$curated" ]; then
          jcat="$(printf '%s' "$curated" | cut -f1)"
          jinject="$(printf '%s' "$curated" | cut -f2)"
          jmatch="$(printf '%s' "$curated" | cut -f3)"
          case "$jcat" in verify|trap|finding) cat="$jcat" ;; esac
          [ "$jinject" = 0 ] && inject=0
          if [ -n "$jmatch" ]; then
            # Re-derive the key with THIS file's norm_key rather than having Python
            # return one: two implementations of the same normalisation would drift,
            # and a key that disagrees by one character silently stops deduplicating.
            # The leading "Nx: " is rebuild's recurrence marker, not part of the fact.
            jmatch="$(printf '%s' "$jmatch" | sed 's/^[0-9]\{1,\}x: //')"
            jkey="$(norm_key "$(strip_anchor "$jmatch")")"
            [ -n "$jkey" ] && key="$jkey"
          fi
        fi
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$ts" "$(sanitize "$RUN_ID")" "$(sanitize "$TASK")" "$(sanitize "$ROLE")" \
        "$(sanitize "$MODEL")" "$(sanitize "$VERDICT")" "$cat" "$key" "$text" \
        "$(sanitize "$DURATION")" "$inject" >> "$led"
      [ -z "$cur" ] || printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$ecat" "$cat" "$key" "$inject" \
        "$jcat" "$jinject" "$jmatch" "$text" >> "$cur"
      n=$((n+1))
    done < <(grep -o 'FORGE_LEARNING:.*' "$LAST" 2>/dev/null)
  fi

  # A dispatch that emitted no learning is still worth a ledger row: it is how the
  # ledger can later answer "which model has worked on this repo, and how did it do".
  if [ "$n" -eq 0 ]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ts" "$(sanitize "$RUN_ID")" "$(sanitize "$TASK")" "$(sanitize "$ROLE")" \
      "$(sanitize "$MODEL")" "$(sanitize "$VERDICT")" "-" "-" "-" \
      "$(sanitize "$DURATION")" >> "$led"
  fi

  local out kept stale capped
  out="$(rebuild "$repo")"
  kept="$(printf '%s' "$out" | cut -f1)"; stale="$(printf '%s' "$out" | cut -f2)"
  capped="$(printf '%s' "$out" | cut -f3)"
  memory_dataset_event memory_record "$repo" "$JEV_RUN_DIR" --set role="$ROLE" --set run_id="$RUN_ID" \
    --set task="$TASK" --set model="$MODEL" --set verdict="$VERDICT" --set recorded="$n" \
    --set kept="${kept:-0}" --set stale="${stale:-0}" --set capped="${capped:-0}" \
    --set max_lines="$MAX_LINES" --set max_bytes="$MAX_BYTES" \
    ${cur:+--file curation="$cur"} ${MEMORY_DROPS:+--file drops="$MEMORY_DROPS"} --file memory="$(memory_for "$repo")"
  rm -f ${cur:+"$cur"} ${MEMORY_DROPS:+"$MEMORY_DROPS"}
  printf 'memory: %s learning(s) recorded, %s fact(s) live' "$n" "${kept:-0}"
  [ "${stale:-0}" -gt 0 ] 2>/dev/null && printf ', %s stale dropped' "$stale"
  [ "${capped:-0}" -gt 0 ] 2>/dev/null && printf ', %s trimmed at cap' "$capped"
  printf '\n'
}

# --- prune --------------------------------------------------------------------
do_prune() { # do_prune <repo>
  local repo="$1" out
  if memory_dataset_on; then
    MEMORY_DROPS="$(mktemp "${TMPDIR:-/tmp}/forge-memory-drops.XXXXXX" 2>/dev/null)" || MEMORY_DROPS=""
  fi
  out="$(FORGE_MEMORY_DEEP_PRUNE=1 rebuild "$repo")"
  memory_dataset_event memory_prune "$repo" "" --set kept="$(printf '%s' "$out" | cut -f1)" \
    --set stale="$(printf '%s' "$out" | cut -f2)" --set capped="$(printf '%s' "$out" | cut -f3)" \
    ${MEMORY_DROPS:+--file drops="$MEMORY_DROPS"} --file memory="$(memory_for "$repo")"
  rm -f ${MEMORY_DROPS:+"$MEMORY_DROPS"}
}

# --- learnings ----------------------------------------------------------------
# Every learning in the ledger and what forge does with it, as JSON (forge-ui's Projects screen,
# #126). The states come from this file's own rebuild, run into a scratch dir: the same grouping,
# promotion threshold, staleness check and cap that `record` applies, so the listing cannot drift
# from what memory.md gets. Read-only for the repo. No Jev: deep pruning happens only on `prune`.
do_learnings() { # do_learnings <repo>
  local repo="${1:?learnings needs a repo}" scratch rc=0
  [ -d "$repo" ] || die "learnings: '$repo' is not a directory"
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/forge-memory-learnings.XXXXXX")" || die "learnings: no scratch dir" 3
  : > "$scratch/drops"
  if [ -f "$(ledger_for "$repo")" ]; then
    MEMORY_DROPS="$scratch/drops" FORGE_MEMORY_DEEP_PRUNE=0 rebuild "$repo" "$scratch/memory.md" > "$scratch/counts"
  fi
  python3 "$SKILL_DIR/scripts/forge_memory_list.py" --repo "$repo" --ledger "$(ledger_for "$repo")" \
    --memory "$(memory_for "$repo")" --rebuilt "$scratch/memory.md" --drops "$scratch/drops" \
    --max-lines "$MAX_LINES" --max-bytes "$MAX_BYTES" --memory-off "$(memory_off && echo 1 || echo 0)" || rc=$?
  rm -rf "$scratch"
  return "$rc"
}

# --- spend --------------------------------------------------------------------
# The ledger already carries one row per dispatch, and it is never injected into a
# prompt, so accounting from it costs nothing at all. Time and dispatch counts
# only: token accounting is exposed differently (or not at all) by the five CLIs,
# and a number forge cannot actually measure would be worse than no number.
do_spend() {
  local repo="${1:?spend needs a repo}"; shift
  local ONLY_RUN=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --run) ONLY_RUN="${2:?--run needs a value}"; shift 2 ;;
      *) die "spend: unknown option '$1'" ;;
    esac
  done
  local led; led="$(ledger_for "$repo")"
  [ -s "$led" ] || { echo "no ledger in $repo/.forge — nothing dispatched here yet"; return 0; }

  awk -F'\t' -v only="$ONLY_RUN" '
    function hms(x,   h,m,s) {
      if (x == "") return "     -"
      h=int(x/3600); m=int((x%3600)/60); s=x%60
      if (h>0) return sprintf("%dh%02dm", h, m)
      if (m>0) return sprintf("%dm%02ds", m, s)
      return sprintf("%ds", s)
    }
    /^#/ || NF < 9 { next }
    {
      ts=$1; run=$2; task=$3; role=$4; model=$5
      dur = (NF >= 10 ? $10 : "-")
      if (only != "" && run != only) next
      # One dispatch emitting two learnings writes two rows. Every row from one
      # dispatch shares its timestamp, so this counts dispatches, not lines.
      k = ts "\034" run "\034" task "\034" role
      if (!(k in seen)) {
        seen[k] = 1; n[model]++; rn[role]++; total++; runs[run] = 1
      }
      # Count the dispatch once, but take its duration from whichever of its rows
      # carries one — a dispatch that straddles the schema change has both kinds.
      if (dur ~ /^[0-9]+$/ && !(k in durof)) {
        durof[k] = dur
        t[model] += dur; timed[model]++
        rt[role] += dur
        tt += dur; ttn++
      }
    }
    END {
      if (total == 0) { print "no dispatches recorded" ; exit }
      nruns = 0; for (r in runs) nruns++
      printf "%d dispatch(es) across %d run(s), %s of model time\n", total, nruns, hms(tt)
      if (ttn < total)
        printf "(%d dispatch(es) predate duration recording and are counted but not timed)\n", total - ttn
      print "Durations sum concurrent work, so they exceed the wall-clock of a parallel run."
      print ""
      printf "  %-26s %6s %9s %9s\n", "by model", "disp", "total", "mean"
      for (m in n)
        printf "  %-26s %6d %9s %9s\n", m, n[m], hms(t[m]), \
               (timed[m] > 0 ? hms(int(t[m]/timed[m])) : "     -")
      print ""
      printf "  %-26s %6s %9s\n", "by role", "disp", "total"
      for (r in rn)
        printf "  %-26s %6d %9s\n", r, rn[r], hms(rt[r])
    }
  ' "$led"
}

# --- forget -------------------------------------------------------------------
# Drop every ledger row of the given category:key learnings and rebuild memory.md.
# A learning is the (category, key) group `rebuild` and `learnings` use. Rows have no id.
# FORGE_MEMORY=off stops record, note and inject from writing: those run unasked. show and
# learnings still read the ledger (learnings only reports memory_off). forget is an explicit
# edit of that same ledger, so it runs when memory is off too. There is no early return.
#
# Sets FORGET_CANON to the ledger's "category:key" form (norm_key on each part), or exits 2.
parse_forget_key() {
  local raw="$1" cat key
  case "$raw" in
    *:*) ;;
    *) die "forget: KEY must be category:key, got '$raw'" ;;
  esac
  cat="$(norm_key "${raw%%:*}")"
  key="$(norm_key "${raw#*:}")"
  [ -n "$cat" ] && [ -n "$key" ] || die "forget: KEY must be category:key with neither part empty, got '$raw'"
  FORGET_CANON="$cat:$key"
}

# Rewrite the ledger without the requested keys. Kept lines stay byte-identical, including
# a missing final newline. Prints how many rows matched. Counts are "<key>\t<rows>\t<1|0>".
forget_filter() { # forget_filter <ledger> <keys-file> <out-ledger> <counts-file>
  python3 - "$@" <<'PY'
import sys
ledger_path, keys_path, out_path, counts_path = sys.argv[1:5]
keys = [line.rstrip('\n') for line in open(keys_path, encoding='utf-8') if line.rstrip('\n')]
seen = {}
for key in keys:
    cat, sep, rest = key.partition(':')
    if sep:
        seen.setdefault((cat, rest), 0)
with open(ledger_path, 'rb') as handle:
    data = handle.read()
kept = []
for line in data.splitlines(keepends=True):
    raw = line.rstrip(b'\n').rstrip(b'\r')
    if raw.startswith(b'#') or raw.count(b'\t') < 8:
        kept.append(line)
        continue
    cols = raw.split(b'\t')
    if len(cols) < 9:
        kept.append(line)
        continue
    pair = (cols[6].decode('utf-8', 'surrogateescape'), cols[7].decode('utf-8', 'surrogateescape'))
    if pair in seen:
        seen[pair] += 1
        continue
    kept.append(line)
with open(out_path, 'wb') as handle:
    handle.write(b''.join(kept))
with open(counts_path, 'w', encoding='utf-8') as handle:
    for key in keys:
        cat, _, rest = key.partition(':')
        n = seen.get((cat, rest), 0)
        handle.write('%s\t%d\t%s\n' % (key, n, '1' if n else '0'))
sys.stdout.write('%d\n' % sum(seen.values()))
PY
}

# Fact lines (`- ...`) present in <before> and absent from <after>, in the before file's
# order. A section heading disappears with its last fact; it is formatting, and so are the
# header and blank lines. Callers match on the `- ` lines. A missing file counts as empty.
forget_removed() { # forget_removed <before> <after> <out>
  python3 - "$@" <<'PY'
import sys
from collections import Counter
before_path, after_path, out_path = sys.argv[1:4]

def lines(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []

have = Counter(lines(after_path))
removed = []
for line in lines(before_path):
    if not line.startswith('- '):
        continue
    if have[line]:
        have[line] -= 1
    else:
        removed.append(line)
with open(out_path, 'w', encoding='utf-8') as handle:
    for line in removed:
        handle.write(line + '\n')
PY
}

forget_report() { # forget_report <json 0|1> <dry 0|1> <counts> <removed>
  if [ "$1" = 1 ]; then
    python3 - "$2" "$3" "$4" <<'PY'
import json, sys
dry, counts_path, removed_path = sys.argv[1:4]
keys = []
for line in open(counts_path, encoding='utf-8'):
    line = line.rstrip('\n')
    if not line:
        continue
    key, rows, found = line.split('\t')
    keys.append({"key": key, "rows": int(rows), "found": found == "1"})
removed = []
try:
    for line in open(removed_path, encoding='utf-8'):
        line = line.rstrip('\n')
        if line:
            removed.append(line)
except FileNotFoundError:
    pass
json.dump({"ok": True, "dry_run": dry == "1", "keys": keys, "memory_lines_removed": removed},
          sys.stdout)
sys.stdout.write('\n')
PY
  else
    local key rows found
    while IFS=$'\t' read -r key rows found; do
      [ -n "$key" ] || continue
      if [ "$found" = 1 ]; then
        printf '%s\t%s\n' "$key" "$rows"
      else
        printf '%s\tnot found\n' "$key"
      fi
    done < "$3"
  fi
}

forget_fail() { # forget_fail <scratch> <message>
  [ -n "$1" ] && rm -rf "$1"
  die "$2"
}

do_forget() { # do_forget <repo> <category:key>... [--dry-run] [--json] [--actor ui|cli]
  local repo="" dry=0 as_json=0 actor="cli" raw c keylist="" total=0 status=0 tmp="" scratch="" already=0
  local -a rawkeys=() canon=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) memory_usage; exit 0 ;;
      --dry-run) dry=1; shift ;;
      --json) as_json=1; shift ;;
      --actor)
        [ $# -ge 2 ] || die "forget: --actor needs ui or cli"
        actor="$2"; shift 2
        case "$actor" in
          ui|cli) ;;
          *) die "forget: --actor must be ui or cli" ;;
        esac
        ;;
      --) shift
        while [ $# -gt 0 ]; do rawkeys+=("$1"); shift; done
        ;;
      -*) die "forget: unknown option '$1'" ;;
      *)
        if [ -z "$repo" ]; then repo="$1"; else rawkeys+=("$1"); fi
        shift
        ;;
    esac
  done
  [ -n "$repo" ] || die "forget: usage: forget <repo> <category:key>... [--dry-run] [--json] [--actor ui|cli]"
  [ "${#rawkeys[@]}" -ge 1 ] || die "forget: usage: forget <repo> <category:key>... [--dry-run] [--json] [--actor ui|cli]"
  [ -d "$repo" ] || die "forget: '$repo' is not a directory"
  # One learning once. A repeated KEY, or two spellings that norm_key collapses together,
  # is a single entry in the report and in the dataset event's keys list. rows counts
  # ledger rows, so listing the key twice must not count them twice either.
  for raw in "${rawkeys[@]}"; do
    parse_forget_key "$raw"
    already=0
    if [ "${#canon[@]}" -gt 0 ]; then
      for c in "${canon[@]}"; do
        [ "$c" = "$FORGET_CANON" ] && already=1 && break
      done
    fi
    [ "$already" = 1 ] || canon+=("$FORGET_CANON")
  done
  keylist="${canon[0]}"
  if [ "${#canon[@]}" -gt 1 ]; then
    for c in "${canon[@]:1}"; do keylist="$keylist,$c"; done
  fi

  scratch="$(mktemp -d "${TMPDIR:-/tmp}/forge-memory-forget.XXXXXX")" || die "forget: no scratch dir"
  printf '%s\n' "${canon[@]}" > "$scratch/keys"
  local led mem
  led="$(ledger_for "$repo")"
  mem="$(memory_for "$repo")"
  if [ -f "$led" ]; then
    total="$(forget_filter "$led" "$scratch/keys" "$scratch/ledger.new" "$scratch/counts")" \
      || forget_fail "$scratch" "forget: could not read '$led'"
  else
    total=0
    : > "$scratch/counts"
    for c in "${canon[@]}"; do printf '%s\t0\t0\n' "$c" >> "$scratch/counts"; done
  fi
  case "$total" in
    ''|*[!0-9]*) forget_fail "$scratch" "forget: could not read '$led'" ;;
  esac

  : > "$scratch/removed"
  if [ "$total" -gt 0 ]; then
    # Lines that leave are the difference between two rebuilds of the ledger, not between
    # whatever memory.md happens to be on disk. A missing file would otherwise report
    # nothing, and a stale or hand-edited one would report edits this forget did not make.
    # The before rebuild reads the full ledger and writes only the scratch <out>.
    rebuild "$repo" "$scratch/before.md" "$led" >/dev/null \
      || forget_fail "$scratch" "forget: could not preview memory"
    if [ "$dry" = 1 ]; then
      # Same rebuild `record` runs, against the reduced ledger, written outside the repo.
      rebuild "$repo" "$scratch/after.md" "$scratch/ledger.new" >/dev/null \
        || forget_fail "$scratch" "forget: could not preview memory"
      forget_removed "$scratch/before.md" "$scratch/after.md" "$scratch/removed" \
        || forget_fail "$scratch" "forget: could not compare memory"
    else
      # do_record appends with >> and takes no lock, so there is none to share. The new
      # ledger is written beside the old one and renamed over it, which is atomic on one
      # filesystem. A concurrent append that lands between this read and that rename is
      # the remaining race: that row is lost.
      tmp="$(mktemp "$(dir_for "$repo")/ledger.tsv.tmp.XXXXXX")" \
        || forget_fail "$scratch" "forget: could not write the ledger"
      # mktemp makes it 0600: cp -p first gives it the ledger's own mode, which the write keeps.
      { cp -p "$led" "$tmp" && cat "$scratch/ledger.new" > "$tmp"; } \
        || { rm -f "$tmp"; forget_fail "$scratch" "forget: could not write the ledger"; }
      mv -f "$tmp" "$led" \
        || { rm -f "$tmp"; forget_fail "$scratch" "forget: could not replace the ledger"; }
      rebuild "$repo" >/dev/null || forget_fail "$scratch" "forget: could not rebuild memory"
      forget_removed "$scratch/before.md" "$mem" "$scratch/removed" \
        || forget_fail "$scratch" "forget: could not compare memory"
      # Only after a rewrite and a rebuild. A miss (rows 0) changes nothing, so it records nothing.
      memory_dataset_event memory_forget "$repo" "" --set "keys=$keylist" --set "rows=$total" --set "actor=$actor"
    fi
  fi

  forget_report "$as_json" "$dry" "$scratch/counts" "$scratch/removed" || status=$?
  rm -rf "$scratch"
  return "$status"
}

# --- add / edit ---------------------------------------------------------------
# A person writes one ledger row (role person, run id manual-<epoch>) and rebuild runs.
# Column 12 is paths, comma-joined, empty when there are none. Rows of 11 columns still
# read as before; only edit reads column 12. Refusals die before either file is written.

# Text is one line, 1..300 characters, with no tab. Checked before sanitize, which is what
# do_record runs next. Dies with one forge: line.
refuse_memory_text() { # refuse_memory_text <cmd> <text>
  local cmd="$1" text="$2" n cleaned
  case "$text" in
    *$'\n'*|*$'\t'*) die "$cmd: text must be one line without a tab" ;;
  esac
  n=${#text}
  if [ "$n" -lt 1 ] || [ "$n" -gt 300 ]; then
    die "$cmd: text must be 1 to 300 characters"
  fi
  cleaned="$(sanitize "$text")"
  [ -n "$cleaned" ] || die "$cmd: text must be 1 to 300 characters"
}

# A path stored in column 12 or used as an anchor: relative, no .. component, at most
# 300 characters, and none of the characters that would break the column or the line.
check_memory_path() { # check_memory_path <cmd> <path>
  local cmd="$1" p="$2" rest part
  [ -n "$p" ] || die "$cmd: path is empty"
  [ "${#p}" -le 300 ] || die "$cmd: path is longer than 300 characters"
  case "$p" in
    *$'\n'*|*$'\t'*|*,*) die "$cmd: path must not contain a tab, newline or comma" ;;
    /*) die "$cmd: path must be relative" ;;
  esac
  rest="$p"
  while [ -n "$rest" ]; do
    part="${rest%%/*}"
    if [ "$part" = ".." ]; then die "$cmd: path must not contain '..'"; fi
    case "$rest" in
      */*) rest="${rest#*/}" ;;
      *) rest="" ;;
    esac
  done
}

# First-seen order. A path that cannot live in a comma-joined column is dropped; new
# --path values are refused earlier, so this only filters an old anchor that has one.
# Sets MEM_PATHS (csv) and MEM_FIRST (the anchor to append, or empty).
collect_paths() {
  local p f
  MEM_PATHS=""
  MEM_FIRST=""
  [ "$#" -gt 0 ] || return 0
  f="$(mktemp "${TMPDIR:-/tmp}/forge-memory-paths.XXXXXX")" || die "memory: no scratch file"
  : > "$f"
  for p in "$@"; do
    [ -n "$p" ] || continue
    case "$p" in
      *$'\n'*|*$'\t'*|*,*) continue ;;
    esac
    if grep -F -x -q -- "$p" "$f"; then continue; fi
    printf '%s\n' "$p" >> "$f"
    [ -n "$MEM_FIRST" ] || MEM_FIRST="$p"
  done
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ -n "$MEM_PATHS" ]; then MEM_PATHS="$MEM_PATHS,$p"; else MEM_PATHS="$p"; fi
  done < "$f"
  rm -f "$f"
}

# Sanitize, append the first path as [anchor] when the text has none, then clamp and
# key the same way do_record does. Sets MEM_TEXT and MEM_KEY.
prepare_memory_text() { # prepare_memory_text <cmd> <text> <first-path>
  local cmd="$1" text="$2" first="$3"
  refuse_memory_text "$cmd" "$text"
  text="$(sanitize "$text")"
  if [ -n "$first" ] && [ -z "$(anchor_of "$text")" ]; then
    text="$text [$first]"
  fi
  MEM_TEXT="$(clamp_fact "$text")"
  MEM_KEY="$(norm_key "$(strip_anchor "$MEM_TEXT")")"
  [ -n "$MEM_KEY" ] || die "$cmd: text has nothing to keep"
}

# One person row. The header matches do_record when the file is missing or empty;
# an existing header is left as it is. The data row always has the 12th column.
write_person_row() { # write_person_row <ledger> <category> <paths-csv>
  local led="$1" cat="$2" paths="$3" ts run_id
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_id="manual-$(date +%s)"
  mkdir -p "$(dirname "$led")"
  if [ ! -s "$led" ]; then
    printf '# ts\trun_id\ttask\trole\tmodel\tverdict\tcategory\tkey\ttext\tduration_s\tinjectable\n' > "$led"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$ts" "$run_id" "-" "person" "-" "-" "$cat" "$MEM_KEY" "$MEM_TEXT" "0" "1" "$paths" >> "$led"
}

ensure_trailing_newline() {
  python3 - "$1" <<'PY'
import sys
path = sys.argv[1]
with open(path, 'rb') as handle:
    data = handle.read()
if data and not data.endswith(b'\n'):
    with open(path, 'ab') as handle:
        handle.write(b'\n')
PY
}

# Paths of one learning, in ledger order: each row's column 12, then that row's anchor.
# Prints how many rows matched. The category:key is the form parse_forget_key stores.
edit_old_paths() { # edit_old_paths <ledger> <canon> <out>
  python3 - "$@" <<'PY'
import re
import sys
ledger_path, canon, out_path = sys.argv[1:4]
cat, _, key = canon.partition(':')
anchor = re.compile(r'\[([^\[\]]+)\]\s*$')
found = 0
paths = []
with open(ledger_path, 'rb') as handle:
    data = handle.read()
for line in data.splitlines():
    if line.startswith(b'#') or line.count(b'\t') < 8:
        continue
    cols = line.split(b'\t')
    if len(cols) < 9:
        continue
    if cols[6].decode('utf-8', 'surrogateescape') != cat:
        continue
    if cols[7].decode('utf-8', 'surrogateescape') != key:
        continue
    found += 1
    if len(cols) >= 12 and cols[11]:
        for part in cols[11].decode('utf-8', 'surrogateescape').split(','):
            if part:
                paths.append(part)
    text = cols[8].decode('utf-8', 'surrogateescape')
    match = anchor.search(text)
    if match:
        paths.append(match.group(1))
with open(out_path, 'wb') as handle:
    for path in paths:
        handle.write(path.encode('utf-8', 'surrogateescape') + b'\n')
sys.stdout.write('%d\n' % found)
PY
}

# `- ` lines gained and lost between two rebuilds. forget_removed is the multiset
# difference in the first file's order; swapping the files is the lines that were added.
memory_line_changes() { # memory_line_changes <before> <after> <added-out> <removed-out>
  forget_removed "$1" "$2" "$4" || return 1
  forget_removed "$2" "$1" "$3" || return 1
}

memory_change_report() { # memory_change_report <json 0|1> <added|edited> <category:key> <added> <removed>
  if [ "$1" = 1 ]; then
    python3 - "$3" "$4" "$5" <<'PY'
import json
import sys
key, added_path, removed_path = sys.argv[1:4]

def load(path):
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
    except FileNotFoundError:
        return []
    return [line for line in lines if line]

json.dump({"ok": True, "key": key, "memory_lines_added": load(added_path),
           "memory_lines_removed": load(removed_path)}, sys.stdout)
sys.stdout.write('\n')
PY
  else
    printf '%s %s\n' "$2" "$3"
  fi
}

do_add() { # do_add <repo> --category C --text TEXT [--path P]... [--actor ui|cli] [--json]
  local repo="" cat="" text="" actor="cli" as_json=0 have_text=0 scratch="" led mem status=0 p
  local -a paths=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) memory_usage; exit 0 ;;
      --json) as_json=1; shift ;;
      --category)
        [ $# -ge 2 ] || die "add: --category needs verify, trap or finding"
        cat="$2"; shift 2
        case "$cat" in
          verify|trap|finding) ;;
          *) die "add: --category must be verify, trap or finding" ;;
        esac
        ;;
      --text)
        [ $# -ge 2 ] || die "add: --text needs a value"
        text="$2"; have_text=1; shift 2
        ;;
      --path)
        [ $# -ge 2 ] || die "add: --path needs a value"
        paths+=("$2"); shift 2
        ;;
      --actor)
        [ $# -ge 2 ] || die "add: --actor needs ui or cli"
        actor="$2"; shift 2
        case "$actor" in
          ui|cli) ;;
          *) die "add: --actor must be ui or cli" ;;
        esac
        ;;
      --) shift; break ;;
      -*) die "add: unknown option '$1'" ;;
      *)
        [ -z "$repo" ] || die "add: unexpected argument '$1'"
        repo="$1"; shift
        ;;
    esac
  done
  while [ $# -gt 0 ]; do
    [ -z "$repo" ] || die "add: unexpected argument '$1'"
    repo="$1"; shift
  done
  [ -n "$repo" ] && [ -n "$cat" ] && [ "$have_text" = 1 ] \
    || die "add: usage: add <repo> --category verify|trap|finding --text TEXT [--path P]... [--actor ui|cli] [--json]"
  [ -d "$repo" ] || die "add: '$repo' is not a directory"
  [ "${#paths[@]}" -le 20 ] || die "add: at most 20 paths"
  if [ "${#paths[@]}" -gt 0 ]; then
    for p in "${paths[@]}"; do check_memory_path add "$p"; done
    collect_paths "${paths[@]}"
  else
    MEM_PATHS=""
    MEM_FIRST=""
  fi
  # Text, paths and the key are settled before any file in the repo is created.
  prepare_memory_text add "$text" "$MEM_FIRST"
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/forge-memory-add.XXXXXX")" || die "add: no scratch dir"
  led="$(ledger_for "$repo")"
  mem="$(memory_for "$repo")"
  if [ -f "$led" ]; then
    rebuild "$repo" "$scratch/before.md" "$led" >/dev/null \
      || forget_fail "$scratch" "add: could not preview memory"
  fi
  mkdir -p "$(dir_for "$repo")"
  write_person_row "$led" "$cat" "$MEM_PATHS"
  rebuild "$repo" >/dev/null || forget_fail "$scratch" "add: could not rebuild memory"
  memory_line_changes "$scratch/before.md" "$mem" "$scratch/added" "$scratch/removed" \
    || forget_fail "$scratch" "add: could not compare memory"
  memory_dataset_event memory_add "$repo" "" --set "key=$cat:$MEM_KEY" --set "actor=$actor"
  memory_change_report "$as_json" added "$cat:$MEM_KEY" "$scratch/added" "$scratch/removed" || status=$?
  rm -rf "$scratch"
  return "$status"
}

do_edit() { # do_edit <repo> <category:key> --text TEXT [--path P]... [--actor ui|cli] [--json]
  local repo="" rawkey="" text="" actor="cli" as_json=0 have_text=0
  local scratch="" led mem status=0 p count cat total tmp
  local -a paths=() union=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) memory_usage; exit 0 ;;
      --json) as_json=1; shift ;;
      --text)
        [ $# -ge 2 ] || die "edit: --text needs a value"
        text="$2"; have_text=1; shift 2
        ;;
      --path)
        [ $# -ge 2 ] || die "edit: --path needs a value"
        paths+=("$2"); shift 2
        ;;
      --actor)
        [ $# -ge 2 ] || die "edit: --actor needs ui or cli"
        actor="$2"; shift 2
        case "$actor" in
          ui|cli) ;;
          *) die "edit: --actor must be ui or cli" ;;
        esac
        ;;
      --) shift; break ;;
      -*) die "edit: unknown option '$1'" ;;
      *)
        if [ -z "$repo" ]; then repo="$1"
        elif [ -z "$rawkey" ]; then rawkey="$1"
        else die "edit: unexpected argument '$1'"
        fi
        shift
        ;;
    esac
  done
  while [ $# -gt 0 ]; do
    if [ -z "$repo" ]; then repo="$1"
    elif [ -z "$rawkey" ]; then rawkey="$1"
    else die "edit: unexpected argument '$1'"
    fi
    shift
  done
  [ -n "$repo" ] && [ -n "$rawkey" ] && [ "$have_text" = 1 ] \
    || die "edit: usage: edit <repo> <category:key> --text TEXT [--path P]... [--actor ui|cli] [--json]"
  [ -d "$repo" ] || die "edit: '$repo' is not a directory"
  [ "${#paths[@]}" -le 20 ] || die "edit: at most 20 paths"
  if [ "${#paths[@]}" -gt 0 ]; then
    for p in "${paths[@]}"; do check_memory_path edit "$p"; done
  fi
  # Same parser forget uses, so Trap:Foo_Bar is the ledger's trap:foo bar.
  parse_forget_key "$rawkey"
  refuse_memory_text edit "$text"
  led="$(ledger_for "$repo")"
  [ -f "$led" ] || die "edit: no ledger row for '$FORGET_CANON'"
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/forge-memory-edit.XXXXXX")" || die "edit: no scratch dir"
  count="$(edit_old_paths "$led" "$FORGET_CANON" "$scratch/oldpaths")" \
    || forget_fail "$scratch" "edit: could not read '$led'"
  case "$count" in
    ''|*[!0-9]*) forget_fail "$scratch" "edit: could not read '$led'" ;;
    0) forget_fail "$scratch" "edit: no ledger row for '$FORGET_CANON'" ;;
  esac
  if [ -s "$scratch/oldpaths" ]; then
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      union+=("$p")
    done < "$scratch/oldpaths"
  fi
  if [ "${#paths[@]}" -gt 0 ]; then
    for p in "${paths[@]}"; do union+=("$p"); done
  fi
  if [ "${#union[@]}" -gt 0 ]; then
    collect_paths "${union[@]}"
  else
    MEM_PATHS=""
    MEM_FIRST=""
  fi
  prepare_memory_text edit "$text" "$MEM_FIRST"
  # The category is the one in the key. edit has no way to file the text elsewhere.
  cat="${FORGET_CANON%%:*}"
  mem="$(memory_for "$repo")"
  rebuild "$repo" "$scratch/before.md" "$led" >/dev/null \
    || forget_fail "$scratch" "edit: could not preview memory"
  printf '%s\n' "$FORGET_CANON" > "$scratch/keys"
  total="$(forget_filter "$led" "$scratch/keys" "$scratch/ledger.new" "$scratch/counts")" \
    || forget_fail "$scratch" "edit: could not read '$led'"
  case "$total" in
    ''|*[!0-9]*) forget_fail "$scratch" "edit: could not read '$led'" ;;
    0) forget_fail "$scratch" "edit: no ledger row for '$FORGET_CANON'" ;;
  esac
  # One rewrite of the ledger, then one rebuild of memory.md. The preview above
  # writes only the scratch copy used to name the lines that changed.
  ensure_trailing_newline "$scratch/ledger.new" \
    || forget_fail "$scratch" "edit: could not write the ledger"
  write_person_row "$scratch/ledger.new" "$cat" "$MEM_PATHS"
  tmp="$(mktemp "$(dir_for "$repo")/ledger.tsv.tmp.XXXXXX")" \
    || forget_fail "$scratch" "edit: could not write the ledger"
  { cp -p "$led" "$tmp" && cat "$scratch/ledger.new" > "$tmp"; } \
    || { rm -f "$tmp"; forget_fail "$scratch" "edit: could not write the ledger"; }
  mv -f "$tmp" "$led" \
    || { rm -f "$tmp"; forget_fail "$scratch" "edit: could not replace the ledger"; }
  rebuild "$repo" >/dev/null || forget_fail "$scratch" "edit: could not rebuild memory"
  memory_line_changes "$scratch/before.md" "$mem" "$scratch/added" "$scratch/removed" \
    || forget_fail "$scratch" "edit: could not compare memory"
  memory_dataset_event memory_edit "$repo" "" --set "key=$cat:$MEM_KEY" --set "actor=$actor"
  memory_change_report "$as_json" edited "$cat:$MEM_KEY" "$scratch/added" "$scratch/removed" || status=$?
  rm -rf "$scratch"
  return "$status"
}

# --- main ---------------------------------------------------------------------
memory_usage() {
  cat <<'USAGE'
usage: forge memory <command> <repo> [...]   (scripts/forge-memory.sh)

  show <repo>                 the repository's .forge/memory.md
  learnings <repo> [--json]   every ledger learning and what forge does with it (in_memory, waiting,
                              stale, capped, muted), from forge's own rebuild; changes nothing
  prune <repo>                re-apply staleness and the cap now, then print what a dwarf is told
  spend <repo> [--run ID]     what forge has cost this repository, from the ledger
  inject <repo> <role>        the block forge prepends to a planner, dwarf or qa prompt
  forget <repo> <cat:key>...  remove those learnings from the ledger and rebuild memory.md
                              (--dry-run, --json, --actor ui|cli); a missing key is not an error
  add <repo> --category C --text T
                              append one learning (C is verify, trap or finding) and rebuild;
                              a finding is kept at once (--path P, repeatable; --actor ui|cli; --json)
  edit <repo> <cat:key> --text T
                              replace that learning and rebuild once, keeping its paths
                              (--path P, --actor ui|cli, --json); an unknown key is an error
USAGE
}
[ $# -ge 1 ] || die "usage: forge-memory.sh <inject|note|record|show|prune|spend|learnings|forget|add|edit> [repo] [...]"
CMD="$1"; shift
case "$CMD" in
  -h|--help|help) memory_usage; exit 0 ;;
  inject) do_inject "$@" ;;
  note)   do_note "$@" ;;
  record) do_record "$@" ;;
  show)   mem="$(memory_for "${1:?show needs a repo}")"; [ -s "$mem" ] && cat "$mem" ;;
  spend)  do_spend "$@" ;;
  learnings) [ "${2:-}" = --json ] || [ $# -eq 1 ] || die "learnings: usage: learnings <repo> [--json]"; do_learnings "$1" ;;
  prune)  do_prune "${1:?prune needs a repo}" >/dev/null
          do_inject "${1}" dwarf ;;
  forget) do_forget "$@" ;;
  add)    do_add "$@" ;;
  edit)   do_edit "$@" ;;
  *) die "unknown subcommand '$CMD'" ;;
esac
