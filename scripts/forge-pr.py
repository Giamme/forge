#!/usr/bin/env python3
"""forge pr [RUN_DIR | --repo DIR] [--base BRANCH] [--head BRANCH] [--title T] [--texts] [--paths] [--out FILE] [--create]

A draft pull-request description for a finished forge run, written from what the run recorded: the goal,
which tasks merged and which did not, who implemented and reviewed each, the verification status, the
final review and its open findings, and the integration branch. It is printed on stdout (a suggested
title goes to stderr), so it can be read, edited or piped: `forge pr | gh pr create --draft --body-file -`.

With no RUN_DIR it describes the newest run of the current repo (the Git top level, or --repo) that is
not still running. A run still running is refused: describe it once it ends.

What could be private stays out, as in `forge ui export`: the request, the prompts and the deferrals
(--texts adds the request and the deferrals) and local paths, which are reduced to their last
component (--paths keeps them). Finding headlines, task titles and the goal are kept, so read the
body before it goes anywhere public.

`--create` is the only thing here that does more than print: it runs `gh pr create --draft` in the repo
with that body, on the run's integration branch (forge/<run id>-integration, or --head). It never
pushes: the branch must already have an upstream (git push -u origin <branch>), and gh must be signed in.
Without --create nothing is written and neither gh nor the network is used (--out FILE writes the body
to a file). Exit status: 0 done, 1 gh failed, 2 usage or nothing to describe.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

import forge_registry  # noqa: E402
from forge_ui import reader  # noqa: E402

DONE = {'MERGED', 'PASS', 'NOCHANGES'}
SETTLED = DONE | {'FAIL', 'ERROR', 'CONFLICT', 'INFRA', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'UNKNOWN', 'BLOCKED'}
MAX_FINDINGS = 20
TITLE_MAX = 72


def load_runs():
    """forge-runs.py's repo lookup and registry reader, so every command agrees on which runs a repo has."""
    spec = importlib.util.spec_from_file_location('forge_runs', SKILL / 'scripts' / 'forge-runs.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('forge_runs', module)
    spec.loader.exec_module(module)
    return module


def unfinished(summary: dict) -> list:
    """Task statuses that mean the run is still going (a failed task is finished, and reported as failed)."""
    return sorted({t['status'] for t in summary.get('task_statuses') or [] if t['status'] not in SETTLED})


def newest_finished(repo: Path, runs) -> Path | None:
    best = None
    for path, source in reader.discover(plans=runs.registered(forge_registry.runs_registry(repo)), registry=False):
        try:
            summary = reader.summarize(path, source)
        except Exception:
            continue
        if summary.get('kind') != 'plan' or not summary.get('task_count') or unfinished(summary):
            continue
        if best is None or (summary['updated'] or 0) > best[0]:
            best = (summary['updated'] or 0, path)
    return best[1] if best else None


class Scrub:
    """Local paths reduced to their last component (the run, the repo) or `~` (the home), as the export does."""

    def __init__(self, keep: bool, *roots):
        self.keep = keep
        self.roots = sorted({str(r) for r in roots if r and str(r) not in ('', '/')}, key=len, reverse=True)
        home = str(Path.home())
        self.home = home if home not in ('', '/') else None

    def __call__(self, text):
        text = '' if text is None else str(text)
        if self.keep:
            return text
        for root in self.roots:
            text = text.replace(root, Path(root).name or root)
        if self.home:
            text = text.replace(self.home, '~')
        return text


def cell(text) -> str:
    return str(text if text not in (None, '') else '–').replace('|', '\\|').replace('\n', ' ').strip()


def title_of(detail: dict, scrub) -> str:
    goal = scrub((detail.get('goal') or '').strip().splitlines()[0] if (detail.get('goal') or '').strip() else '')
    if not goal:
        return 'forge run %s' % (detail.get('run_id') or detail['key'])
    return goal if len(goal) <= TITLE_MAX else goal[:TITLE_MAX - 1].rstrip() + '…'


def headline(item: dict) -> str:
    """A finding's first line without its `[P1][CONFIRMED] path:line —` lead, which the list shows itself."""
    text = re.sub(r'^\s*(?:[-*]\s*)?(?:\[[^\]]*\]\s*)*', '', item['summary'])
    if item.get('location') and text.startswith(item['location']):
        text = text[len(item['location']):]
    return re.sub(r'^[\s:—–-]+', '', text).strip() or item['summary']


def fenced(text: str) -> list:
    fence = '`' * max(3, max((len(m) for m in re.findall(r'`+', text)), default=0) + 1)
    return [fence + 'text', text, fence]


def finding_lines(path: Path, source: str, scrub) -> list:
    """Open findings of the newest final review, worst first: severity, location, headline (never the text)."""
    try:
        found = reader.run_findings(path, source) or {}
    except Exception:
        return []
    finals = found.get('final_reviews') or []
    if not finals:
        return []
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3, None: 4}
    items = sorted(finals[0]['findings'], key=lambda f: order.get(f['severity'], 4))
    lines = []
    for item in items[:MAX_FINDINGS]:
        where = ' `%s`' % scrub(item['location']) if item.get('location') else ''
        label = item['severity'] or item['status'].lower()
        lines.append('- **%s**%s %s' % (label, where, scrub(headline(item))))
    if len(items) > MAX_FINDINGS:
        lines.append('- … and %d more (see the run in `forge ui`)' % (len(items) - MAX_FINDINGS))
    return lines


def integration_branch(detail: dict) -> str | None:
    return 'forge/%s-integration' % detail['run_id'] if detail.get('run_id') else None


def build(path: Path, source: str, scrub, texts: bool) -> tuple[str, str]:
    """(title, body) for a run."""
    d = reader.detail(path, source)
    rid = d.get('run_id') or d['key']
    tasks = d['tasks']
    merged = [t for t in tasks if t['status'] in DONE]
    other = [t for t in tasks if t['status'] not in DONE]
    out = []
    goal = scrub((d.get('goal') or '').strip()) or 'Untitled run'
    out += ['## Summary', '', goal, '',
            'Built by forge run `%s`: %d of %d tasks merged.' % (scrub(rid), len(merged), len(tasks)), '']
    out += ['## Tasks', '', '| Task | Title | Difficulty | Status | Implementer | Reviewer |', '|---|---|---|---|---|---|']
    for t in tasks:
        out.append('| %s |' % ' | '.join(cell(scrub(v)) for v in (t['id'], t['title'], t['difficulty'], t['status'], t['dwarf'], t['qa'])))
    out.append('')
    if other:
        out += ['## Not merged', '']
        out += ['- `%s` %s: %s' % (scrub(t['id']), cell(scrub(t['title'])), t['status']) for t in other]
        out += ['', 'These tasks are not part of the integration branch.', '']
    verification = d.get('verification_status')
    review = d.get('final_review')
    out += ['## Checks', '', '- Verification: %s' % (verification or 'not recorded')]
    if review:
        note = ''
        if review.get('outdated'):
            later = ', '.join(scrub(i) for i in review.get('merged_since') or [])
            note = ' (outdated: the integration branch has moved since%s)' % (f'; merged after it: {later}' if later else '')
        out.append('- Final review: %s%s' % (review.get('verdict') or 'no verdict', note))
    else:
        out.append('- Final review: none recorded')
    out.append('')
    found = finding_lines(path, source, scrub)
    if found:
        out += ['## Open findings from the final review', ''] + found + ['']
    integration = d.get('integration') or {}
    branch = integration_branch(d)
    out += ['## Branch', '', '- Integration branch: `%s`' % branch]
    if integration.get('base'):
        out.append('- Forked from: `%s`' % integration['base'][:12])
    out.append('')
    if texts:
        if (d.get('request') or '').strip():
            out += ['## Request', ''] + fenced(scrub(d['request'].strip())) + ['']
        if d.get('deferrals'):
            out += ['## Deferred', ''] + ['- %s' % scrub(x) for x in d['deferrals']] + ['']
    out += ['---', '_Draft written by `forge pr` from forge run `%s`. Read it before it goes public._' % scrub(rid), '']
    return title_of(d, scrub), '\n'.join(out)


def create(repo: Path, head: str, title: str, body: str, base: str | None) -> int:
    """`gh pr create --draft` for a branch that is already pushed. Pushes nothing."""
    local = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', 'refs/heads/' + head],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode == 0
    if local:
        upstream = subprocess.run(['git', '-C', str(repo), 'for-each-ref', '--format=%(upstream)', 'refs/heads/' + head],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
        if not upstream:
            print(f'forge pr: {head} has not been pushed; forge pr never pushes. Run: git -C {repo} push -u origin {head}', file=sys.stderr)
            return 2
    elif ':' not in head:
        print(f'forge pr: no branch {head} in {repo}; name the branch with --head', file=sys.stderr)
        return 2
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as handle:
        handle.write(body)
    try:
        argv = ['gh', 'pr', 'create', '--draft', '--title', title, '--body-file', handle.name, '--head', head]
        if base:
            argv += ['--base', base]
        try:
            return 1 if subprocess.run(argv, cwd=repo, stdin=subprocess.DEVNULL).returncode else 0
        except FileNotFoundError:
            print('forge pr: --create needs the gh CLI (https://cli.github.com), signed in', file=sys.stderr)
            return 2
    finally:
        os.unlink(handle.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge pr', description='A draft pull-request description for a finished forge run. Prints it; '
                                     'opens a draft PR with gh only when --create is given.')
    parser.add_argument('run_dir', nargs='?', type=Path, metavar='RUN_DIR', help='the run (default: the newest finished run of the current repo)')
    parser.add_argument('--repo', type=Path, metavar='DIR', help='the repo whose newest finished run to describe (default: the Git top level of the current directory)')
    parser.add_argument('--title', help='the title (default: the goal, first line)')
    parser.add_argument('--base', metavar='BRANCH', help="with --create: the branch to merge into (default: gh's default branch)")
    parser.add_argument('--head', metavar='BRANCH', help="with --create: the branch to propose (default: the run's forge/<run id>-integration)")
    parser.add_argument('--texts', action='store_true', help='also include the run\'s request and its deferrals')
    parser.add_argument('--paths', action='store_true', help='keep local paths (default: their last component)')
    parser.add_argument('--out', type=Path, metavar='FILE', help='also write the body to FILE')
    parser.add_argument('--create', action='store_true', help='run `gh pr create --draft` with this body (never pushes)')
    args = parser.parse_args(argv)
    if args.run_dir is not None and args.repo is not None:
        parser.error('RUN_DIR cannot be combined with --repo')
    if args.repo is not None and not args.repo.is_dir():
        parser.error(f'--repo: no such directory: {args.repo}')
    if (args.base or args.head) and not args.create:
        parser.error('--base and --head are used by --create only')
    runs = load_runs()
    if args.run_dir is not None:
        path = args.run_dir.resolve()
    else:
        repo = runs.repo_of(args.repo if args.repo is not None else Path.cwd())
        path = newest_finished(repo, runs)
        if path is None:
            print(f'forge pr: no finished run is registered for {repo} (forge runs lists them)', file=sys.stderr)
            return 2
    if reader._kind(path) != 'plan':
        what = 'a solo run (no tasks or integration branch to describe)' if reader._kind(path) == 'solo' else 'not a forge run (no tasks.tsv)'
        print(f'forge pr: {path} is {what}', file=sys.stderr)
        return 2
    summary = reader.summarize(path, 'plan')
    if not summary.get('task_count'):
        print(f'forge pr: {path} has no tasks yet', file=sys.stderr)
        return 2
    if unfinished(summary):
        print(f"forge pr: run {summary.get('run_id') or path.name} is still running ({', '.join(unfinished(summary)).lower()} tasks); describe it once it ends", file=sys.stderr)
        return 2
    repo = Path(summary['repo']) if summary.get('repo') else None
    title, body = build(path, 'plan', Scrub(args.paths, path, repo), args.texts)
    title = args.title or title
    if args.out is not None:
        args.out.write_text(body, encoding='utf-8')
    if args.create:
        if repo is None or not repo.is_dir():
            print('forge pr: the run records no repo that exists here; --create needs it', file=sys.stderr)
            return 2
        return create(repo, args.head or integration_branch(summary), title, body, args.base)
    sys.stdout.write(body)
    print(f'forge pr: suggested title: {title}\nforge pr: nothing was created; add --create to open a draft PR with gh', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
