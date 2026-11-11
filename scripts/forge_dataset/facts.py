"""`forge dataset facts`: the reach, origin and evidence reviewers give each finding (#119), measured in shadow.

A reviewer ends a finding's first line with `{reach=main|realistic|unusual origin=new|existing
evidence=ran|traced|suspected}`. Nothing in forge decides on these facts yet. This report says whether
a gate that did would be better than the severity line alone:

  coverage   how many findings carry the facts, and how reach splits by severity
  agreement  pairs of reviews of one attempt (`--shadow-qa`): of the findings both reviewers name (same
             file, lines at most LINE_WINDOW apart), how often they agree on the severity and on each
             fact, raw and chance-corrected (Cohen's kappa, since severity has four values and reach three)
  rules      each candidate rule replayed on the review every task attempt ended on, against the same
             replay without it: the reviews that would have passed, the attempts after them that would
             not have run, and the findings that would have stopped blocking, to check by hand whether
             they were bugs that mattered

The rules only relax: a finding they release is CONFIRMED with a severity at the threshold or worse.
`unusual` releases reach=unusual unless it is P0 (P0 is data loss, a security hole or a crash: the
"unless security or data loss" exception), `existing` releases origin=existing (it would go to the
backlog), `both` does both. An unlabelled finding is never released, as it blocks at every threshold.

Advisory only: nothing that builds a prompt, gates or routes reads this report.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

from . import connect, dataset_dir, is_side_role
from .export import Runs, Store, _ending_qa, _gate_decision, not_an_attempt, shadow_pairs

FACTS = {'reach': ('main', 'realistic', 'unusual'), 'origin': ('new', 'existing'),
         'evidence': ('ran', 'traced', 'suspected')}
LINE_WINDOW = 10
EXAMPLES = 50
RULES = {
    'unusual': 'unusual reach does not block unless P0',
    'existing': 'existing findings do not block',
    'both': 'both of the above',
}
_WHERE = re.compile(r'([\w./\\-]+\.\w+|[\w./\\-]+/[\w.-]+):(\d+)')


def _facts(f) -> dict:
    found = f.get('facts')
    return found if isinstance(found, dict) else {}


def _released(rule):
    def release(f):
        sev, facts = f.get('severity'), _facts(f)
        if f.get('status') not in ('CONFIRMED', 'UNSTATED') or sev is None:
            return False
        unusual = facts.get('reach') == 'unusual' and sev > 0
        existing = facts.get('origin') == 'existing'
        return {'unusual': unusual, 'existing': existing, 'both': unusual or existing}[rule]
    return release


def decide(verdict, found, rank, release=lambda f: False) -> str:
    """pass or fail: forge-contract's gate on recorded findings, with `release`d findings tolerated.

    Without a release it is the gate itself: a PASS fails on a blocking finding (CONFIRMED or UNSTATED,
    at the threshold or worse, or unlabelled); a FAIL passes when it has CONFIRMED findings, none
    UNSTATED, and every CONFIRMED one is labelled below the threshold. A released finding counts as
    tolerated, so a FAIL whose blocking findings are all released passes."""
    def tolerated(f):
        return release(f) or (f.get('severity') is not None and f['severity'] > rank)
    if verdict == 'PASS':
        return 'fail' if any(f.get('status') in ('CONFIRMED', 'UNSTATED') and not tolerated(f) for f in found) else 'pass'
    confirmed = [f for f in found if f.get('status') == 'CONFIRMED']
    if not confirmed or any(f.get('status') == 'UNSTATED' and not release(f) for f in found):
        return 'fail'
    return 'pass' if all(tolerated(f) for f in confirmed) else 'fail'


def _rank(value) -> int:
    value = (value or '').strip().upper()
    if value == 'NONE':
        return 3
    return int(value[1]) if value in ('P0', 'P1', 'P2', 'P3') else 2


def _threshold(runs, run, task, attempt):
    """The threshold the attempt's gate ran with (its qa.gate line), else the plan's, else P2."""
    for e in runs.attempts.get((run, task), []):
        if e['attempt'] == attempt:
            cells = (e['d'].get('gate') or '').split('\t')
            if cells[0].strip():
                return _rank(cells[0])
    plans = runs.plans.get(run) or []
    return _rank(((plans[-1]['d'].get('options') or {}).get('qa_threshold')) if plans else None)


def _kappa(pairs) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    seen = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    chance = sum(left[c] * right[c] for c in left) / (n * n)
    return None if chance >= 1 else (seen - chance) / (1 - chance)


def _where(f):
    match = _WHERE.search(f.get('head') or '')
    return (match.group(1), int(match.group(2))) if match else None


def _matched(a_found, b_found) -> list:
    """(a, b) findings two reviewers both named: same file, the nearest line at most LINE_WINDOW apart."""
    out, used = [], set()
    for a in a_found:
        at = _where(a)
        if at is None:
            continue
        best = None
        for i, b in enumerate(b_found):
            bt = _where(b)
            if i in used or bt is None or bt[0] != at[0] or abs(bt[1] - at[1]) > LINE_WINDOW:
                continue
            if best is None or abs(bt[1] - at[1]) < best[0]:
                best = (abs(bt[1] - at[1]), i)
        if best is not None:
            used.add(best[1])
            out.append((a, b_found[best[1]]))
    return out


def _measure(pairs) -> dict:
    return dict(compared=len(pairs), agree=sum(a == b for a, b in pairs),
                kappa=None if _kappa(pairs) is None else round(_kappa(pairs), 3))


def _empty():
    return dict(coverage=dict(reviews=0, reviews_with_facts=0, findings=0, findings_with_facts=0,
                              values={k: {} for k in FACTS}, reach_by_severity={}),
                agreement=dict(pairs=0, matched=0, severity=_measure([]), **{k: _measure([]) for k in FACTS}),
                rules=dict(reviews=0, reviews_with_facts=0,
                           **{name: dict(label=label, would_pass=0, attempts_saved=0, released=[])
                              for name, label in RULES.items()}),
                line_window=LINE_WINDOW)


def report(runs: Runs, repo: str = None) -> dict:
    out = _empty()
    paths = runs.store.repo_paths

    def mine(e):
        return not repo or paths.get(e['repo']) == repo

    cov = out['coverage']
    values = {k: Counter() for k in FACTS}
    by_sev = defaultdict(Counter)
    for e in runs.store.of('dispatch'):
        d = e['d']
        role = d.get('role')
        # qa and qa_shadow are reviews. A side dispatch is neither, even when it carries a findings list.
        if role not in ('qa', 'qa_shadow') or is_side_role(role) or not mine(e) or not isinstance(d.get('findings'), list):
            continue
        found = [f for f in d['findings'] if isinstance(f, dict)]
        cov['reviews'] += 1
        cov['reviews_with_facts'] += any(_facts(f) for f in found)
        for f in found:
            cov['findings'] += 1
            facts = _facts(f)
            if not facts:
                continue
            cov['findings_with_facts'] += 1
            for key in FACTS:
                values[key][facts.get(key) or 'unreadable'] += 1
            sev = f.get('severity')
            by_sev['P%d' % sev if isinstance(sev, int) else 'unlabelled'][facts.get('reach') or 'unreadable'] += 1
    cov['values'] = {k: dict(sorted(c.items())) for k, c in values.items()}
    cov['reach_by_severity'] = {s: dict(sorted(c.items())) for s, c in sorted(by_sev.items())}

    agree = out['agreement']
    severity, per_fact = [], {k: [] for k in FACTS}
    for primary, shadow in shadow_pairs(runs).values():
        if not mine(primary):
            continue
        agree['pairs'] += 1
        for a, b in _matched(primary['d'].get('findings') or [], shadow['d'].get('findings') or []):
            agree['matched'] += 1
            if a.get('severity') is not None and b.get('severity') is not None:
                severity.append((a['severity'], b['severity']))
            for key in FACTS:
                x, y = _facts(a).get(key), _facts(b).get(key)
                if x and y:
                    per_fact[key].append((x, y))
    agree['severity'] = _measure(severity)
    for key in FACTS:
        agree[key] = _measure(per_fact[key])

    rules = out['rules']
    for (run, task, role), rows in runs.dispatches.items():
        if not_an_attempt(role) or role != 'qa' or not rows or not mine(rows[0]):
            continue
        ended = sorted(_ending_qa(rows).items(), key=lambda item: item[0] or 0)
        attempts = [n for n, _ in ended]
        for name in RULES:
            release = _released(name)
            for n, index in ended:
                d = rows[index]['d']
                found = [f for f in d.get('findings') or [] if isinstance(f, dict)]
                rank = _threshold(runs, run, task, n)
                if decide(d['verdict'], found, rank) == 'fail' and decide(d['verdict'], found, rank, release) == 'pass':
                    item = rules[name]
                    item['would_pass'] += 1
                    # The first review this rule would have passed ends the task: the attempts after it did not need to run.
                    item['attempts_saved'] += sum(1 for m in attempts if (m or 0) > (n or 0))
                    for f in found:
                        if release(f) and not (f['severity'] > rank) and len(item['released']) < EXAMPLES:
                            item['released'].append(dict(run=run, task=task, attempt=n, severity='P%d' % f['severity'],
                                                         facts=_facts(f), head=f.get('head')))
                    break
        for n, index in ended:
            found = rows[index]['d'].get('findings') or []
            rules['reviews'] += 1
            rules['reviews_with_facts'] += any(_facts(f) for f in found if isinstance(f, dict))
    return out


def load(repo: str = None) -> dict:
    # Reading must not create a store: a fresh machine gets an empty report.
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return _empty()
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    return report(runs, repo=repo)


def _pct(n, d):
    return '%d%%' % round(100 * n / d) if d else '-'


def _line(name, m):
    if not m['compared']:
        return '  %-9s not compared yet' % name
    kappa = '' if m['kappa'] is None else ', kappa %.2f' % m['kappa']
    return '  %-9s %d of %d agree (%s%s)' % (name, m['agree'], m['compared'], _pct(m['agree'], m['compared']), kappa)


def main(args) -> int:
    rep = load(os.path.realpath(args.repo) if args.repo else None)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    cov, agree, rules = rep['coverage'], rep['agreement'], rep['rules']
    print('facts: %d of %d findings carry reach, origin and evidence (%s), in %d of %d reviews'
          % (cov['findings_with_facts'], cov['findings'], _pct(cov['findings_with_facts'], cov['findings']),
             cov['reviews_with_facts'], cov['reviews']))
    print('  (recorded in shadow: nothing in forge decides on them; advisory only)')
    for key in FACTS:
        if cov['values'][key]:
            print('  %-9s %s' % (key, '  '.join('%s %d' % kv for kv in cov['values'][key].items())))
    for sev, counts in cov['reach_by_severity'].items():
        print('  %-9s reach %s' % (sev, '  '.join('%s %d' % kv for kv in counts.items())))
    print('agreement: %d shadow pairs, %d findings both reviewers named (same file, lines within %d)'
          % (agree['pairs'], agree['matched'], rep['line_window']))
    for key in ('severity',) + tuple(FACTS):
        print(_line(key, agree[key]))
    print('rules: replayed on the review each of %d task attempts ended on (%d with facts)'
          % (rules['reviews'], rules['reviews_with_facts']))
    for name, label in RULES.items():
        item = rules[name]
        print('  %-9s %s: %d reviews would have passed, %d later attempts not run'
              % (name, label, item['would_pass'], item['attempts_saved']))
    shown = rules['both']['released']
    if shown:
        print('findings that would have stopped blocking (were they real bugs that mattered?):')
        for f in shown:
            facts = '/'.join(f['facts'].get(k) or '?' for k in FACTS)
            print('  %s %s#%s %s %s  %s' % (f['run'], f['task'], f['attempt'], f['severity'], facts, f['head']))
    return 0
