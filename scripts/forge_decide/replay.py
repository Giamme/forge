"""`forge decide replay`: re-ask a site's stored live requests on one provider and model, and
judge the answers against the free labels and the site's cheap baseline (GitHub #37).

    plan(site, provider, model, spec, *, runs=None) -> Plan which requests, what is answered
    estimate(plan) -> dict                                   the dry run's count, tokens, cost
    send_all(plan, *, workers=3) -> dict                     ask every unanswered request
    evaluate(plan, *, seed=7, margin=0.02) -> dict           AUCs, interval, precision, promotes
    record(gate, result) / recorded(gate, provider, model)   the result the configuration reads

Answers are appended to state_dir()/replay/<site>/<provider>/<model>.jsonl, one line per
attempt ({key, answers, usage, at} or {key, reason, at}); a key already answered there is not
sent again, so a replay resumes. A failed key is asked again by the next --send.

Requests go through forge_jev.client.send: never the budget, the cache, jev.jsonl or the
dataset. Replays are deliberate, user-run spend. The privacy floor is read per request, from
the repo the store recorded for it, not from the working directory.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote

from . import _save, providers, state_dir
from . import labels, metrics, sites

VERSION = 1
# A replay is not waiting on a dispatch: a short live deadline would only lose answers.
MIN_DEADLINE_S = 30.0


@dataclass
class Plan:
    site: str
    provider: str
    model: str
    spec: dict
    rows: list                       # labels.collect rows that may be sent
    usage: dict                      # key -> the stored live usage, when the store has it
    skipped_privacy: int = 0
    answered: dict = field(default_factory=dict)    # key -> answers already in the replay file


def _part(name: str) -> str:
    """A path component: a Clef model is `@cf/meta/...`, which must not open directories."""
    return quote(name, safe='') or '_'


def answers_path(site: str, provider: str, model: str) -> Path:
    return state_dir() / 'replay' / _part(site) / _part(provider) / (_part(model) + '.jsonl')


def record_path(gate: str) -> Path:
    return state_dir() / 'replays' / (_part(gate) + '.json')


def _private_dir(path: Path) -> None:
    """`path` and its missing parents, each created 0700."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)


def answered(path: Path) -> dict:
    """{key: answers} of the replay file; a later answer of a key wins, failures do not count."""
    out = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue   # a line cut by an interrupted write
        if isinstance(entry, dict) and entry.get('key') and isinstance(entry.get('answers'), dict):
            out[entry['key']] = entry['answers']
    return out


def _newest_runs(events, n: int) -> set:
    """The n dataset runs whose first stored request is newest."""
    first = {}
    for event in events:
        first[event['run']] = min(first.get(event['run'], event['ts']), event['ts'])
    return set(sorted(first, key=lambda run: (first[run], str(run)), reverse=True)[:n])


def plan(site: str, provider: str, model: str, spec: dict, *, runs: int | None = None, store=None) -> Plan:
    """The labelled live requests of `site` (newest `runs` dataset runs only, when given), less
    those whose own repo has the privacy floor, and which of them the replay file answers."""
    store = store if store is not None else labels._open_store()
    rows, usage, skipped = [], {}, 0
    if store is not None:
        stored = labels.requests(store, site)
        usage = {event['d'].get('key'): event['d'].get('usage') for event, _body in stored}
        keep = _newest_runs([event for event, _body in stored], runs) if runs else None
        floors = {}
        for row in labels.collect(site, store=store, runs=keep):
            path = store.repo_paths.get(row['repo'])
            # No recorded path, no floor: privacy_floor(None) would read the working directory's.
            if path not in floors:
                floors[path] = bool(path) and providers.privacy_floor(path)
            if floors[path]:
                skipped += 1
                continue
            rows.append(row)
    found = answered(answers_path(site, provider, model))
    return Plan(site=site, provider=provider, model=model, spec=spec, rows=rows, usage=usage,
                skipped_privacy=skipped, answered={row['key']: found[row['key']] for row in rows if row['key'] in found})


def _input_tokens(plan_: Plan, row: dict) -> int:
    usage = plan_.usage.get(row['key'])
    value = usage.get('input_tokens') if isinstance(usage, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value)
    return len(json.dumps(row['body'], sort_keys=True, separators=(',', ':'))) // 4


def pending(plan_: Plan) -> list:
    return [row for row in plan_.rows if row['key'] not in plan_.answered]


def estimate(plan_: Plan) -> dict:
    """What --send would send: requests, answered, to send, input tokens, cost (None unpriced)."""
    todo = pending(plan_)
    tokens = sum(_input_tokens(plan_, row) for row in todo)
    price = plan_.spec.get('price_usd_per_mtok')
    return dict(site=plan_.site, provider=plan_.provider, model=plan_.model, requests=len(plan_.rows),
                answered=len(plan_.answered), to_send=len(todo), skipped_privacy=plan_.skipped_privacy,
                input_tokens=tokens, cost_usd=None if price is None else tokens * price / 1e6)


def _cut(path: Path) -> bool:
    """Whether `path` ends inside a line."""
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b'\n'
    except OSError:
        return False


def send_all(plan_: Plan, *, workers: int = 3, send=None) -> dict:
    """Ask every unanswered request on the plan's spec, `workers` at a time, appending one line
    per answer or failure. {sent, answered, failed: {reason: count}}; plan_.answered grows."""
    if send is None:
        from forge_jev import client
        send = client.send
    path = answers_path(plan_.site, plan_.provider, plan_.model)
    _private_dir(path.parent)
    lock = threading.Lock()
    failed = {}
    deadline = max(float(plan_.spec.get('deadline_s') or 0.0), MIN_DEADLINE_S)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)
    if _cut(path):
        os.write(fd, b'\n')   # end a line cut by an interrupted write, so the next answer stays whole

    def one(row):
        body = row['body']
        result, reason, usage = send(plan_.spec, body.get('state'), body.get('questions') or {}, deadline_s=deadline)
        if result is not None:
            entry = dict(key=row['key'], answers=result['answers'], usage=usage, at=time.time())
        else:
            entry = dict(key=row['key'], reason=reason or 'no answer', at=time.time())
        with lock:
            os.write(fd, (json.dumps(entry) + '\n').encode())
            if result is not None:
                plan_.answered[row['key']] = result['answers']
            else:
                failed[entry['reason']] = failed.get(entry['reason'], 0) + 1

    todo = pending(plan_)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(one, todo))
    finally:
        os.close(fd)
    return dict(sent=len(todo), answered=len(todo) - sum(failed.values()), failed=failed)


def rows(plan_: Plan) -> list:
    """One row per labelled decision of an answered request."""
    record = sites.site(plan_.site)
    out = []
    for row in plan_.rows:
        answers = plan_.answered.get(row['key'])
        if answers is None:
            continue
        for qid, label in row['labels'].items():
            replay = record.score(answers, qid, row['body'])
            out.append(dict(run=row['run'], ts=row['ts'], key=row['key'], qid=qid, label=bool(label),
                            replay=None if replay is None else float(replay),
                            baseline=float(record.baseline(row['body'], qid)),
                            live=row['live'].get(qid), live_model=row.get('model')))
    return out


def _promotes(judged, interval, *, margin, floor, threshold):
    """metrics.promotes, judged on `interval` (paired_bootstrap at --seed) instead of the
    default seed's, so the recorded verdict and the reported interval always agree."""
    labelled = [row for row in judged if row.get('label') is not None]
    runs = {row['run'] for row in labelled}
    if len(labelled) < metrics.MIN_DECISIONS or len(runs) < metrics.MIN_RUNS:
        return metrics.promotes(labelled)   # its count reasons; it fails before any bootstrap
    if interval is None:
        return False, 'the replay and the baseline cannot be compared (a class is missing or too few resamples)'
    if not interval[0] > -margin:
        return False, (f'replay is worse than the cheap baseline: AUC difference lower bound '
                       f'{interval[0]:.3f} is not above -{margin}')
    if floor is not None:
        if threshold is None:
            return False, 'the precision floor needs a threshold'
        got = metrics.precision_at([(row['replay'], row['label']) for row in labelled
                                    if row.get('replay') is not None], threshold)['precision']
        if got is None:
            return False, f'the replay flags nothing at {threshold}, so precision cannot reach {floor}'
        if got < floor:
            return False, f'precision {got:.3f} at {threshold} is below the floor {floor}'
    return True, 'passes'


def evaluate(plan_: Plan, *, seed: int = 7, margin: float = 0.02) -> dict:
    """The replay judged on the newer half of the runs, against the baseline and the live answers.

    The threshold is the one the score is compared with: an inverted site (sites.Site.invert)
    flags a score at or above 1 - threshold; `threshold_name` stays the config name."""
    import forge_jev
    record = sites.site(plan_.site)
    _calibrate, judged = metrics.split(rows(plan_))
    live = {}
    for row in judged:
        if row['live'] is not None:
            live.setdefault(row['live_model'] or '-', []).append((row['live'], row['label']))
    threshold = (forge_jev.threshold(record.threshold, site=plan_.site, model=plan_.model)
                 if record.threshold else None)
    if threshold is not None and record.invert:
        threshold = 1.0 - threshold
    answered_pairs = [(r['replay'], r['label']) for r in judged if r['replay'] is not None]
    precision = (metrics.precision_at(answered_pairs, threshold) if threshold is not None
                 else dict(precision=None, recall=None, flagged=None))
    interval = metrics.paired_bootstrap(judged, metrics._difference, seed=seed) if judged else None
    passes, reason = _promotes(judged, interval, margin=margin, floor=record.floor, threshold=threshold)
    return dict(at=time.time(), site=plan_.site, gate=record.gate, provider=plan_.provider, model=plan_.model,
                passes=passes, reason=reason,
                judged=dict(decisions=len(judged), runs=len({r['run'] for r in judged}),
                            positives=sum(r['label'] for r in judged)),
                auc=dict(replay=metrics.auc(answered_pairs),
                         baseline=metrics.auc([(r['baseline'], r['label']) for r in judged]),
                         live={model: metrics.auc(pairs) for model, pairs in sorted(live.items())}),
                diff=dict(value=metrics._difference(judged), ci=list(interval) if interval else None),
                threshold=threshold, threshold_name=record.threshold, precision=precision['precision'],
                recall=precision['recall'], flagged=precision['flagged'], floor=record.floor,
                seed=seed, margin=margin, skipped_privacy=plan_.skipped_privacy,
                requests=len(plan_.rows), answered=len(plan_.answered))


_RECORDED = ('at', 'site', 'provider', 'model', 'passes', 'reason', 'judged', 'auc', 'diff', 'threshold',
             'precision', 'floor', 'seed', 'margin', 'threshold_name', 'recall', 'flagged')


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(gate: str, result: dict) -> Path:
    """Store `result` under results["<provider>/<model>"] of replays/<gate>.json (0600, atomic).

    The read-modify-write holds flock on a sibling .lock, so two replays of one gate cannot each
    save a snapshot that drops the other's entry."""
    path = record_path(gate)
    _private_dir(path.parent)
    with open(f'{path}.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(path)
        results = data.get('results') if isinstance(data.get('results'), dict) else {}
        results['%s/%s' % (result['provider'], result['model'])] = {k: result[k] for k in _RECORDED if k in result}
        _save(path, dict(data, version=VERSION, results=results), 0o600)
    return path


def recorded(gate: str, provider: str, model: str) -> dict | None:
    """The recorded replay of `gate` on provider/model, or None."""
    results = _load(record_path(gate)).get('results')
    entry = results.get('%s/%s' % (provider, model)) if isinstance(results, dict) else None
    return entry if isinstance(entry, dict) else None


def _number(value, digits=3) -> str:
    return '-' if value is None else f'{value:.{digits}f}'


def render_estimate(data: dict) -> str:
    cost = '-' if data['cost_usd'] is None else f"${data['cost_usd']:.4f}"
    return '\n'.join([
        f"replay {data['site']} on {data['provider']}/{data['model']} (dry run: nothing sent; --send asks)",
        f"  requests       {data['requests']}",
        f"  answered       {data['answered']}",
        f"  to send        {data['to_send']}",
        f"  privacy floor  {data['skipped_privacy']} skipped",
        f"  input tokens   ~{data['input_tokens']}",
        f"  cost           {cost}",
    ])


def render(result: dict) -> str:
    judged = result['judged']
    ci = result['diff']['ci']
    lines = [
        f"replay {result['site']} on {result['provider']}/{result['model']}: "
        f"{result['answered']}/{result['requests']} requests answered, {result['skipped_privacy']} skipped "
        f"(privacy floor)",
        f"judged half: {judged['decisions']} decisions, {judged['positives']} positive, {judged['runs']} runs",
        '',
        f"  {'scorer':<28} {'AUC':>6}",
        f"  {'replay ' + result['model']:<28} {_number(result['auc']['replay']):>6}",
        f"  {'baseline':<28} {_number(result['auc']['baseline']):>6}",
    ]
    for model, value in result['auc']['live'].items():
        lines.append(f"  {'live ' + model:<28} {_number(value):>6}")
    lines += [
        '',
        f"  replay - baseline  {_number(result['diff']['value'])}  95% CI "
        + ('-' if ci is None else f'[{ci[0]:.3f}, {ci[1]:.3f}]') + f"  (margin {result['margin']})",
    ]
    if result['threshold'] is not None:
        lines.append(f"  at {result['threshold_name']} {result['threshold']}: precision "
                     f"{_number(result['precision'])}, recall {_number(result['recall'])}, "
                     f"{result['flagged']} flagged (floor {_number(result['floor'], 2)})")
    lines += ['', ('PASSES: ' if result['passes'] else 'does not pass: ') + result['reason']]
    return '\n'.join(lines)
