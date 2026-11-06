#!/usr/bin/env python3
"""`forge-memory.sh learnings <repo> --json`: every learning in a repo's ledger and its state (#126).

forge-memory.sh runs its own `rebuild` into a scratch dir first, with MEMORY_DROPS recording what it dropped
and why; this reads the ledger, that rebuilt memory and the drops, and says per learning what the rebuild did,
so the state shown is the one forge's bash decided, never a second implementation of it:

  in_memory  the rebuilt memory.md has it
  waiting    a finding seen in one run only: it needs a second run to be promoted
  stale      its [anchor] path no longer exists in the repo
  capped     trimmed by the memory cap (FORGE_MEMORY_MAX_LINES / MAX_BYTES)
  muted      every row of it was marked not injectable (Jev's curation)

A learning is a (category, key) group, as rebuild groups the ledger: `count` is the number of distinct runs
it appeared in, `text` the latest wording. `memory` is the repo's current .forge/memory.md by section, with
the roles each section reaches; `rebuilt_differs` says the current file is not what a rebuild makes now (an
older forge wrote it, or a `prune` asked Jev to drop facts). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SECTIONS = (('verify', 'Verify', ('planner', 'dwarf')),
            ('trap', 'Known traps', ('planner', 'dwarf', 'qa')),
            ('finding', 'Recurring QA findings', ('planner', 'dwarf', 'qa')))
_TITLE = {'## ' + title: (cat, roles) for cat, title, roles in SECTIONS}
_FACT = re.compile(r'^- (?:([0-9]+)x: )?(.*)$')
_MAX_READ = 32 << 20


def _read(path):
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _MAX_READ:
            return None
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def parse_memory(text):
    """[{category, title, roles, lines: [{text, count}]}] of a memory.md, in file order."""
    out, current = [], None
    for line in (text or '').splitlines():
        if line.startswith('## '):
            found = _TITLE.get(line.strip())
            current = None
            if found:
                current = dict(category=found[0], title=line[3:].strip(), roles=list(found[1]), lines=[])
                out.append(current)
            continue
        fact = _FACT.match(line)
        if fact and current is not None:
            current['lines'].append(dict(text=fact.group(2), count=int(fact.group(1)) if fact.group(1) else None))
    return out


def ledger_groups(text):
    """{(category, key): group} as rebuild groups the ledger, plus every learning row."""
    groups = {}
    for line in (text or '').splitlines():
        if not line or line.startswith('#'):
            continue
        cols = line.split('\t')
        if len(cols) < 9:
            continue
        ts, run, task, role, model, verdict, cat, key, txt = cols[:9]
        if cat == '-' or key == '-':
            continue                                  # a dispatch that emitted no learning
        injectable = not (len(cols) >= 11 and cols[10] == '0')
        g = groups.setdefault((cat, key), dict(category=cat, key=key, rows=[], runs=set(), last_ts=None, text=None,
                                               any_injectable=False))
        g['rows'].append(dict(ts=ts, run_id=run, task=task, role=role, model=model, verdict=verdict, text=txt,
                              injectable=injectable))
        if injectable:
            g['any_injectable'] = True
            g['runs'].add(run)
            # awk's `ts >= lastts[k]` on strings: the later row of two with the same time wins
            if g['last_ts'] is None or ts >= g['last_ts']:
                g['last_ts'], g['text'] = ts, txt
    return groups


def drops_of(text):
    """{(reason, category or '-', text)} of rebuild's MEMORY_DROPS rows; cap rows hold the whole '- ...' line."""
    out = set()
    for line in (text or '').splitlines():
        reason, _, rest = line.partition('\t')
        cat, _, fact = rest.partition('\t')
        if reason == 'cap':
            m = _FACT.match(fact)
            fact = m.group(2) if m else fact
        out.add((reason, cat, fact))
    return out


def classify(groups, rebuilt, drops):
    kept = {(s['category'], line['text']) for s in rebuilt for line in s['lines']}
    capped = {fact for reason, _cat, fact in drops if reason == 'cap'}
    stale = {(cat, fact) for reason, cat, fact in drops if reason in ('anchor_missing', 'jev_stale')}
    out = []
    for (cat, key), g in groups.items():
        if not g['any_injectable']:
            state, text, count = 'muted', g['rows'][-1]['text'], 0
        else:
            text, count = g['text'], len(g['runs'])
            if (cat, text) in kept:
                state = 'in_memory'
            elif (cat, text) in stale:
                state = 'stale'
            elif text in capped:
                state = 'capped'
            elif cat == 'finding' and count < 2:
                state = 'waiting'
            else:
                state = 'dropped'                     # not reached by today's rebuild; kept honest if it changes
        rows = g['rows']
        out.append(dict(category=cat, key=key, text=text, count=count, state=state,
                        first=min(r['ts'] for r in rows), last=max(r['ts'] for r in rows), rows=rows))
    out.sort(key=lambda r: (r['last'], r['category'], r['key']), reverse=True)
    return out


def build(repo, ledger, memory, rebuilt, drops, max_lines, max_bytes, memory_off):
    ledger_text, current, made = _read(ledger), _read(memory), _read(rebuilt)
    groups = ledger_groups(ledger_text)
    rebuilt_sections = parse_memory(made)
    return dict(repo=repo, ledger=ledger_text is not None, memory_off=memory_off, max_lines=max_lines,
                max_bytes=max_bytes, memory=parse_memory(current) if current is not None else None,
                rebuilt_differs=(current or '') != (made or ''),
                learnings=classify(groups, rebuilt_sections, drops_of(_read(drops))))


def main(argv=None):
    p = argparse.ArgumentParser(prog='forge-memory.sh learnings')
    for name in ('repo', 'ledger', 'memory', 'rebuilt', 'drops'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--max-lines', type=int, required=True)
    p.add_argument('--max-bytes', type=int, required=True)
    p.add_argument('--memory-off', choices=('0', '1'), default='0')
    a = p.parse_args(argv)
    json.dump(build(a.repo, a.ledger, a.memory, a.rebuilt, a.drops, a.max_lines, a.max_bytes, a.memory_off == '1'),
              sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
