#!/usr/bin/env python3
"""groups.tsv for the shell: parse, validate, derive from waves, resolve QA, freeze.

  validate PLAN [--max-tasks N]   check groups.tsv against tasks.tsv
  derive PLAN --review task|group [--max-tasks N]
                                  write groups.tsv, one group per computed wave
  resolve PLAN                    write back the resolved qa of review groups
  freeze PLAN                     write groups.frozen (removed when there are no groups)
  check PLAN                      exit 0 when groups.frozen still matches
  of PLAN TASK                    gid<TAB>review<TAB>qa of the task's group, or nothing
  show PLAN [--json]              the groups

Exit codes: 0 ok, 2 a GroupError or usage, with the message on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_groups  # noqa: E402


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('forge-groups: %s\n' % message)
        raise SystemExit(2)


def main(argv=None) -> int:
    parser = Parser(prog='forge-groups', description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    for name in ('validate', 'derive', 'resolve', 'freeze', 'check', 'of', 'show'):
        p = sub.add_parser(name)
        p.add_argument('plan')
        if name == 'of':
            p.add_argument('task')
        if name in ('validate', 'derive'):
            p.add_argument('--max-tasks', type=int, default=6)
        if name == 'derive':
            p.add_argument('--review', choices=forge_groups.REVIEWS, required=True)
        if name == 'show':
            p.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    plan = Path(args.plan)
    try:
        if not plan.is_dir():
            raise forge_groups.GroupError('%s is not a plan directory' % args.plan)
        return run(args, plan)
    except forge_groups.GroupError as error:
        sys.stderr.write('forge-groups: %s\n' % ' '.join(str(error).split()))
        return 2


def run(args, plan: Path) -> int:
    fg = forge_groups
    if args.command == 'validate':
        fg.validate(plan, fg.load(plan), args.max_tasks)
    elif args.command == 'derive':
        fg.write(plan, fg.derive(plan, args.review, args.max_tasks))
    elif args.command == 'resolve':
        # No groups.tsv means nothing to write back. write() of an empty list
        # would create a header-only file that was not there.
        if (plan / 'groups.tsv').is_file():
            fg.write(plan, fg.resolve_qa(plan, fg.load(plan)))
    elif args.command == 'freeze':
        fg.freeze(plan)
    elif args.command == 'check':
        reason = fg.check_frozen(plan)
        if reason:
            raise fg.GroupError(reason)
    elif args.command == 'of':
        g = fg.group_of(plan, args.task)
        if g:
            print('%s\t%s\t%s' % (g.id, g.review, g.qa))
    elif args.command == 'show':
        groups = fg.load(plan)
        if args.json:
            print(json.dumps([dict(g._asdict(), members=list(g.members)) for g in groups], indent=1))
        else:
            for g in groups:
                print('\t'.join([g.id, g.review, g.qa, ','.join(g.members), g.title, g.goal]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
