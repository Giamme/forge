"""Stdlib-only TypeSafe HTTP client; every failure degrades to None, never raises."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from . import DEFAULTS, acting, api_key, load_config, redact, shadow, write_json

_BACKOFFS = (0.2, 0.4, 0.8)
_RETRYABLE_CODES = (429, 529)


def _skip(run_dir: Path | str | None, site: str, reason: str) -> None:
    if run_dir is None:
        return
    try:
        with (Path(run_dir) / 'jev.skip').open('a') as handle:
            handle.write(f'{site}: {reason}\n')
            handle.flush()
    except OSError:
        pass


def _log(run_dir, site, key, ok, reason, latency_s, model, usage, questions, answers) -> None:
    if run_dir is None:
        return
    entry = dict(at=time.time(), site=site, key=key, ok=ok, reason=reason, latency_s=latency_s,
                model=model, usage=usage, questions=questions, answers=answers,
                acting=acting(), shadow=shadow())
    try:
        with (Path(run_dir) / 'jev.jsonl').open('a') as handle:
            handle.write(json.dumps(entry) + '\n')
            handle.flush()
    except OSError:
        pass
    if not ok:
        _skip(run_dir, site, reason)


def ask(state, questions: dict, *, site: str, run_dir=None, config: dict | None = None,
        deadline_s: float | None = None) -> dict | None:
    config = config or load_config()
    key = api_key(config)
    qids = list(questions)
    started = time.time()

    def done(ok, reason, result, model=None, usage=None, latency=None):
        _log(run_dir, site, fixture_key, ok, reason, latency if latency is not None else time.time() - started,
             model, usage, qids, result['answers'] if result else None)
        return result

    fixture_key = None
    body = dict(state=state, model=config.get('model', DEFAULTS['model']), questions=questions)
    try:
        canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    except (TypeError, ValueError) as error:
        # A caller that built state out of something unserializable gets today's behaviour,
        # not a traceback from inside a dispatch.
        return done(False, 'unserializable-state: ' + str(error), None)
    fixture_key = hashlib.sha256(canonical).hexdigest()

    if not key:
        return done(False, 'no-key', None)

    fixtures_dir = os.environ.get('FORGE_JEV_FIXTURES')
    if fixtures_dir:
        try:
            payload = json.loads((Path(fixtures_dir) / (fixture_key + '.json')).read_text())
        except (OSError, ValueError):
            strict = bool(os.environ.get('FORGE_JEV_FIXTURES_STRICT'))
            reason = ('fixture-miss:' + fixture_key) if strict else 'fixture-miss'
            if strict:
                print('forge jev: ' + reason, file=sys.stderr)
            return done(False, reason, None)
        if not (isinstance(payload, dict) and isinstance(payload.get('answers'), dict)):
            return done(False, 'malformed', None)
        result = dict(answers=payload['answers'], usage=payload.get('usage'),
                      model=payload.get('model', config.get('model')), latency_s=0.0, site=site)
        return done(True, None, result, model=result['model'], usage=result['usage'], latency=0.0)

    budget = deadline_s if deadline_s is not None else config.get('deadline_s', DEFAULTS['deadline_s'])
    deadline_at = started + budget
    url = config.get('endpoint', DEFAULTS['endpoint'])
    backoffs = iter(_BACKOFFS)
    reason = 'timeout'
    while True:
        remaining = deadline_at - time.time()
        if remaining <= 0:
            return done(False, reason, None)
        request = urllib.request.Request(url, data=canonical, method='POST', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                payload = json.loads(response.read().decode())
            break
        except urllib.error.HTTPError as error:
            detail = redact(error.read().decode(errors='replace')[:200], key)
            reason = f'http-{error.code}: {detail}'
            if error.code not in _RETRYABLE_CODES:
                return done(False, reason, None)
        except urllib.error.URLError as error:
            reason = redact('network: ' + str(error.reason), key)
        except (TimeoutError, ValueError) as error:
            return done(False, redact('network: ' + str(error), key), None)
        delay = next(backoffs, None)
        remaining = deadline_at - time.time()
        if delay is None or remaining <= 0:
            return done(False, reason, None)
        time.sleep(min(delay, remaining))

    if not (isinstance(payload, dict) and isinstance(payload.get('answers'), dict)):
        return done(False, 'malformed', None)

    record_dir = os.environ.get('FORGE_JEV_RECORD')
    if record_dir:
        try:
            write_json(Path(record_dir) / (fixture_key + '.json'), payload)
        except OSError:
            pass   # recording is a convenience; never fail a call that already succeeded

    latency = time.time() - started
    result = dict(answers=payload['answers'], usage=payload.get('usage'),
                  model=payload.get('model', config.get('model')), latency_s=latency, site=site)
    return done(True, None, result, model=result['model'], usage=result['usage'], latency=latency)


def noul(result, qid: str) -> float | None:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    return answer.get('noul') if answer and answer.get('type') == 'noul' else None


def choice(result, qid: str) -> tuple[str | None, float]:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    if not answer or answer.get('type') != 'choice':
        return (None, 0.0)
    return (answer.get('choice'), answer.get('confidence', 0.0))


def score(result, qid: str) -> tuple[float | None, float]:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    if not answer or answer.get('type') != 'score':
        return (None, 0.0)
    return (answer.get('score'), answer.get('confidence', 0.0))


def normalized(result, qid: str) -> float | None:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    if not answer or answer.get('type') != 'score':
        return None
    legend = answer.get('legend') or {}
    value = answer.get('score')
    if value is None or len(legend) < 2:
        return None
    return value / (len(legend) - 1)
