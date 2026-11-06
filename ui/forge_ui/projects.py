"""The Projects screen (#126): one page per repository, with its runs, every finding forge wrote about it and
every memory forge wrote for it, whichever run or checkout they came from.

A project is one repository root, as repos.py resolves it, and its id is the one the run cloud uses
(cloudmodel.js hashId: FNV-1a over the root's UTF-16 code units), so links between the two views match.

Sources, run dirs first:
  runs       the catalog's run dirs, grouped by their identity's root
  findings   every review of every run dir (each attempt, shadow reviews, final reviews), parsed by forge's
             contract module through findings.py; their state in `forge issues` (dismissed, filed, swept) from
             one offline listing per project; repeats within a task collapsed by `forge issues`' own
             fingerprint (scripts/forge-issues.py, loaded read-only), never matched by guesswork
  memories   `forge memory learnings <dir> --json` for the root and each checkout a run started in that still
             exists: the states come from forge-memory.sh itself
  dataset    `forge dataset history --json` for runs whose dir is gone and checkouts that are gone; those rows
             say so and link to no run page or diff

Finding statuses: open, filed, fixed, swept, dismissed (as on a run's Open findings page) and superseded: a later
review of the same task (or a later final review) passed. That does not claim the finding was fixed.

Cost: nothing here runs on the page's poll. Each tab's endpoint is read when the tab opens; a run's findings
are cached by its `updated` time, the CLI answers for CACHE_S, and ?refresh=1 reads everything again.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import threading
import time

from forge_ui import executions, findings as F, reader, repos

ISSUES = Path(__file__).resolve().parents[2] / 'scripts' / 'forge-issues.py'
CACHE_S = 300
_SEVERITIES = ('P0', 'P1', 'P2', 'P3')
_STATE = {}
_LOCK = threading.Lock()


class _ReadOnlyLoader(importlib.machinery.SourceFileLoader):
    def set_data(self, *args, **kwargs):
        pass


def issues_module():
    """scripts/forge-issues.py for its fingerprint (finding_key over parse_findings), or None."""
    with _LOCK:
        if 'issues' not in _STATE:
            try:
                spec = importlib.util.spec_from_file_location(
                    'forge_issues_for_ui', ISSUES, loader=_ReadOnlyLoader('forge_issues_for_ui', str(ISSUES)))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not (callable(module.finding_key) and callable(module.parse_findings)):
                    raise TypeError('not forge issues')
                _STATE['issues'] = module
            except Exception:
                _STATE['issues'] = None
        return _STATE['issues']


def project_id(root):
    """cloudmodel.js hashId(root): FNV-1a 32-bit over UTF-16 code units, 8 hex digits."""
    h = 0x811c9dc5
    data = str(root).encode('utf-16-le', 'surrogatepass')
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xffffffff
    return '%08x' % h


def fingerprint(first_line):
    """`forge issues`' key for a finding whose first line it can parse, else None (such rows never collapse).
    The canonical `[P1][CONFIRMED] path:line — text` form first, then the other forms forge issues reads from a
    known_issues.md (`parse_known`), so a tolerated finding written another way still finds its entry."""
    module = issues_module()
    if module is None or not first_line:
        return None
    try:
        parsed = module.parse_findings(first_line)
        if not parsed and callable(getattr(module, 'parse_known', None)):
            parsed = module.parse_known(first_line)
        return module.finding_key(parsed[0]) if parsed else None
    except Exception:
        return None


def _isdir(path):
    try:
        return bool(path) and os.path.isabs(path) and os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _first_line(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            return stream.readline(4096).strip() or None
    except (OSError, ValueError):
        return None


# ---- runs and projects ---------------------------------------------------------------------------

def _catalog_runs(catalog):
    """Run summaries with their repository fields, identity source and dataset uid."""
    listed, identities = reader.batch(reader.discover(**catalog.options))
    out = []
    for path, source, entry in listed:
        identity = identities[entry[0]]
        summary = reader._with_repo(reader.summarize(path, source), identity)
        summary['identity_source'] = identity['source']
        summary['dataset_uid'] = _first_line(os.path.join(str(path), 'dataset.uid'))
        summary['no_memory'] = os.path.exists(os.path.join(str(path), 'no_memory'))
        out.append(summary)
    out.sort(key=lambda r: r['updated'] if r['updated'] is not None else float('-inf'), reverse=True)
    return out


def _names(projects):
    """Two repositories with the same folder name get their parent folder too, as in the cloud."""
    counts = {}
    for p in projects:
        counts[p['name']] = counts.get(p['name'], 0) + 1
    for p in projects:
        if counts[p['name']] > 1:
            parts = [x for x in str(p['root']).split('/') if x]
            p['name'] = '%s (%s)' % (p['name'], parts[-2] if len(parts) > 1 else '/')


def _memory_lines(root):
    try:
        with open(os.path.join(root, '.forge', 'memory.md'), encoding='utf-8', errors='replace') as stream:
            return sum(1 for line in stream if line.startswith('- '))
    except (OSError, ValueError):
        return None


def group(runs):
    """{project id: project} for the catalog's runs."""
    out = {}
    for run in runs:
        root = reader.repo_root(dict(root=run.get('repo_root')), run.get('repo'))
        if not root:
            continue
        pid = project_id(root)
        p = out.setdefault(pid, dict(id=pid, root=root, name=os.path.basename(root.rstrip('/')) or root, runs=[],
                                     exists=_isdir(root), live=0, last=None, path_only=0))
        p['runs'].append(run)
        if (run.get('counts') or {}).get('RUNNING'):
            p['live'] += 1
        if run.get('updated') is not None and (p['last'] is None or run['updated'] > p['last']):
            p['last'] = run['updated']
        if run.get('identity_source') == 'path':
            p['path_only'] += 1
    _names(list(out.values()))
    return out


def checkouts(project):
    """[{path, exists, branch, runs}] of the checkouts the project's runs started in, the root first."""
    seen = {}
    for run in project['runs']:
        path = run.get('repo_worktree') or project['root']
        c = seen.setdefault(path, dict(path=path, exists=_isdir(path), branch=run.get('repo_branch'), runs=0,
                                       root=path == project['root']))
        c['runs'] += 1
        c['branch'] = c['branch'] or run.get('repo_branch')
    return sorted(seen.values(), key=lambda c: (not c['root'], c['path']))


# ---- findings --------------------------------------------------------------------------------------

def _review_rows(rd, run, owner, reviews, names=None):
    """One occurrence per finding of each review in `reviews` (oldest first), with `superseded` set. `names`
    gives each final review's dir name; a task's reviews have none."""
    out = []
    passes = [(i, r) for i, r in enumerate(reviews) if r['verdict'] == 'PASS' and r['kind'] in ('primary', 'final')]
    for index, review in enumerate(reviews):
        if not review['last']:
            continue
        text = rd.text(review['last'])
        listed = F.listing(text) if text else None
        if not listed:
            continue
        info = rd.stat(review['last'])
        at = float(info.st_mtime) if info is not None else None
        attempt = review['attempt']

        def later(pass_index, pass_review):
            if review['kind'] == 'shadow':
                return pass_review['attempt'] is not None and attempt is not None and pass_review['attempt'] > attempt
            if attempt is not None and pass_review['attempt'] is not None and review['kind'] == 'primary':
                return pass_review['attempt'] > attempt
            return pass_index > index
        superseded = any(later(i, r) for i, r in passes)
        final_name = names[index] if names else None
        for n, item in enumerate(listed):
            first = (item['text'] or '').split('\n', 1)[0]
            fid = reader._finding_id(review, n)
            if final_name:
                href = '#/runs/%s/final-review/%s/diff/finding/%s' % (run['key'], final_name, fid)
            else:
                href = '#/runs/%s/tasks/%s/diff/finding/%s' % (run['key'], owner, fid)
            out.append(dict(source='run', run=run['key'], run_id=run.get('run_id'), task=None if final_name else owner,
                            final_review=final_name, attempt=attempt, kind=review['kind'], reviewer=review['spec'],
                            verdict=review['verdict'], at=at, id=fid, severity=item['severity'],
                            confidence=item['status'], location=item['location'], summary=item['summary'],
                            text=item['text'], fp=fingerprint(first), superseded=superseded, href=href))
    return out


def run_occurrences(run):
    """Every finding of every review of one run dir, oldest review first within each task."""
    rd = reader._Read(run['path'])
    kind = reader._kind(rd.path)
    out = []
    if kind is None:
        return out
    task_ids = ['solo'] if kind == 'solo' else [t['id'] for t in reader._tasks(rd)]
    for task_id in task_ids:
        ctx = reader._diff_task(rd, task_id)
        if ctx:
            out += _review_rows(rd, run, task_id, ctx['reviews'])
    if kind == 'plan':
        items, published = executions.review_dirs(rd)[0], reader._published(rd)
        finals = []
        for index, item in enumerate(items[:reader._MAX_REVIEWS]):
            summary = reader._final_review(rd, item, index == 0, published)[0]
            finals.append((item, reader._diff_review('final', None, None, summary['spec'], summary['verdict'],
                                                     item[3] + '/qa.last', [], summary['execution'])))
        finals.reverse()                                   # oldest first: a later PASS supersedes
        out += _review_rows(rd, run, None, [review for _, review in finals], names=[item[0] for item, _ in finals])
    return out


def _dataset_occurrences(hist, runs_here):
    """Rows from the dataset for runs whose dir is gone: no link, labelled `dataset`."""
    out = []
    by_run = {}
    for review in hist.get('findings') or []:
        if review.get('run') in runs_here:
            by_run.setdefault((review['run'], review.get('task')), []).append(review)
    for (run, task), reviews in by_run.items():
        reviews.sort(key=lambda r: r.get('ts') or 0)
        for index, review in enumerate(reviews):
            superseded = any(r.get('verdict') == 'PASS' and r.get('role') == 'qa' for r in reviews[index + 1:])
            for item in review.get('findings') or []:
                head = item['head']
                module = F._load()
                try:
                    match = module._LOCATION.search(head) if module else None
                except Exception:
                    match = None
                sev = item.get('severity')
                out.append(dict(source='dataset', run=None, run_id=review.get('run_id'), task=task,
                                final_review='final' if review.get('scope') == 'final_review' else None,
                                attempt=review.get('attempt'), kind='shadow' if review.get('role') == 'qa_shadow' else
                                'final' if review.get('scope') == 'final_review' else 'primary',
                                reviewer=review.get('reviewer'), verdict=review.get('verdict'), at=review.get('ts'),
                                id=None, severity='P%d' % sev if isinstance(sev, int) else None,
                                confidence=item.get('status'), location=match.group(0) if match else None,
                                summary=head.strip()[:240], text=head, fp=fingerprint(head), superseded=superseded,
                                href=None))
    return out


def collapse(occurrences):
    """Rows: repeats of one finding within a task (or a run's final reviews) are one row with a count, by
    `forge issues`' fingerprint; a finding it cannot fingerprint is its own row. The newest occurrence leads, a
    primary one before any shadow one: a finding both reviewers reported is not hidden with the shadow reviews."""
    rows, order = {}, []
    for occ in occurrences:
        scope = (occ['source'], occ['run'] or occ['run_id'], occ['task'], bool(occ['final_review']))
        key = scope + (occ['fp'],) if occ['fp'] else ('one', id(occ))
        if key not in rows:
            rows[key] = dict(occ, count=0, occurrences=[])
            order.append(key)
        row = rows[key]
        row['count'] += 1
        row['occurrences'].append(dict(run=occ['run'], run_id=occ['run_id'], task=occ['task'], attempt=occ['attempt'],
                                       kind=occ['kind'], reviewer=occ['reviewer'], at=occ['at'], href=occ['href']))
        shadow, lead_shadow = occ['kind'] == 'shadow', row['kind'] == 'shadow'
        if (lead_shadow and not shadow) or (shadow == lead_shadow and (occ['at'] or 0) >= (row['at'] or 0)):
            row.update({k: occ[k] for k in occ})
    return [rows[k] for k in order]


def status_of(row, issue):
    """open | filed | fixed | swept | dismissed | superseded, as findingsview.statusOf plus superseded."""
    if issue:
        if issue.get('dismissed'):
            return 'dismissed'
        if issue.get('filed'):
            return 'fixed' if str((issue['filed'] or {}).get('state') or '').upper() == 'CLOSED' else 'filed'
        if issue.get('swept'):
            return 'swept'
        return 'open'
    return 'superseded' if row['superseded'] else 'open'


def _real(path):
    try:
        return os.path.realpath(path) if path else None
    except (OSError, ValueError):
        return None


def issue_index(listing):
    """{(run dir, task, forge issues key): entry} of an offline `forge issues --json --all-runs` listing. The key
    holds the run and the task: the same text found in two runs is two findings, each with its own state."""
    module = issues_module()
    out = {}
    for entry in (listing or {}).get('findings') or []:
        if not isinstance(entry, dict) or not isinstance(entry.get('text'), str):
            continue
        try:
            key = module.finding_key(dict(file=entry.get('file') or '', text=entry['text'])) if module else None
        except Exception:
            key = None
        if key:
            out.setdefault((_real(entry.get('plan_dir')), entry.get('task'), key), entry)
    return out


def issue_for(index, row, run_path):
    """The entry of a row found in a run dir: same run, same task (none for a final review). A solo run's task is
    named after its dir in forge issues and `solo` here: any entry of that run with the key is its own."""
    if not row['fp'] or row['source'] != 'run' or not run_path:
        return None
    where = _real(run_path)
    found = index.get((where, row['task'], row['fp']))
    if found is None and row['task'] == 'solo':
        found = next((e for (w, _t, k), e in index.items() if w == where and k == row['fp']), None)
    return found


# ---- the cached screen -----------------------------------------------------------------------------

class Screen:
    """Cached answers for one server: per-run findings by `updated`, the CLI's by CACHE_S."""

    def __init__(self):
        self.lock = threading.Lock()
        self.runs = {}            # run path -> (updated, occurrences)
        self.cli = {}             # key -> (time, value)

    def _cli(self, key, call, refresh, now):
        hit = self.cli.get(key)
        if not refresh and hit is not None and now - hit[0] < CACHE_S:
            return hit[1]
        value = call()
        self.cli[key] = (now, value)
        return value

    def occurrences(self, run):
        hit = self.runs.get(run['path'])
        if hit is not None and hit[0] == run['updated']:
            return hit[1]
        found = run_occurrences(run)
        self.runs[run['path']] = (run['updated'], found)
        return found

    def projects(self, catalog):
        return group(_catalog_runs(catalog))

    def project(self, catalog, pid):
        return self.projects(catalog).get(pid)

    def listing(self, catalog):
        with self.lock:
            projects = self.projects(catalog)
            out = []
            for p in projects.values():
                # not superseded by a later passing review; dismissals and filed issues show on the Findings tab
                open_findings = sum(1 for row in collapse([o for run in p['runs'] for o in self.occurrences(run)])
                                    if not row['superseded'])
                out.append(dict(id=p['id'], name=p['name'], root=p['root'], exists=p['exists'], runs=len(p['runs']),
                                live=p['live'], last=p['last'], path_only=p['path_only'],
                                checkouts=len(checkouts(p)), memory_lines=_memory_lines(p['root']) if p['exists'] else None,
                                open_findings=open_findings))
            out.sort(key=lambda p: p['last'] or 0, reverse=True)
            return dict(projects=out)

    def detail(self, catalog, pid):
        p = self.project(catalog, pid)
        if p is None:
            return None
        return dict(id=p['id'], name=p['name'], root=p['root'], exists=p['exists'], live=p['live'], last=p['last'],
                    path_only=p['path_only'], checkouts=checkouts(p), runs=p['runs'],
                    memory_off_runs=sum(1 for r in p['runs'] if r.get('no_memory')))

    def _dataset_runs(self, p, hist, all_projects):
        """Dataset uids of the project's runs whose dir is gone."""
        existing = {r['dataset_uid'] for q in all_projects.values() for r in q['runs'] if r.get('dataset_uid')}
        known_ids = {(r.get('run_id'), r.get('repo')) for r in p['runs']}
        here = {c['path'] for c in checkouts(p)} | {p['root']}
        roots = [q['root'] for q in all_projects.values() if q['exists']]
        out = set()
        for run in (hist or {}).get('runs') or []:
            if run.get('run') in existing or (run.get('run_id'), run.get('repo_path')) in known_ids:
                continue
            path = run.get('repo_path')
            if path in here:
                out.add(run['run'])
            elif path and not _isdir(path) and run.get('base') and p['exists']:
                # the rule repos.py uses for a run whose checkout is gone: its base commit in exactly one known root
                matches = [root for root in roots if _has_commit(root, run['base'])]
                if matches == [p['root']]:
                    out.add(run['run'])
        return out

    def findings(self, catalog, pid, history, issues, refresh=False, now=None):
        now = time.time() if now is None else now
        with self.lock:
            projects = self.projects(catalog)
            p = projects.get(pid)
            if p is None:
                return None
            if refresh:
                self.runs.clear()
            occ = []
            for run in p['runs']:
                occ += self.occurrences(run)
            hist = self._cli('history', history, refresh, now)
            gone = self._dataset_runs(p, hist, projects) if hist else set()
            if hist:
                occ += _dataset_occurrences(hist, gone)
            # forge issues reads every run of the repository (--all-runs) from any one of them, but needs that run's
            # checkout to name its GitHub repository: runs started in the root first, at most three tries.
            tried = sorted(p['runs'], key=lambda r: (r.get('repo_worktree') is not None, r.get('kind') != 'plan'))[:3]
            listing = None
            for run in tried:
                listing = self._cli(('issues', run['path']), lambda: issues(run['path']), refresh, now)
                if listing is not None:
                    break
            index = issue_index(listing)
            paths = {run['key']: run['path'] for run in p['runs']}
            rows = collapse(occ)
            for row in rows:
                issue = issue_for(index, row, paths.get(row['run']))
                row['status'] = status_of(row, issue)
                row['fileable'] = issue is not None and row['status'] == 'open' and row['source'] == 'run'
                row['origin'] = issue.get('origin') if issue else None
            rows.sort(key=lambda r: (_SEVERITIES.index(r['severity']) if r['severity'] in _SEVERITIES else 9, -(r['at'] or 0)))
            notes = []
            if hist is None:
                notes.append('the dataset could not be read: findings of runs whose directory is gone are not listed')
            elif not hist.get('enabled'):
                notes.append('the dataset is off (FORGE_DATASET=off): only the run directories that still exist are read')
            if listing is None:
                notes.append('forge issues could not be read: dismissed and filed findings show as open')
            return dict(id=pid, rows=rows, notes=notes, runs=len(p['runs']), dataset_runs=len(gone),
                        computed=now, available=F.available())

    def memories(self, catalog, pid, learnings, history, refresh=False, now=None):
        now = time.time() if now is None else now
        with self.lock:
            projects = self.projects(catalog)
            p = projects.get(pid)
            if p is None:
                return None
            sources = []
            for c in checkouts(p):
                if not c['exists']:
                    continue
                stamp = tuple(_mtime(os.path.join(c['path'], '.forge', name)) for name in ('ledger.tsv', 'memory.md'))
                got = self._cli(('memory', c['path'], stamp), lambda: learnings(c['path']), refresh, now)
                sources.append(dict(path=c['path'], root=c['root'], branch=c['branch'], result=got))
            hist = self._cli('history', history, refresh, now)
            merged = _merge_learnings(sources, p, hist, self._dataset_runs(p, hist, projects) if hist else set())
            texts = {s['path']: _memory_text(s['result']) for s in sources if s['result'] and s['result'].get('memory') is not None}
            notes = []
            if hist is None:
                notes.append('the dataset could not be read: learnings of checkouts that are gone are not listed')
            elif not hist.get('enabled'):
                notes.append('the dataset is off (FORGE_DATASET=off): only the checkouts that still exist are read')
            return dict(id=pid, sources=[dict(path=s['path'], root=s['root'], branch=s['branch'],
                                              ok=s['result'] is not None,
                                              ledger=bool(s['result'] and s['result'].get('ledger')),
                                              memory=(s['result'] or {}).get('memory'),
                                              rebuilt_differs=bool((s['result'] or {}).get('rebuilt_differs')),
                                              max_lines=(s['result'] or {}).get('max_lines'))
                                         for s in sources],
                        differs=len(set(texts.values())) > 1, learnings=merged, notes=notes,
                        memory_off_runs=sum(1 for r in p['runs'] if r.get('no_memory')), runs=len(p['runs']),
                        computed=now)


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _memory_text(result):
    return '\n'.join('%s:%s' % (s['category'], line['text']) for s in result['memory'] for line in s['lines'])


def _merge_learnings(sources, project, hist, gone_runs):
    """Every learning once: rows from each checkout's ledger, then the dataset's for checkouts that are gone.
    `states` says what each checkout's forge-memory.sh does with it."""
    merged, order = {}, []
    for s in sources:
        for item in (s['result'] or {}).get('learnings') or []:
            key = (item['category'], item['key'])
            if key not in merged:
                merged[key] = dict(category=item['category'], key=item['key'], text=item['text'], count=item['count'],
                                   states={}, rows=[], seen=set(), last=item['last'])
                order.append(key)
            m = merged[key]
            m['states'][s['path']] = item['state']
            if item['last'] >= m['last']:
                m['last'], m['text'] = item['last'], item['text']
            for row in item['rows']:
                ident = (row['ts'], row['run_id'], row['task'], row['role'], row['text'])
                if ident in m['seen']:
                    for r in m['rows']:
                        if (r['ts'], r['run_id'], r['task'], r['role'], r['text']) == ident and s['path'] not in r['from']:
                            r['from'].append(s['path'])
                    continue
                m['seen'].add(ident)
                m['rows'].append(dict(row, source='ledger', **{'from': [s['path']]}))
    norm = _norm_key
    for item in (hist or {}).get('learnings') or []:
        if item.get('run') not in gone_runs or item.get('category') not in ('verify', 'trap', 'finding'):
            continue
        key = (item['category'], norm(item['text']))
        ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(item['ts'])) if isinstance(item.get('ts'), (int, float)) else None
        if key not in merged:
            merged[key] = dict(category=item['category'], key=key[1], text=item['text'], count=0, states={}, rows=[],
                               seen=set(), last=ts or '')
            order.append(key)
        m = merged[key]
        ident = (ts, item.get('run_id'), item.get('task'), item.get('role'), item['text'])
        if ident not in m['seen']:
            m['seen'].add(ident)
            m['rows'].append(dict(ts=ts, run_id=item.get('run_id'), task=item.get('task'), role=item.get('role'),
                                  model=item.get('model'), verdict=None, text=item['text'], injectable=True,
                                  source='dataset', **{'from': ['dataset']}))
    out = []
    for key in order:
        m = merged.pop(key)
        m.pop('seen')
        m['dataset_only'] = not m['states']
        out.append(m)
    out.sort(key=lambda m: m['last'] or '', reverse=True)
    return out


def _norm_key(text):
    """forge-memory.sh's norm_key of a fact without its [anchor], for dataset learnings that have no ledger key."""
    import re
    body = re.sub(r'\s*\[[^][]+\]\s*$', '', text)
    return re.sub(r'[^a-z0-9]+', ' ', body.lower()).strip()


def _has_commit(root, sha):
    done = repos._run_git(root, 'cat-file', '-e', sha + '^{commit}')
    return done is not None and done.returncode == 0


def screen(server):
    with _LOCK:
        found = getattr(server, 'project_screen', None)
        if found is None:
            found = server.project_screen = Screen()
        return found
