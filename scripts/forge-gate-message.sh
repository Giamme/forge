#!/usr/bin/env bash
# The dwarf-message gate (#44): the implementer's final message is read before a review is paid
# for. Sourced by forge-lib-gates.sh; forge_post_dwarf_gates calls it right after the dwarf's
# reply is in, before the diff snapshot and its empty-diff exits.
#
# `question` stops the task as NEEDS_INPUT for the user's answer; `blocked` and `unfinished`
# send the attempt back as FAIL, with the implementer's own words as the retry's findings, and no
# review. Neither writes qa.gate or dispatches QA. It fails open (a gate that is off or in
# shadow, no key, no answer, any error all return 0), and it is the only way an attempt that
# changed nothing can fail on what its dwarf said.

forge_gate_message() { # forge_gate_message <plan> <id> <tdir> <wt> <attempt>
  local plan="$1" id="$2" tdir="$3" wt="$4" attempt="$5"
  declare -F forge_decide_active >/dev/null || return 0
  forge_decide_active dwarf-message || return 0

  local row rc=0 which p said
  row="$(python3 "$SKILL_DIR/scripts/forge-jev.py" dwarf-message \
    --plan "$plan" --task "$id" --attempt "$attempt" --repo "$REPO" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$row" ] || return 0
  IFS=$'\t' read -r which p <<< "$row"
  # the last 3000 characters, not bytes: a byte cut can split a UTF-8 character, and sed then
  # fails on the illegal byte sequence (invalid input bytes become U+FFFD)
  said="$(python3 -c '
import sys
text = open(sys.argv[1], "rb").read().decode("utf-8", "replace")[-3000:]
sys.stdout.buffer.write(text.encode("utf-8"))' "$tdir/dwarf.last" 2>/dev/null)"

  case "$which" in
    question)
      {
        echo "The implementer asked:"
        echo
        printf '%s\n' "$said"
      } > "$tdir/.needs_input.md.tmp" && mv "$tdir/.needs_input.md.tmp" "$tdir/needs_input.md" || return 0
      rm -f "$tdir/qa.last"            # an earlier review describes other code
      : > "$tdir/noretry"
      echo NEEDS_INPUT > "$tdir/status"
      note "$id: the implementer asked a question only you can answer — stopped (see $tdir/needs_input.md)"
      return 1
      ;;
    blocked|unfinished)
      {
        echo "forge: the implementer's final message says it did not finish ($which, $p); its own words:"
        echo
        printf '%s\n' "$said" | sed 's/^/> /'
      } > "$tdir/.message.md.tmp" && mv "$tdir/.message.md.tmp" "$tdir/message.md" || return 0
      : > "$tdir/message.failed"
      rm -f "$tdir/qa.last"
      echo FAIL > "$tdir/status"
      note "$id: the implementer says it did not finish ($which) — sent back without a review (see $tdir/message.md)"
      return 1
      ;;
  esac
  return 0
}
