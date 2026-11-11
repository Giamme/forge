#!/usr/bin/env python3
"""Retro notes for one plan. Report and issues are later subcommands of this file.

  record PLAN --role ROLE --dir DISPATCH [--task ID] [--attempt N] [--inbox PATH]
      Parse the dispatch's FORGE_RETRO lines (and, for a dwarf, its inbox), check them
      against the log, and store them.
  note PLAN --type T --fixable-by F --summary S [--command C --exit N --error E]
       [--blocking] [--tried TEXT]
      Record one orchestrator note. It is always claimed.

Exit codes: 0 stored (or nothing to store), 2 usage, 3 PLAN is not a directory.
Prints each stored note as one JSON line.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_retro  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge retro',
        description='Notes on what got in a run\'s way. Kept on this machine, never in a prompt.')
    sub = parser.add_subparsers(dest='command', required=True)
    record = sub.add_parser('record', help='parse a dispatch and store its retro notes')
    record.add_argument('plan', help='the plan directory')
    record.add_argument('--role', required=True, help='the dispatch role, e.g. dwarf or qa')
    record.add_argument('--dir', required=True, dest='dispatch_dir',
                        help='the directory that holds <role>.last and <role>.log')
    record.add_argument('--task', help='task id; also writes tasks/<id>/retro.json')
    record.add_argument('--attempt', type=int, help='the attempt number')
    record.add_argument('--inbox', help="the dwarf worktree's .forge/retro-inbox.jsonl")
    note = sub.add_parser('note', help='record an orchestrator note (always claimed)')
    note.add_argument('plan', help='the plan directory')
    note.add_argument('--type', required=True, choices=forge_retro.TYPES)
    note.add_argument('--fixable-by', required=True, dest='fixable_by', choices=forge_retro.FIXABLE)
    note.add_argument('--summary', required=True)
    note.add_argument('--command', dest='command_text', help='the command that failed')
    note.add_argument('--exit', dest='exit_code', type=int, help='that command\'s exit code')
    note.add_argument('--error', help='the error text, kept up to 2 KB')
    note.add_argument('--blocking', action='store_true', help='the run could not finish without this')
    note.add_argument('--tried', help='what was tried before giving up')
    return parser


def _emit(notes: list) -> None:
    if not notes:
        return
    blob = ''.join(json.dumps(note, ensure_ascii=False, sort_keys=True) + '\n' for note in notes)
    sys.stdout.buffer.write(blob.encode('utf-8'))
    sys.stdout.buffer.flush()


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'record':
            notes = forge_retro.record(args.plan, args.role, args.dispatch_dir, task=args.task,
                                       attempt=args.attempt, inbox=args.inbox)
        else:
            notes = forge_retro.note(args.plan, type=args.type, fixable_by=args.fixable_by,
                                     summary=args.summary, command=args.command_text, exit=args.exit_code,
                                     error=args.error, blocking=args.blocking, tried=args.tried)
    except FileNotFoundError as error:
        sys.stderr.write('forge retro: not a plan dir: %s\n' % error)
        return 3
    _emit(notes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
