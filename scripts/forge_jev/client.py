"""Stdlib-only decision-model HTTP client; every failure degrades to None, never raises.

Two providers answer the same request shape ({state, model, questions}): TypeSafe's
hosted Jev, and Cloudflare Workers AI's hosted Clef. Which one a call goes to is the
site's (`provider_for`); without a Cloudflare provider configured every site asks
TypeSafe exactly as it always has.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import (CLOUDFLARE_CONTEXT, CLOUDFLARE_MAX_QUESTIONS, CLOUDFLARE_MODEL, CLOUDFLARE_URL, DEFAULTS,
               acting, api_key, cloudflare, load_config, provider_for, redact, secrets, shadow, write_json)

_BACKOFFS = (0.2, 0.4, 0.8)
_RETRYABLE_CODES = (429, 529)
# Cloudflare's own codes inside the envelope. Capacity and throttling pass; a used-up
# allocation or a plan that does not include the model will not pass by retrying.
_CF_RETRY = (3040, 971)
_CF_FINAL = {4006: 'quota', 3036: 'quota', 5035: 'plan'}
# How many chunks of one request are in flight at once, so a 120-question drift request
# does not open 120 / 64 sockets at a rate limiter that already counts requests.
_CF_CONCURRENCY = 4
# Bytes of an error body read for its codes; a misbehaving proxy cannot stream forever.
_ERROR_BODY_BYTES = 64 * 1024


def _skip(run_dir: Path | str | None, site: str, reason: str) -> None:
    if run_dir is None:
        return
    try:
        with (Path(run_dir) / 'jev.skip').open('a') as handle:
            handle.write(f'{site}: {reason}\n')
            handle.flush()
    except OSError:
        pass


def _log(run_dir, site, key, ok, reason, latency_s, model, usage, questions, answers, provider='typesafe') -> None:
    if run_dir is None:
        return
    entry = dict(at=time.time(), site=site, key=key, ok=ok, reason=reason, latency_s=latency_s,
                model=model, usage=usage, questions=questions, answers=answers,
                acting=acting(), shadow=shadow(), provider=provider)
    try:
        with (Path(run_dir) / 'jev.jsonl').open('a') as handle:
            handle.write(json.dumps(entry) + '\n')
            handle.flush()
    except OSError:
        pass
    if not ok:
        _skip(run_dir, site, reason)


# Not decisions about a real run: replays of recorded answers and the setup key probe.
# Kept out of the run dataset so it only holds live judgments.
_DATASET_SKIP_SITES = ('setup-probe',)


def _dataset(site, key, canonical, ok, reason, latency, model, usage, qids, answers, run_dir, extra=None) -> None:
    """Keep the full request (state and questions) with its answers in the run dataset.

    jev.jsonl keeps only the request's sha256, so a judgment could never be replayed
    against another model, or used to train one. The blob's original sha IS that key,
    so the two records join. Fail-open: never raises, never prints.
    """
    if os.environ.get('FORGE_JEV_FIXTURES') or site in _DATASET_SKIP_SITES:
        return
    try:
        scripts = str(Path(__file__).resolve().parent.parent)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from forge_dataset.capture import jev
        jev(site=site, key=key, canonical=canonical, ok=ok, reason=reason, latency=latency, model=model,
            usage=usage, questions=qids, answers=answers, run_dir=run_dir, acting=acting(), shadow=shadow(),
            extra=extra)
    except Exception:
        pass


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def _probability(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0.0 <= value <= 1.0)


def valid_answer(answer, question: dict) -> bool:
    """An answer of the question's own type that a caller can read without guarding."""
    if not isinstance(answer, dict) or answer.get('type') != question.get('type'):
        return False
    kind = question['type']
    if kind == 'noul':
        return _probability(answer.get('noul'))
    if kind == 'choice':
        if answer.get('choice') not in (question.get('criteria') or {}):
            return False
        found = answer.get('probabilities')
        return found is None or (isinstance(found, dict) and all(_probability(v) for v in found.values()))
    if kind == 'score':
        value = answer.get('score')
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return False


def _tokens(text: str) -> int:
    # Conservative: forge's states are code and JSON, which run 3.3-4 characters per token,
    # so dividing by 3 over-counts. Over-counting only means an extra chunk.
    return (len(text) + 2) // 3


def chunks(state, questions: dict, model: str) -> list[dict] | None:
    """Questions packed into requests Cloudflare will evaluate whole, or None if none fits.

    Each request carries the full state, at most CLOUDFLARE_MAX_QUESTIONS questions, and
    stays under 90% of the model's context, which counts state and questions together.
    Past that the service cuts the state silently, so a request that cannot fit even one
    question is refused here instead of being answered on evidence it never saw.
    """
    budget = int(CLOUDFLARE_CONTEXT.get(model, CLOUDFLARE_CONTEXT[CLOUDFLARE_MODEL]) * 0.9)
    base = _tokens(_canonical(state).decode())
    out, current, cost = [], {}, base
    for qid, question in questions.items():
        need = _tokens(_canonical({qid: question}).decode()) + 8
        if base + need > budget:
            return None
        if current and (len(current) >= CLOUDFLARE_MAX_QUESTIONS or cost + need > budget):
            out.append(current)
            current, cost = {}, base
        current[qid] = question
        cost += need
    if current:
        out.append(current)
    return out


def _error_codes(raw: bytes) -> list:
    try:
        envelope = json.loads(raw.decode(errors='replace'))
    except ValueError:
        return []
    errors = envelope.get('errors') if isinstance(envelope, dict) else None
    return [e.get('code') for e in errors or [] if isinstance(e, dict)]


def _retry_after(error) -> float | None:
    try:
        value = float(error.headers.get('Retry-After'))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _cloudflare_post(url: str, body: bytes, token: str, deadline_at: float, hidden: list) -> tuple:
    """One request to Workers AI: (result dict, None) or (None, reason)."""
    backoffs = iter(_BACKOFFS)
    reason = 'timeout'
    while True:
        remaining = deadline_at - time.time()
        if remaining <= 0:
            return None, reason
        request = urllib.request.Request(url, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        wait = None
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                raw = response.read()
            try:
                envelope = json.loads(raw.decode())
            except ValueError:
                return None, 'malformed'
            if not isinstance(envelope, dict):
                return None, 'malformed'
            if envelope.get('success') is False:
                codes = _error_codes(raw)
                final = next((_CF_FINAL[c] for c in codes if c in _CF_FINAL), None)
                return None, final or 'cloudflare: ' + ','.join(str(c) for c in codes)
            result = envelope.get('result') if 'result' in envelope else envelope
            return (result, None) if isinstance(result, dict) else (None, 'malformed')
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(_ERROR_BODY_BYTES)
            except OSError:
                raw = b''
            codes = _error_codes(raw)
            final = next((_CF_FINAL[c] for c in codes if c in _CF_FINAL), None)
            if final:
                return None, final
            if error.code in (401, 403):
                return None, 'auth'
            # Redacted before it is cut, so a token straddling the cut leaves no prefix.
            reason = 'http-%d: %s' % (error.code, redact(raw.decode(errors='replace'), *hidden)[:200])
            if not (error.code in _RETRYABLE_CODES or error.code >= 500 or set(codes) & set(_CF_RETRY)):
                return None, reason
            wait = _retry_after(error)
        except urllib.error.URLError as error:
            reason = redact('network: ' + str(error.reason), *hidden)
        except (TimeoutError, ValueError, OSError) as error:
            return None, redact('network: ' + str(error), *hidden)
        step = next(backoffs, None)
        remaining = deadline_at - time.time()
        if step is None or remaining <= 0:
            return None, reason
        time.sleep(min(max(step, wait or 0.0), remaining))


def _cloudflare_ask(state, questions: dict, provider: dict, deadline_at: float, hidden: list) -> tuple:
    """(result, None, chunk hashes) or (None, reason, chunk hashes); chunks run concurrently."""
    parts = chunks(state, questions, provider['model'])
    if parts is None:
        return None, 'too-large', []
    url = (provider.get('url') or CLOUDFLARE_URL).format(account=provider['account_id'], model=provider['model'])
    bodies = [_canonical(dict(model=provider['model'], state=state, questions=part)) for part in parts]
    hashes = [hashlib.sha256(body).hexdigest() for body in bodies]
    results, reasons = [None] * len(bodies), ['timeout'] * len(bodies)
    gate = threading.BoundedSemaphore(_CF_CONCURRENCY)

    def work(i):
        with gate:
            if time.time() < deadline_at:
                results[i], reasons[i] = _cloudflare_post(url, bodies[i], provider['token'], deadline_at, hidden)

    if len(bodies) == 1:
        work(0)
    else:
        # Daemon threads: a stalled chunk past the deadline must not hold the process open.
        threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(bodies))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(max(0.0, deadline_at - time.time()))
    answers, usage, model = {}, {}, None
    for part, result, reason in zip(parts, results, reasons):
        if result is None:
            return None, reason or 'timeout', hashes
        got = result.get('answers')
        if not isinstance(got, dict) or not all(valid_answer(got.get(q), part[q]) for q in part):
            return None, 'malformed', hashes
        answers.update({q: got[q] for q in part})
        for name, value in (result.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                usage[name] = usage.get(name, 0) + value
        model = result.get('model') or model
    return dict(answers=answers, usage=usage or None, model=model or provider['model']), None, hashes


def ask(state, questions: dict, *, site: str, run_dir=None, config: dict | None = None,
        deadline_s: float | None = None, context: dict | None = None) -> dict | None:
    """One judgment, or None. `context` (task, stage, attempt, assignment) is recorded with
    the request in the run dataset, for sites whose run dir does not say which task it is."""
    config = config or load_config()
    provider = provider_for(site, config)
    remote = cloudflare(config) if provider == 'cloudflare' else None
    if provider == 'cloudflare':
        key = remote['token'] if remote and remote['account_id'] else None
        model = remote['model'] if remote else CLOUDFLARE_MODEL
    else:
        key = api_key(config)
        model = config.get('model', DEFAULTS['model'])
    qids = list(questions)
    started = time.time()
    extra = dict(context or {}, provider=provider, occurrence=uuid.uuid4().hex)

    def done(ok, reason, result, model=None, usage=None, latency=None):
        latency = latency if latency is not None else time.time() - started
        answers = result['answers'] if result else None
        _log(run_dir, site, fixture_key, ok, reason, latency, model, usage, qids, answers, provider)
        if canonical is not None:
            _dataset(site, fixture_key, canonical, ok, reason, latency, model, usage, qids, answers, run_dir,
                     extra)
        return result

    fixture_key = canonical = None
    body = dict(state=state, model=model, questions=questions)
    try:
        canonical = _canonical(body)
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
                      model=payload.get('model', model), latency_s=0.0, site=site)
        return done(True, None, result, model=result['model'], usage=result['usage'], latency=0.0)

    budget = deadline_s if deadline_s is not None else config.get('deadline_s', DEFAULTS['deadline_s'])
    deadline_at = started + budget
    hidden = secrets(config)
    if provider == 'cloudflare':
        payload, reason, extra['chunks'] = _cloudflare_ask(state, questions, remote, deadline_at, hidden)
        if payload is None:
            return done(False, reason, None)
    else:
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
                # Redacted before it is cut, so a key straddling the cut leaves no prefix.
                detail = redact(error.read().decode(errors='replace'), key)[:200]
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
                  model=payload.get('model', model), latency_s=latency, site=site)
    return done(True, None, result, model=result['model'], usage=result['usage'], latency=latency)


def noul(result, qid: str) -> float | None:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    return answer.get('noul') if answer and answer.get('type') == 'noul' else None


def choice(result, qid: str) -> tuple[str | None, float]:
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    if not answer or answer.get('type') != 'choice':
        return (None, 0.0)
    return (answer.get('choice'), answer.get('confidence', 0.0))


def probabilities(result, qid: str) -> dict[str, float]:
    """A Choice's full distribution over its options, {} when the response omits it.

    `choice()` returns only the winner and its confidence, and for a selection among
    several ACCEPTABLE options that confidence is the wrong quantity: it measures
    "which one", not "any of them". Two commands that both run the suite split the mass
    and depress it, which is a tie, not doubt.
    """
    answer = (result or {}).get('answers', {}).get(qid) if result else None
    if not answer or answer.get('type') != 'choice':
        return {}
    found = answer.get('probabilities')
    if not isinstance(found, dict):
        return {}
    out = {}
    for option, value in found.items():
        try:
            out[str(option)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


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
