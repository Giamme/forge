"""`forge decide report`: what each decision-model site cost and how often it was right (#37).

    collect_runs(...)  which run dirs to read: --run DIRs, else the newest N of the repo's
                       run registry (every repo's with --all), newest first by mtime
    summarize(...)     one record per site over every jev.jsonl of those runs
    render(...)        the text table

Each run dir's jev.jsonl files are the plan or run dir's own, `tasks/*/` and `integrate-*/`;
a path with a `jev-backtests` component is never read. Per site:

    calls, cached, errors, skipped: {reason: n}
        every entry is a call; a failed one with a reason is a skip, one without is an error
    input_tokens, output_tokens
        from the entry's usage
    cost_usd
        tokens x the user decide.json price of the entry's provider; None when any entry
        names a provider with no price
    fired, fired_unknown
        calls the site's `fired` turns into a non-empty list at the threshold for the entry's
        model. verify-discover needs the stored request body, looked up in the dataset by
        `key`; a call whose body is not there counts under fired_unknown instead
    labelled: {decisions, fired_right, fired_wrong, missed}
        labels.collect's labelled decisions of these runs (run uid = first line of the run
        dir's dataset.uid), joined to the run's entry by key; None for a site with no label
    mode
        the gate's resolved mode for the current repo

A site with no registry entry (retired: jev-score-plan, clef-first-pass, ...) gets calls,
tokens and cost; its fired, labelled and mode are None.

Read-only: the dataset store is opened read-only, and nothing here asks a model.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path

from . import load_user
from . import labels as labels_module
from . import sites as sites_module
from .resolve import resolve

DEFAULT_RUNS = 10
BACKTESTS = 'jev-backtests'
COUNTS = ('calls', 'cached', 'errors', 'input_tokens', 'output_tokens', 'fired', 'fired_unknown')
LABELLED = ('decisions', 'fired_right', 'fired_wrong', 'missed')


# ------------------------------------------------------------------------------- runs

def _registered(path: Path) -> list[str]:
    """The run dirs one registry file lists (absolute paths, comments skipped)."""
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines
            if line.strip() and not line.startswith('#') and os.path.isabs(line.strip())]


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def collect_runs(*, repo=None, dirs=(), every=False, limit=DEFAULT_RUNS) -> tuple[list[str], int]:
    """(run dirs newest first, how many listed dirs no longer exist).

    `dirs` names the runs directly and ignores the registry. Otherwise the registry of
    `repo`, or every repo's with `every`, gives the dirs, of which the newest `limit` are kept.
    """
    import forge_registry
    if dirs:
        listed = [os.path.abspath(d) for d in dirs]
        limit = None
    elif every:
        root = forge_registry.registry_dir()
        files = sorted(root.glob('*.txt')) if root.is_dir() else []
        listed = [d for path in files for d in _registered(path)]
    else:
        listed = _registered(forge_registry.runs_registry(repo))
    seen, present, missing = set(), [], 0
    for d in listed:
        key = os.path.realpath(d)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(d):
            present.append(d)
        else:
            missing += 1
    if limit is not None:
        present = sorted(present, key=_mtime, reverse=True)[:limit]
    return present, missing


def jev_logs(run_dir) -> list[Path]:
    """The run dir's jev.jsonl files: its own, tasks/*/ and integrate-*/, none under
    jev-backtests."""
    root = Path(os.path.abspath(run_dir))
    found = [root / 'jev.jsonl', *sorted(root.glob('tasks/*/jev.jsonl')), *sorted(root.glob('integrate-*/jev.jsonl'))]
    return [path for path in found if path.is_file() and BACKTESTS not in path.parts]


def run_uid(run_dir) -> str | None:
    """The run's dataset uid: the first line of its dataset.uid."""
    try:
        with open(Path(run_dir) / 'dataset.uid', encoding='utf-8', errors='replace') as handle:
            return handle.readline().strip() or None
    except OSError:
        return None


def _entries(path: Path):
    try:
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


# ------------------------------------------------------------------------------- summary

def _at(entry: dict) -> float:
    """The entry's log time; an entry without one sorts last."""
    value = entry.get('at')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float('inf')


def _tokens(usage, field) -> int:
    value = usage.get(field) if isinstance(usage, dict) else None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def _prices() -> dict:
    """{provider: USD per million tokens} from the user decide.json."""
    stored = load_user()[0].get('providers')
    out = {}
    for name, entry in (stored.items() if isinstance(stored, dict) else ()):
        price = entry.get('price_usd_per_mtok') if isinstance(entry, dict) else None
        if isinstance(price, (int, float)) and not isinstance(price, bool) and price >= 0:
            out[name] = float(price)
    return out


class _Store:
    """The dataset store, opened read-only once, on first use; None when there is none."""

    def __init__(self):
        self._store = False
        self._bodies = None

    def get(self):
        if self._store is False:
            try:
                self._store = labels_module._open_store()
            except Exception:
                self._store = None
        return self._store

    def body(self, key) -> dict | None:
        """The stored request body of a request key."""
        if self._bodies is None:
            self._bodies = {}
            store = self.get()
            for event in (store.of('jev') if store is not None else ()):
                d = event['d']
                if d.get('key') and d.get('key') not in self._bodies:
                    body = labels_module._body(store, (d.get('blobs') or {}).get('request'))
                    if body is not None:
                        self._bodies[d['key']] = body
        return self._bodies.get(key)


def _blank(retired: bool) -> dict:
    row = dict(calls=0, cached=0, errors=0, skipped={}, input_tokens=0, output_tokens=0, cost_usd=0.0,
               fired=0, fired_unknown=0, labelled=None, mode=None, retired=retired)
    if retired:
        row.update(fired=None, fired_unknown=None)
    return row


def summarize(run_dirs, *, repo=None, site=None) -> dict:
    """{runs: [{dir, uid}], sites: {site: record}, totals: record} over the run dirs
    (record shape in the module docstring). `site` keeps one site only."""
    import forge_jev
    forge_jev.use_repo(repo)
    prices = _prices()
    store = _Store()
    thresholds = {}
    rows: dict = {}
    # (site, run uid, key) -> (at, fired list) of the run's earliest answered entry for that
    # key by `at`, the request labels.collect labels (the first per key by ts)
    answered: dict = {}
    runs = []
    for run_dir in run_dirs:
        uid = run_uid(run_dir)
        runs.append(dict(dir=str(run_dir), uid=uid))
        for path in jev_logs(run_dir):
            for entry in _entries(path):
                name = str(entry.get('site') or 'unknown')
                if site is not None and name != site:
                    continue
                record = sites_module.site(name)
                row = rows.get(name)
                if row is None:
                    row = rows[name] = _blank(record is None)
                _count(row, entry, prices)
                if record is None:
                    continue
                fired = _fired(record, entry, thresholds, store)
                if fired is None:
                    row['fired_unknown'] += 1
                elif fired:
                    row['fired'] += 1
                key = entry.get('key')
                if fired is not None and entry.get('ok') is True and uid and key:
                    at = _at(entry)
                    held = answered.get((name, uid, key))
                    if held is None or at < held[0]:
                        answered[(name, uid, key)] = (at, set(fired))

    if site is not None and site not in rows and sites_module.site(site) is not None:
        rows[site] = _blank(False)
    uids = {run['uid'] for run in runs if run['uid']}
    resolved = resolve(repo)['gates']
    for name, row in rows.items():
        record = sites_module.site(name)
        if record is None:
            continue
        gate = resolved.get(record.gate)
        row['mode'] = gate['mode'] if gate else None
        if record.label is not None:
            row['labelled'] = _labelled(name, uids, answered, store)
    ordered = dict(sorted(rows.items(), key=lambda item: (-item[1]['input_tokens'], item[0])))
    return dict(runs=runs, sites=ordered, totals=_totals(ordered.values()))


def _count(row: dict, entry: dict, prices: dict) -> None:
    row['calls'] += 1
    if entry.get('cached') is True:
        row['cached'] += 1
    if entry.get('ok') is not True:
        reason = entry.get('reason')
        if reason:
            row['skipped'][str(reason)] = row['skipped'].get(str(reason), 0) + 1
        else:
            row['errors'] += 1
    usage = entry.get('usage')
    tokens_in, tokens_out = _tokens(usage, 'input_tokens'), _tokens(usage, 'output_tokens')
    row['input_tokens'] += tokens_in
    row['output_tokens'] += tokens_out
    if row['cost_usd'] is not None:
        price = prices.get(entry.get('provider'))
        row['cost_usd'] = None if price is None else row['cost_usd'] + (tokens_in + tokens_out) * price / 1e6


def _fired(record, entry: dict, thresholds: dict, store: _Store) -> list | None:
    """The question ids the entry's answers fired; None when that cannot be known."""
    answers = entry.get('answers')
    if entry.get('ok') is not True or not isinstance(answers, dict):
        return []
    model = entry.get('model')
    if (record.name, model) not in thresholds:
        import forge_jev
        thresholds[(record.name, model)] = (
            forge_jev.threshold(record.threshold, site=record.name, model=model) if record.threshold else None)
    value = thresholds[(record.name, model)]
    if record.threshold and value is None:
        return None
    request = entry.get('questions') if isinstance(entry.get('questions'), list) else []
    if record.name == 'jev-verify-discover':
        request = store.body(entry.get('key'))
        if request is None:
            return None
    fired = record.fired(answers, value, request)
    return None if fired is None else list(fired)


def _labelled(name: str, uids: set, answered: dict, store: _Store) -> dict:
    out = dict.fromkeys(LABELLED, 0)
    data = store.get() if uids else None
    if data is None:
        return out
    for row in labels_module.collect(name, store=data, runs=uids):
        held = answered.get((name, row['run'], row['key']))
        if held is None:
            continue
        fired = held[1]
        for qid, truth in row['labels'].items():
            out['decisions'] += 1
            if qid in fired:
                out['fired_right' if truth else 'fired_wrong'] += 1
            elif truth:
                out['missed'] += 1
    return out


def _totals(rows) -> dict:
    rows = list(rows)
    out = {field: sum(row[field] or 0 for row in rows) for field in COUNTS}
    skipped = Counter()
    for row in rows:
        skipped.update(row['skipped'])
    out['skipped'] = dict(sorted(skipped.items()))
    costs = [row['cost_usd'] for row in rows]
    out['cost_usd'] = None if any(cost is None for cost in costs) else sum(costs)
    labelled = [row['labelled'] for row in rows if row['labelled'] is not None]
    out['labelled'] = {field: sum(item[field] for item in labelled) for field in LABELLED}
    return out


# ------------------------------------------------------------------------------- text

COLUMNS = ('site', 'mode', 'calls', 'cached', 'errors', 'skipped', 'in tokens', 'out tokens', 'cost $',
           'fired', 'right', 'wrong', 'missed')


def _cells(name: str, row: dict, mode=True) -> list[str]:
    def number(value):
        return '-' if value is None else f'{value:,}'
    labelled = row.get('labelled')
    fired = number(row.get('fired'))
    if row.get('fired_unknown'):
        fired += f' (+{row["fired_unknown"]}?)'
    return [name, (row.get('mode') or '-') if mode else '', number(row['calls']), number(row['cached']),
            number(row['errors']), number(sum(row['skipped'].values())), number(row['input_tokens']),
            number(row['output_tokens']), '-' if row['cost_usd'] is None else '%.4f' % row['cost_usd'], fired,
            *(number(labelled[field]) if labelled else '-' for field in LABELLED[1:])]


def render(data: dict) -> str:
    """One row per site (as summarize ordered them), a totals row, then the runs read."""
    table = [list(COLUMNS)] + [_cells(name, row) for name, row in data['sites'].items()]
    table.append(_cells('total', data['totals'], mode=False))
    widths = [max(len(line[i]) for line in table) for i in range(len(COLUMNS))]
    lines = ['  '.join(cell.ljust(widths[i]) if i < 2 else cell.rjust(widths[i]) for i, cell in enumerate(line)).rstrip()
             for line in table]
    skips = data['totals']['skipped']
    if skips:
        lines.append('skipped: ' + ', '.join(f'{reason} {n}' for reason, n in skips.items()))
    runs = data['runs']
    gone = data.get('skipped_runs') or 0
    lines.append('runs read: %d%s%s' % (len(runs), ' (%d listed dirs no longer exist)' % gone if gone else '',
                                         ': ' + ', '.join(run['dir'] for run in runs) if runs else ''))
    return '\n'.join(lines)
