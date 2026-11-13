#!/usr/bin/env python3
"""Brief mode: each task's open questions, asked before the task starts.

  write PLAN [--task ID] [--jobs N] [--force]
      One read-only side dispatch per task that has no brief.json (earliest projected
      start first), stored as tasks/<id>/brief.json and brief.parked.json.
  show PLAN [--task ID] [--json]
      Every question with its state, flagged open questions first.
  answer PLAN TASK QID TEXT [--actor A] [--file F]
      Record an answer. Before the task starts it goes to brief.answers.json; after,
      to brief.late.json. A late answer prints the tasks.tsv row and the prompt.md
      a follow-up needs, then the `forge control add` line that queues that row.
      --file F reads the text (- is stdin).
  stale PLAN MERGED-TASK
      Flag open questions of dependents that have not started, when the merge changed
      a file outside the task's declared files. A declared directory covers the paths
      under it.

  propose PLAN TASK QID [--actor A]
      Turn the answer to a parked wave question into a proposal: one read-only dispatch asks
      the brief model what it changes in the question's group, and the diff is stored under
      PLAN/proposals/<pid>/. A change outside the group, to a started member or to any field
      but prompt, approach and files makes the whole proposal `refused`. Nothing else changes.
  proposals PLAN [--json]
      Every proposal with its state, scope, group, summary, diff and the exact apply command.
  apply PLAN PID --approve HASH [--actor A]
      Write the proposal's diff, re-validate and re-freeze the groups. Refuses, changing
      nothing, when the hash differs, the proposal is not `proposed`, a target started or a
      target file changed since. A `files` change is refused while a run is live.
  reject PLAN PID [--reason TEXT] [--actor A]
      Mark the proposal rejected; no plan file is touched.

Exit codes: 0 ok, 2 usage or an unknown task, question or proposal (the message lists the
valid ids), 3 PLAN is not a plan dir, brief is off (write, show, propose), or a proposal is
refused (propose, apply, reject).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_brief  # noqa: E402
import forge_brief.answers as answers  # noqa: E402
import forge_brief.proposals as proposals  # noqa: E402

REASONS = {
    'env': 'FORGE_BRIEF=off is set',
    'off': 'brief mode is off for this plan (plan --brief advisory|gated|strict)',
    'never-send': "the repository's privacy is never-send",
    'no-model': 'no brief model (plan --brief-model SPEC, or forge config set brief.model SPEC)',
    'cap': 'the brief-change dispatch cap for this run is reached',
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
    answer = sub.add_parser('answer', help='record an answer to one question')
    answer.add_argument('plan', help='the plan directory')
    answer.add_argument('task', help='the task the question belongs to')
    answer.add_argument('qid', help='the question id, such as q1')
    answer.add_argument('text', nargs='?', default=None, help='the answer (or pass --file)')
    answer.add_argument('--actor', default='cli', help='who is answering (default cli; forge-ui passes ui)')
    answer.add_argument('--file', default=None, help='read the answer from this file, or - for stdin')
    stale = sub.add_parser('stale', help="flag dependents' open questions after a merge outside its files")
    stale.add_argument('plan', help='the plan directory')
    stale.add_argument('task', help='the task that merged')
    propose = sub.add_parser('propose', help='turn the answer to a parked wave question into a proposal')
    propose.add_argument('plan', help='the plan directory')
    propose.add_argument('task', help='the task the question belongs to')
    propose.add_argument('qid', help='the parked question id, such as q2')
    propose.add_argument('--actor', default='cli', help='who is proposing (default cli)')
    listed = sub.add_parser('proposals', help='every proposal with its diff and apply command')
    listed.add_argument('plan', help='the plan directory')
    listed.add_argument('--json', action='store_true', dest='as_json', help='machine-readable output')
    apply = sub.add_parser('apply', help='write an approved proposal')
    apply.add_argument('plan', help='the plan directory')
    apply.add_argument('pid', help='the proposal id, such as p1')
    apply.add_argument('--approve', required=True, metavar='HASH', help="the proposal's hash, as `proposals` shows it")
    apply.add_argument('--actor', default='cli', help='who is approving (default cli; forge-ui passes ui)')
    reject = sub.add_parser('reject', help='reject a proposal; no plan file is touched')
    reject.add_argument('plan', help='the plan directory')
    reject.add_argument('pid', help='the proposal id, such as p1')
    reject.add_argument('--reason', default=None, help='why')
    reject.add_argument('--actor', default='cli', help='who is rejecting (default cli; forge-ui passes ui)')
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


def _ids(ids) -> str:
    return ', '.join(ids) if ids else '(none)'


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'write' and args.jobs < 1:
        sys.stderr.write('forge brief write: --jobs must be at least 1\n')
        return 2
    try:
        if args.command == 'write':
            forge_brief.write(args.plan, task=args.task, jobs=args.jobs, force=args.force, progress=_say)
            return 0
        if args.command == 'answer':
            result = answers.answer(args.plan, args.task, args.qid, text=args.text,
                                    actor=args.actor, file=args.file)
            sys.stdout.write(result['message'])
            return 0
        if args.command == 'stale':
            for task, qid in answers.stale(args.plan, args.task):
                sys.stdout.write('%s/%s\n' % (task, qid))
            return 0
        if args.command == 'propose':
            record = proposals.propose(args.plan, args.task, args.qid, actor=args.actor)
            item = next(i for i in proposals.listing(args.plan) if i['pid'] == record['pid'])
            sys.stdout.write(proposals.render_one(item))
            if record['state'] == 'refused':
                sys.stderr.write('forge brief: proposal %s is refused: %s\n' % (record['pid'], record['reason']))
                return 3
            return 0
        if args.command == 'proposals':
            if not Path(args.plan).is_dir():
                raise forge_brief.NotAPlan(args.plan)
            items = proposals.listing(args.plan)
            if args.as_json:
                sys.stdout.write(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
            else:
                sys.stdout.write(proposals.render(items))
            return 0
        if args.command == 'apply':
            result = proposals.apply(args.plan, args.pid, args.approve, actor=args.actor)
            sys.stdout.write('%s applied: %s\n' % (result['pid'], ', '.join(result['files']) or 'nothing'))
            if result['warning']:
                sys.stderr.write('forge brief: %s\n' % result['warning'])
            return 0
        if args.command == 'reject':
            record = proposals.reject(args.plan, args.pid, actor=args.actor, reason=args.reason)
            sys.stdout.write('%s rejected\n' % record['pid'])
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
    except answers.NoSuchTask as error:
        sys.stderr.write('forge brief: no such task: %s\nvalid tasks: %s\n' % (error.task, _ids(error.valid)))
        return 2
    except answers.NoSuchQuestion as error:
        sys.stderr.write('forge brief: no such question: %s\nvalid questions for %s: %s\n'
                         % (error.qid, error.task, _ids(error.valid)))
        return 2
    except answers.Usage as error:
        sys.stderr.write('forge brief: %s\n' % error)
        return 2
    except proposals.NoSuchProposal as error:
        sys.stderr.write('forge brief: no such proposal: %s\nvalid proposals: %s\n' % (error.pid, _ids(error.valid)))
        return 2
    except proposals.Refused as error:
        sys.stderr.write('forge brief: refused: %s\n' % error.reason)
        return 3
    if args.as_json:
        sys.stdout.write(json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    else:
        sys.stdout.write(forge_brief.render(found, args.plan))
    return 0


if __name__ == '__main__':
    sys.exit(main())
