"""`forge dataset outcomes`: what happened to merged work after it landed, read from git.

Every other outcome in the store is a reviewer's opinion. This one is not: once
`integrate` fast-forwards a branch, the lines each task added either stay in the code or
get rewritten or reverted. That is observable later, from the repo's history alone.

`integrate` merges the run branch with --no-ff and fast-forwards the user's branch to that
merge, never squashing, so every attempt commit lands as-is and `git blame` still
attributes a task's surviving lines to its own commits (base_ref..reviewed_commit).
For each `integrate_advanced` event this records, per task it brought in, how many of the
lines the task added are still attributed to the task's commits at a horizon (14 days by
default) after the integration, and whether any later commit reverted it.

Survival describes the artifact that landed (with every retry it took), not the first
model that was routed to the task: the exporter attaches it to the review of that diff.

Read-only by construction: the store is opened read-only (connect() would migrate it),
and git runs only object-store queries (rev-parse, rev-list, log, diff between commits,
blame at a commit, cat-file) with GIT_OPTIONAL_LOCKS=0, so nothing touches a working
tree or an index. Results are written with record(), which refuses a store inside the
repo. A horizon still in the future, or a query that failed, writes nothing and is tried
again next time; the rid names the integration, the reviewed commit and the horizon, so
a repeat harvest adds nothing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import dataset_dir, record, sha256

HORIZON_DAYS = 14
GIT_TIMEOUT_S = 120
_REVERTS = re.compile(r'This reverts commit ([0-9a-f]{7,40})')
_HUNK = re.compile(rb'^([0-9a-f]{40}) \d+ \d+ (\d+)$')


class GitError(Exception):
    pass


class Git:
    """Object-store queries in one repo. A failure raises; it never reads as "nothing found"."""

    def __init__(self, repo: str):
        self.repo = repo
        self.env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', LC_ALL='C')

    def out(self, *args, ok=(0,)) -> tuple[int, bytes]:
        try:
            result = subprocess.run(['git', '-C', self.repo, *args], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, env=self.env)
        except (OSError, subprocess.SubprocessError) as error:
            raise GitError('%s: %s' % (args[0], error)) from error
        if result.returncode not in ok:
            raise GitError('%s exited %d' % (args[0], result.returncode))
        return result.returncode, result.stdout

    def text(self, *args) -> str:
        return self.out(*args)[1].decode('utf-8', 'replace')

    def exists(self, obj: str) -> bool:
        return self.out('cat-file', '-e', obj, ok=(0, 1, 128))[0] == 0

    def blamed(self, rev: str, path: str, commits: set) -> int:
        """Lines of `path` at `rev` whose origin is one of `commits`, streamed, never capped."""
        try:
            proc = subprocess.Popen(['git', '-C', self.repo, 'blame', '--incremental', rev, '--', path],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        except OSError as error:
            raise GitError('blame: %s' % error) from error
        count = 0
        try:
            for line in proc.stdout:
                match = _HUNK.match(line.rstrip(b'\n'))
                if match and match.group(1).decode() in commits:
                    count += int(match.group(2))
            if proc.wait(timeout=GIT_TIMEOUT_S):
                raise GitError('blame %s exited %d' % (path, proc.returncode))
        except subprocess.TimeoutExpired as error:
            proc.kill()
            raise GitError('blame %s timed out' % path) from error
        finally:
            proc.stdout.close()
        return count


def read_store():
    """Integrations, merges and attempts, from a read-only connection; None without a store."""
    path = dataset_dir() / 'dataset.sqlite'
    if not path.exists():
        return None
    db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)
    try:
        rows = db.execute("SELECT kind, run, task, repo, ts, data FROM events WHERE kind IN ('event', 'attempt')"
                          ).fetchall()
        repos = dict(db.execute('SELECT repo, path FROM repos'))
        have = {rid for (rid,) in db.execute("SELECT rid FROM events WHERE kind = 'outcome'")}
    finally:
        db.close()
    integrations, merges, bases = [], {}, {}
    for kind, run, task, repo, ts, data in rows:
        d = json.loads(data)
        if kind == 'attempt':
            if d.get('reviewed_commit') and d.get('base_ref'):
                bases[d['reviewed_commit']] = d['base_ref']
        elif d.get('name') == 'integrate_advanced' and d.get('candidate') and d.get('parent'):
            integrations.append(dict(run=run, repo=repo, ts=ts, **{k: d.get(k) for k in
                                                                   ('branch', 'parent', 'candidate', 'target')}))
        elif d.get('name') == 'merge' and d.get('status') == 'MERGED' and task:
            commit = d.get('commit') or d.get('reviewed_commit')
            if commit:
                # The last MERGED event for a commit wins; split/combine copies keep their run.
                merges[(repo, commit)] = dict(run=run, task=task, commit=commit, integration=d.get('integration'))
    return dict(integrations=sorted(integrations, key=lambda i: i['ts']), merges=merges, bases=bases,
                repos=repos, have=have)


def outcome_rid(candidate: str, reviewed: str, horizon_days: int) -> str:
    return 'outcome:survival:' + sha256(('%s:%s:%d' % (candidate, reviewed, horizon_days)).encode())[:24]


def horizon_rev(git: Git, branch: str, candidate: str, cutoff: float, now: float):
    """('ok', rev, ts) | ('lost', None, None) | ('pending', None, None).

    The branch is resolved once to an immutable tip. The candidate must sit on its
    first-parent chain (a rebase, squash or reset loses it: unobservable, not failure).
    The horizon is found by position along that chain, so out-of-order commit dates
    cannot jump past a commit that came later.
    """
    if not branch or git.out('rev-parse', '--verify', '-q', branch + '^{commit}', ok=(0, 1))[0]:
        return 'lost', None, None
    tip = git.text('rev-parse', branch + '^{commit}').strip()
    chain = []                            # first-parent commits after the candidate, newest first
    for line in git.text('log', '--first-parent', '--format=%H %ct %P', '%s..%s' % (candidate, tip)).splitlines():
        sha, ct, *parents = line.split()
        chain.append((sha, int(ct), parents))
    if tip != candidate and (not chain or chain[-1][2][:1] != [candidate]):
        return 'lost', None, None         # the walk never passed through the candidate
    chain = [(sha, ct) for sha, ct, _ in reversed(chain)]
    candidate_ct = int(git.text('log', '-1', '--format=%ct', candidate).strip())
    rev, rev_ts = candidate, candidate_ct
    for sha, ct in chain:
        if ct > cutoff:
            return 'ok', rev, rev_ts
        rev, rev_ts = sha, ct
    if now < cutoff:
        return 'pending', None, None      # more commits may still land before the horizon
    return 'ok', rev, rev_ts


def survival(git: Git, base: str, reviewed: str, rev: str) -> dict:
    commits = set(git.text('rev-list', '%s..%s' % (base, reviewed)).split())
    files, added_total, surviving_total = {}, 0, 0
    for entry in git.text('diff', '--numstat', '-z', '--no-renames', base, reviewed).split('\0'):
        cells = entry.lstrip('\n').split('\t', 2)
        if len(cells) != 3 or cells[0] == '-':
            continue                       # binary: no lines to follow
        added, path = int(cells[0]), cells[2]
        if added == 0:
            continue
        kept = git.blamed(rev, path, commits) if git.exists('%s:%s' % (rev, path)) else 0
        kept = min(kept, added)
        files[path] = [added, kept]
        added_total += added
        surviving_total += kept
    return dict(commits=sorted(commits), files=files, added=added_total, surviving=surviving_total)


def reverts(git: Git, candidate: str, rev: str, shas: set) -> list:
    if rev == candidate:
        return []
    out = []
    for block in git.text('log', '--format=%H%x00%B%x01', '%s..%s' % (candidate, rev)).split('\x01'):
        sha, _, body = block.strip('\n').partition('\x00')
        if any(any(s.startswith(m) for s in shas) for m in _REVERTS.findall(body)):
            out.append(sha)
    return out


def harvest(*, horizon_days: int = HORIZON_DAYS, repo: str | None = None, include_temp: bool = False,
            now: float | None = None) -> tuple[list, Counter]:
    from .importer import is_temp
    state = read_store()
    counts = Counter()
    if state is None:
        return [], counts
    now = time.time() if now is None else now
    wanted = str(Path(repo).resolve()) if repo else None
    by_repo = defaultdict(list)
    for integration in state['integrations']:
        path = state['repos'].get(integration['repo'])
        if not path or (wanted and str(Path(path).resolve()) != wanted):
            continue
        if is_temp(path) and not include_temp:
            counts['skipped_temp'] += 1
            continue
        if not Path(path).is_dir():
            counts['repo_missing'] += 1
            continue
        by_repo[path].append(integration)
    out = []
    for path, integrations in by_repo.items():
        git = Git(path)
        for integration in integrations:
            candidate, parent = integration['candidate'], integration['parent']
            try:
                if not git.exists(candidate + '^{commit}'):
                    counts['candidate_missing'] += 1
                    continue
                landed = set(git.text('rev-list', candidate, '^' + parent).split())
                tasks = [m for (r, c), m in state['merges'].items() if r == integration['repo'] and c in landed]
                todo = [m for m in tasks if outcome_rid(candidate, m['commit'], horizon_days) not in state['have']]
                counts['already'] += len(tasks) - len(todo)
                if not todo:
                    continue
                cutoff = integration['ts'] + horizon_days * 86400
                status, rev, rev_ts = horizon_rev(git, integration['branch'], candidate, cutoff, now)
            except GitError:
                counts['git_error'] += 1
                continue
            if status == 'pending':
                counts['pending'] += len(todo)
                continue
            for merge in todo:
                base = state['bases'].get(merge['commit'])
                if not base:
                    counts['no_base'] += 1
                    continue
                data = dict(name='survival', candidate=candidate, reviewed=merge['commit'], base_ref=base,
                            branch=integration['branch'], horizon_days=horizon_days, integrated_ts=integration['ts'],
                            horizon_rev=rev, horizon_ts=rev_ts, integrate_run=integration['run'])
                try:
                    if status == 'ok':
                        found = survival(git, base, merge['commit'], rev)
                        task_shas = set(found.pop('commits')) | {s for s in (merge.get('integration'), candidate) if s}
                        data.update(found, reverted_by=reverts(git, candidate, rev, task_shas),
                                    status='ok' if found['added'] else 'no_additions')
                    else:
                        data.update(status='lost')
                except GitError:
                    counts['git_error'] += 1
                    continue
                counts[data['status']] += 1
                out.append((path, dict(rid=outcome_rid(candidate, merge['commit'], horizon_days), kind='outcome',
                                       src='harvest', ts=now, run=merge['run'], task=merge['task'],
                                       mode='parallel', data=data)))
    return out, counts


def main(args) -> int:
    events, counts = harvest(horizon_days=args.horizon_days, repo=args.repo, include_temp=args.include_temp)
    written = 0
    if not args.dry_run:
        by_repo = defaultdict(list)
        for path, event in events:
            by_repo[path].append(event)
        for path, batch in by_repo.items():
            written += record(batch, repo=path, where='outcomes')
    label = 'would record' if args.dry_run else 'recorded'
    print('%s %d survival outcome(s) at %d days' % (label, len(events) if args.dry_run else written,
                                                     args.horizon_days))
    for key in ('ok', 'no_additions', 'lost', 'pending', 'already', 'no_base', 'candidate_missing',
                'repo_missing', 'skipped_temp', 'git_error'):
        if counts.get(key):
            print('  %-17s %d' % (key, counts[key]))
    if not args.dry_run and written < len(events):
        print('  %d not written; see %s' % (len(events) - written, dataset_dir() / 'errors.log'))
    return 0
