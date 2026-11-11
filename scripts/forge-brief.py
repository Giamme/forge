#!/usr/bin/env python3
"""Brief mode: each task's open questions, asked before the task starts.

  write PLAN [--task ID] [--jobs N] [--force]
      One read-only side dispatch per task that has no brief.json (earliest projected
      start first), stored as tasks/<id>/brief.json and brief.parked.json.
  show PLAN [--task ID] [--json]
      Every question with its state, flagged open questions first.

Exit codes: 0 ok, 2 usage, 3 PLAN is not a plan dir or brief is off for it (with the reason).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_brief  # noqa: E402

REASONS = {
    'env': 'FORGE_BRIEF=off is set',
    'off': 'brief mode is off for this plan (plan --brief advisory|gated|strict)',
    'never-send': "the repository's privacy is never-send",
    'no-model': 'no brief model (plan --brief-model SPEC, or forge config set brief.model SPEC)',
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge brief',
        description="Each task's open questions, asked before the task starts.")
    sub = parser.add_subparsers(dest='command', required=True)
    write = sub.add_parser('write', help='write the questions of the tasks that have none yet')
    write.add_argument('plan', help='the plan directory')
    write.add_argument('--task', help='only this task')
    write.add_argument('--jobs', type=int, default=forge_brief.DEFAULT_JOBS,
                       help='dispatches at once (default %(default)s)')
    write.add_argument('--force', action='store_true', help='redo a task that already has a brief.json')
    show = sub.add_parser('show', help='every question with its state, flagged first')
    show.add_argument('plan', help='the plan directory')
    show.add_argument('--task', help='only this task')
    show.add_argument('--json', action='store_true', dest='as_json', help='machine-readable output')
    return parser


def _say(result: dict) -> None:
    if result['status'] == 'written':
        detail = '%d question(s)' % result['questions']
        if result['parked']:
            detail += ', %d parked' % result['parked']
    elif result['status'] == 'error':
        detail = 'writer failed (%s); no questions' % result['reason']
    else:
        detail = 'skipped (%s)' % result['reason']
    sys.stdout.write('%s: %s\n' % (result['task'], detail))
    sys.stdout.flush()


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'write' and args.jobs < 1:
        sys.stderr.write('forge brief write: --jobs must be at least 1\n')
        return 2
    try:
        if args.command == 'write':
            forge_brief.write(args.plan, task=args.task, jobs=args.jobs, force=args.force, progress=_say)
            return 0
        found = forge_brief.collect(args.plan, task=args.task)
    except forge_brief.NotAPlan as error:
        sys.stderr.write('forge brief: not a plan dir: %s\n' % error)
        return 3
    except forge_brief.BriefOff as error:
        sys.stderr.write('forge brief: %s\n' % REASONS.get(error.reason, error.reason))
        return 3
    except forge_brief.UnknownTask as error:
        sys.stderr.write('forge brief: no such task: %s\n' % error)
        return 2
    if args.as_json:
        sys.stdout.write(json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    else:
        sys.stdout.write(forge_brief.render(found))
    return 0


if __name__ == '__main__':
    sys.exit(main())
