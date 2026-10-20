"""QA findings, parsed by forge's own contract module (scripts/forge-contract.py), never re-implemented."""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import re
import threading


# build_id() hashes this package only, not forge-contract.py: a server keeps the parser it loaded
# first, and an edit to the contract is picked up by the next server, not by a running one.
CONTRACT = Path(__file__).resolve().parents[2] / 'scripts' / 'forge-contract.py'
_LOCK = threading.Lock()
_STATE = {}
# The contract reads whole replies; this only bounds a pathological file.
_MAX_CHARS = 1000000
_SUMMARY_MAX = 240
_TEXT_MAX = 20000
_GATE = ('threshold', 'decision', 'worst', 'n_confirmed', 'n_unlabelled', 'n_below')


class _ReadOnlyLoader(importlib.machinery.SourceFileLoader):
    def set_data(self, *args, **kwargs):
        pass  # forge-ui writes nothing, not even a .pyc beside forge's scripts


def _load():
    with _LOCK:
        if 'module' not in _STATE:
            try:
                spec = importlib.util.spec_from_file_location(
                    'forge_contract_for_ui', CONTRACT, loader=_ReadOnlyLoader('forge_contract_for_ui', str(CONTRACT)))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not (callable(module.parse_findings) and hasattr(module._LOCATION, 'search')):
                    raise TypeError('not the forge contract module')
                _STATE['module'] = module
            except Exception:  # a missing or broken contract only turns findings off
                _STATE['module'] = None
        return _STATE['module']


def available() -> bool:
    return _load() is not None


# forge_verdict in scripts/forge-artifact.sh, as forge_dataset.harvest.verdict_of also ports it: only the
# reply's last non-blank line counts, and only as exactly `FORGE_VERDICT: PASS|FAIL`, optionally followed by
# a dash and a gloss. A verdict anywhere else (quoted, followed by more text) is no verdict, as for forge.
_VERDICT = re.compile(r'^FORGE_VERDICT: (PASS|FAIL)([ \t]+(—|–|-)( .*)?)?[ \t]*$')


def verdict(text):
    """PASS, FAIL or None, exactly as forge decides it: from the reply's last non-blank line only.

    None means forge recorded no verdict (UNKNOWN), which forge's gate treats as blocking."""
    lines = [line for line in (text or '').splitlines() if line.strip()]
    match = _VERDICT.match(lines[-1].rstrip('\r')) if lines else None
    return match.group(1) if match else None


def _parse(text):
    module = _load()
    if module is None or not text:
        return None, None
    try:
        found = []
        for item in module.parse_findings(text[:_MAX_CHARS]):
            severity = item.severity if type(item.severity) is int and 0 <= item.severity <= 3 else None
            if item.status in ('CONFIRMED', 'PLAUSIBLE', 'UNSTATED') and isinstance(item.text, str):
                found.append((item.status, severity, item.line if type(item.line) is int else None, item.text))
        return module, found
    except Exception:
        return None, None


def summarize(text):
    """FindingsSummary of a reply, or None when there is no text or no contract module."""
    found = _parse(text)[1]
    if found is None:
        return None
    result = dict(P0=0, P1=0, P2=0, P3=0, unlabelled=0, plausible=0, confirmed=0, worst=None)
    for status, severity, _, _ in found:
        if status == 'CONFIRMED':
            result['confirmed'] += 1
            if severity is None:
                result['unlabelled'] += 1
            else:
                result['P%d' % severity] += 1
        elif status == 'PLAUSIBLE':
            result['plausible'] += 1
        else:
            result['unlabelled'] += 1  # UNSTATED counts as unlabelled, as in the gate
    labelled = [severity for status, severity, _, _ in found if status == 'CONFIRMED' and severity is not None]
    result['worst'] = 'P%d' % min(labelled) if labelled else None
    return result


def listing(text, limit=200):
    """The first `limit` Findings of a reply, or None when there is no text or no contract module."""
    module, found = _parse(text)
    if found is None:
        return None
    result = []
    for status, severity, line, block in found[:limit]:
        first = block.split('\n', 1)[0]
        try:
            match = module._LOCATION.search(first)
        except Exception:
            match = None
        result.append(dict(severity=None if severity is None else 'P%d' % severity, status=status,
                           location=match.group(0) if match else None, line=line,
                           summary=first.strip()[:_SUMMARY_MAX],
                           text=block if len(block) <= _TEXT_MAX else block[:_TEXT_MAX] + '…'))
    return result


def parse_gate(text):
    """A qa.gate line: threshold, decision, worst, n_confirmed, n_unlabelled, n_below; None if malformed."""
    line = next((item for item in (text or '').splitlines() if item.strip()), None)
    parts = line.rstrip('\r').split('\t') if line else []
    if len(parts) < len(_GATE):
        return None
    result = {}
    for key, value in zip(_GATE, parts):
        value = value.strip()
        if key.startswith('n_'):
            result[key] = int(value) if re.fullmatch(r'[0-9]{1,9}', value) else None
        else:
            result[key] = None if value in ('', '-') else value
    return result
