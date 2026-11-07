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

Integrations are followed where they went (#120). One whose branch was deleted, or whose
candidate is no longer on that branch's first-parent chain, is looked for in every other
branch (local, then remote; never forge's own): the commit where it entered a branch's
first-parent chain, a merge or itself, is where the horizon is walked from. A run nobody
ran `integrate` for, its integration branch merged by hand, is followed the same way from
its last integration commit, with the horizon counted from when it landed. A squash or a
rebase leaves no ancestry to follow: an `integrate` that went that way is `lost`, and a
hand-merged run that did is `not_landed`, retried at every harvest and never written.

`--deadline S` stops starting new work after S seconds and records what it has; the run's
automatic harvest (end of `run` and `integrate`, FORGE_OUTCOMES=off to skip) uses it. Every
harvest that writes leaves `outcomes.last` in the dataset dir for `forge dataset status`.

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
_FORGE_REF = re.compile(r'^refs/(?:heads|remotes/[^/]+)/forge/')
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
    integrations, merges, bases, landed_by_hand = [], {}, {}, defaultdict(list)
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
                if d.get('integration'):
                    landed_by_hand[(repo, run)].append((ts, d['integration']))
    integrated = {(i['repo'], i['run']) for i in integrations}
    runs = [dict(repo=repo, run=run, commits=[c for _, c in sorted(found)]) for (repo, run), found in landed_by_hand.items()
            if (repo, run) not in integrated]
    return dict(integrations=sorted(integrations, key=lambda i: i['ts']), merges=merges, bases=bases,
                repos=repos, have=have, runs=sorted(runs, key=lambda r: (r['repo'] or '', r['run'] or '')))


def outcome_rid(candidate: str, reviewed: str, horizon_days: int) -> str:
    return 'outcome:survival:' + sha256(('%s:%s:%d' % (candidate, reviewed, horizon_days)).encode())[:24]


def landing(git: Git, tip: str, candidate: str):
    """The commit where `candidate` entered `tip`'s first-parent chain: `candidate` itself when it is on
    that chain, else the oldest chain commit that contains it (the merge that brought it in); None when
    `tip` does not contain it."""
    if tip == candidate:
        return candidate
    if git.out('merge-base', '--is-ancestor', candidate, tip, ok=(0, 1))[0]:
        return None
    chain = [line.split() for line in git.text('log', '--first-parent', '--format=%H %P', '%s..%s' % (candidate, tip)
                                               ).splitlines()]           # newest first
    if chain and chain[-1][1:2] == [candidate]:
        return candidate
    descendants = set(git.text('rev-list', '--ancestry-path', '%s..%s' % (candidate, tip)).split())
    return next((row[0] for row in reversed(chain) if row[0] in descendants), None)


def walk(git: Git, tip: str, start: str, cutoff: float, now: float):
    """('ok', rev, ts) | ('pending', None, None): the last commit of `tip`'s first-parent chain from `start`
    on that was there at `cutoff`. By position along the chain, so out-of-order commit dates cannot jump
    past a commit that came later; `start` itself when it came after the cutoff."""
    chain = []
    for line in git.text('log', '--first-parent', '--format=%H %ct', '%s..%s' % (start, tip)).splitlines():
        sha, ct = line.split()[:2]
        chain.append((sha, int(ct)))
    rev, rev_ts = start, int(git.text('log', '-1', '--format=%ct', start).strip())
    for sha, ct in reversed(chain):
        if ct > cutoff:
            return 'ok', rev, rev_ts
        rev, rev_ts = sha, ct
    if now < cutoff:
        return 'pending', None, None      # more commits may still land before the horizon
    return 'ok', rev, rev_ts


def horizon_rev(git: Git, branch: str, candidate: str, cutoff: float, now: float):
    """('ok', rev, ts) | ('lost', None, None) | ('pending', None, None) on the branch `integrate` advanced.

    The branch is resolved once to an immutable tip. The candidate must sit on its
    first-parent chain; when it does not (the branch was deleted, rebased, squashed or reset),
    harvest() looks for it elsewhere with follow().
    """
    if not branch or git.out('rev-parse', '--verify', '-q', branch + '^{commit}', ok=(0, 1))[0]:
        return 'lost', None, None
    tip = git.text('rev-parse', branch + '^{commit}').strip()
    if landing(git, tip, candidate) != candidate:
        return 'lost', None, None         # the walk never passed through the candidate
    return walk(git, tip, candidate, cutoff, now)


def follow(git: Git, candidate: str):
    """(branch, tip, landing, landing time) of the branch where `candidate` landed first, or None. Every
    branch that contains it (local and remote, never forge's own) is a candidate; the earliest landing
    wins, a local branch before a remote one at the same time, then by name."""
    best = None
    refs = git.text('for-each-ref', '--contains', candidate, '--format=%(refname)', 'refs/heads', 'refs/remotes').split()
    for ref in refs:
        if _FORGE_REF.match(ref) or ref.endswith('/HEAD'):
            continue
        tip = git.text('rev-parse', ref + '^{commit}').strip()
        landed = landing(git, tip, candidate)
        if landed is None:
            continue
        at = int(git.text('log', '-1', '--format=%ct', landed).strip())
        key = (at, ref.startswith('refs/remotes/'), ref)
        if best is None or key < best[0]:
            best = (key, ref, tip, landed, at)
    if best is None:
        return None
    _, ref, tip, landed, at = best
    return ref.split('/', 2)[2], tip, landed, at


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
            now: float | None = None, deadline: float | None = None) -> tuple[list, Counter]:
    """(events to record, counts). `deadline` (seconds) stops starting new integrations once passed; what
    was left is counted as `deferred` and found again by the next harvest."""
    from .importer import is_temp
    state = read_store()
    counts = Counter()
    if state is None:
        return [], counts
    now = time.time() if now is None else now
    stop = time.monotonic() + deadline if deadline is not None else None
    wanted = str(Path(repo).resolve()) if repo else None
    by_repo = defaultdict(list)
    for kind, items in (('integrate', state['integrations']), ('merged', state['runs'])):
        for item in items:
            path = state['repos'].get(item['repo'])
            if not path or (wanted and str(Path(path).resolve()) != wanted):
                continue
            if is_temp(path) and not include_temp:
                counts['skipped_temp'] += 1
                continue
            if not Path(path).is_dir():
                counts['repo_missing'] += 1
                continue
            by_repo[path].append((kind, item))
    out = []
    for path, items in by_repo.items():
        git = Git(path)
        for kind, item in items:
            if stop is not None and time.monotonic() > stop:
                counts['deferred'] += 1
                continue
            try:
                found = _locate(git, kind, item, state, horizon_days, now, counts)
            except GitError:
                counts['git_error'] += 1
                continue
            if found is None:
                continue
            candidate, todo, status, rev, rev_ts, where = found
            for merge in todo:
                base = state['bases'].get(merge['commit'])
                if not base:
                    counts['no_base'] += 1
                    continue
                data = dict(name='survival', candidate=candidate, reviewed=merge['commit'], base_ref=base,
                            horizon_days=horizon_days, horizon_rev=rev, horizon_ts=rev_ts, **where)
                try:
                    if status == 'ok':
                        measured = survival(git, base, merge['commit'], rev)
                        task_shas = set(measured.pop('commits')) | {s for s in (merge.get('integration'), candidate) if s}
                        data.update(measured, reverted_by=reverts(git, where.get('landed') or candidate, rev, task_shas),
                                    status='ok' if measured['added'] else 'no_additions')
                    else:
                        data.update(status='lost')
                except GitError:
                    counts['git_error'] += 1
                    continue
                counts[data['status']] += 1
                if where.get('followed'):
                    counts['followed'] += 1
                out.append((path, dict(rid=outcome_rid(candidate, merge['commit'], horizon_days), kind='outcome',
                                       src='harvest', ts=now, run=merge['run'], task=merge['task'],
                                       mode='parallel', data=data)))
    return out, counts


def _locate(git, kind, item, state, horizon_days, now, counts):
    """(candidate, merges to record, status, horizon rev, its time, how it was found) for one integration,
    or None when there is nothing to record now (counted)."""
    if kind == 'integrate':
        candidate, parent, run = item['candidate'], item['parent'], item['run']
    else:
        commits = [c for c in item['commits'] if git.exists(c + '^{commit}')]
        if not commits:
            counts['integration_gone'] += 1
            return None
        candidate, run = commits[-1], item['run']
        parent = git.text('rev-parse', commits[0] + '^1').strip() if git.exists(commits[0] + '^1') else None
    if not git.exists(candidate + '^{commit}'):
        counts['candidate_missing'] += 1
        return None
    landed = set(git.text('rev-list', candidate, *(['^' + parent] if parent else [])).split())
    tasks = [m for (r, c), m in state['merges'].items() if r == item['repo'] and c in landed
             and (kind == 'integrate' or m['run'] == run)]
    todo = [m for m in tasks if outcome_rid(candidate, m['commit'], horizon_days) not in state['have']]
    counts['already'] += len(tasks) - len(todo)
    if not todo:
        return None
    if kind == 'integrate':
        cutoff = item['ts'] + horizon_days * 86400
        where = dict(branch=item['branch'], integrated_ts=item['ts'], integrate_run=run)
        status, rev, rev_ts = horizon_rev(git, item['branch'], candidate, cutoff, now)
        if status == 'lost':
            seen = follow(git, candidate)
            if seen is not None:
                branch, tip, landing_commit, _at = seen
                status, rev, rev_ts = walk(git, tip, landing_commit, cutoff, now)
                where.update(followed=branch, landed=landing_commit)
            else:
                gone = not item['branch'] or git.out('rev-parse', '--verify', '-q', item['branch'] + '^{commit}',
                                                     ok=(0, 1))[0] != 0
                where.update(lost_cause='branch_gone' if gone else 'off_branch')
    else:
        seen = follow(git, candidate)
        if seen is None:
            counts['not_landed'] += len(todo)        # not merged yet, or squashed: nothing to follow
            return None
        branch, tip, landing_commit, at = seen
        cutoff = at + horizon_days * 86400
        status, rev, rev_ts = walk(git, tip, landing_commit, cutoff, now)
        where = dict(branch=None, integrated_ts=at, integrate_run=None, followed=branch, landed=landing_commit,
                     merged_by_hand=True)
    if status == 'pending':
        counts['pending'] += len(todo)
        return None
    return candidate, todo, status, rev, rev_ts, where


LAST = 'outcomes.last'


def last_harvest():
    """What the last harvest that wrote did ({at, repo, recorded, counts}), or None."""
    try:
        data = json.loads((dataset_dir() / LAST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(args) -> int:
    if getattr(args, 'quiet', False):
        # the automatic harvest at the end of a run: whatever goes wrong is logged, never raised
        try:
            return _main(args)
        except Exception as error:  # noqa: BLE001 - a harvest must never fail the run that called it
            from . import log_error
            log_error('outcomes', error)
            return 0
    return _main(args)


def _main(args) -> int:
    events, counts = harvest(horizon_days=args.horizon_days, repo=args.repo, include_temp=args.include_temp,
                             deadline=getattr(args, 'deadline', None))
    written = 0
    if not args.dry_run:
        by_repo = defaultdict(list)
        for path, event in events:
            by_repo[path].append(event)
        for path, batch in by_repo.items():
            written += record(batch, repo=path, where='outcomes')
        stamp = dict(at=time.time(), repo=str(Path(args.repo).resolve()) if args.repo else None, recorded=written,
                     counts=dict(counts))
        try:
            target = dataset_dir() / LAST
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp')
            tmp.write_text(json.dumps(stamp, sort_keys=True) + '\n')
            os.replace(tmp, target)
        except OSError:
            pass
    if getattr(args, 'quiet', False):
        return 0
    label = 'would record' if args.dry_run else 'recorded'
    print('%s %d survival outcome(s) at %d days' % (label, len(events) if args.dry_run else written,
                                                     args.horizon_days))
    for key in ('ok', 'no_additions', 'lost', 'followed', 'pending', 'not_landed', 'already', 'no_base',
                'candidate_missing', 'integration_gone', 'repo_missing', 'skipped_temp', 'deferred', 'git_error'):
        if counts.get(key):
            print('  %-17s %d' % (key, counts[key]))
    if not args.dry_run and written < len(events):
        print('  %d not written; see %s' % (len(events) - written, dataset_dir() / 'errors.log'))
    return 0
