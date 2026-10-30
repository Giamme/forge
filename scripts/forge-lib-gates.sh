#!/usr/bin/env bash
# Where run-time gates plug into do_task_body. Sourced by forge-parallel.sh next to forge-lib-task.sh.
#
# Each hook calls its gate only when the gate is defined, so a forge without the gate files runs
# exactly as before. A gate returns 1 when it sent the attempt back (it has written the status and
# the notes); the hook passes that on and the call site ends the attempt. Any other failure of a
# gate (exit 2 and up) counts as "did not act": gates fail open.

forge_gates_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for forge_gate_file in forge-gate-message.sh forge-gate-addressed.sh; do
  [ -f "$forge_gates_dir/$forge_gate_file" ] && source "$forge_gates_dir/$forge_gate_file"
done
unset forge_gate_file

# After the dwarf's reply is in, before the diff snapshot and its empty-diff exits: a dwarf that
# only asks a question usually changes nothing, and must still reach the gate.
forge_post_dwarf_gates() { # forge_post_dwarf_gates <plan> <id> <tdir> <wt> <attempt>
  declare -F forge_gate_message >/dev/null || return 0
  local rc=0
  forge_gate_message "$@" || rc=$?
  [ "$rc" -eq 1 ] && return 1
  return 0
}

# After reviewed.commit and reviewed.dwarf.* are written for a fresh dwarf, before QA is dispatched.
forge_pre_qa_gates() { # forge_pre_qa_gates <plan> <id> <tdir> <wt> <attempt> <reviewed>
  declare -F forge_gate_addressed >/dev/null || return 0
  local rc=0
  forge_gate_addressed "$@" || rc=$?
  [ "$rc" -eq 1 ] && return 1
  return 0
}
