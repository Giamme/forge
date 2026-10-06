#!/usr/bin/env python3
"""Offline runtime support; no provider requests or third-party dependencies."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def capability_help(cache: str, registry: str, recipe: str, command: list[str]) -> str:
    binary = Path(command[0]).resolve()
    stat = binary.stat()
    identity = [
        str(binary), stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns, command[1:],
        hashlib.sha256(Path(registry).read_bytes()).hexdigest(),
        hashlib.sha256(Path(recipe).read_bytes()).hexdigest(),
    ]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    root = Path(cache)
    root.mkdir(parents=True, exist_ok=True)
    path = root / key
    if path.exists():
        return path.read_text()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors='replace'))
    temporary = root / (key + '.' + uuid.uuid4().hex)
    temporary.write_bytes(result.stdout)
    temporary.replace(path)
    return result.stdout.decode(errors='replace')


def begin(root: str, role: str) -> Path:
    attempt = Path(root) / 'attempts' / (role + '-' + uuid.uuid4().hex)
    attempt.mkdir(parents=True)
    now = time.monotonic()
    write_json(attempt / 'metrics.json', {
        'role': role, 'started': now, 'wall_started': time.time(),
        'phase': 'preparation', 'phase_started': now,
        'elapsed_s': {key: 0.0 for key in (
            'preparation', 'preflight', 'model', 'snapshot', 'verification',
        )},
        'exit_code': None,
    })
    return attempt


def phase(attempt: str, name: str, rc: str | None = None) -> None:
    path = Path(attempt) / 'metrics.json'
    data = json.loads(path.read_text())
    now = time.monotonic()
    previous = data['phase']
    duration = now - data['phase_started']
    data['elapsed_s'][previous] = data['elapsed_s'].get(previous, 0) + duration
    data.update(phase=name, phase_started=now)
    if rc is not None:
        data['exit_code'] = int(rc)
        data['elapsed_s']['total'] = now - data['started']
    write_json(path, data)


def token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def extract(log: str, harness: str) -> tuple[str | None, dict[str, int | None]]:
    usage = dict(input_tokens=None, cached_input_tokens=None, output_tokens=None)
    final = None
    for line in log.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        native = event.get('usage')
        if not isinstance(native, dict):
            native = {}
        if harness == 'codex' and event.get('type') == 'turn.completed':
            for key in usage:
                value = token_count(native.get(key))
                if value is not None:
                    usage[key] = (usage[key] or 0) + value
        if harness == 'codex' and event.get('type') == 'item.completed':
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                if isinstance(item.get('text'), str):
                    final = item['text']
        if harness == 'claude' and event.get('type') == 'result':
            if isinstance(event.get('result'), str):
                final = event['result']
            # Claude reports uncached, cache-read and cache-creation separately.
            parts = [token_count(native.get(key)) for key in (
                'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens',
            )]
            usage['input_tokens'] = sum(parts) if all(v is not None for v in parts) else None
            usage['cached_input_tokens'] = token_count(native.get('cache_read_input_tokens'))
            usage['output_tokens'] = token_count(native.get('output_tokens'))
    return final, usage


def finish(attempt: str, root: str, role: str, harness: str, rc: str) -> None:
    root, attempt = Path(root), Path(attempt)
    log = root / (role + '.log')
    last = root / (role + '.last')
    structured = harness in ('codex', 'claude')
    text = log.read_text(errors='replace') if structured and log.exists() else ''
    final, usage = extract(text, harness)
    if not last.exists() or not last.stat().st_size:
        if final is not None:
            last.write_text(final + '\n')
        elif not structured and harness != 'pipeline' and log.exists():
            shutil.copyfile(log, last)
    path = attempt / 'metrics.json'
    data = json.loads(path.read_text())
    prompt = root / (role + '.prompt')
    prompt_ready = (attempt / 'prompt.ready').exists() and prompt.exists()
    data.update(
        harness=harness,
        prompt_bytes=prompt.stat().st_size if prompt_ready else None,
        **usage,
    )
    infra = root / (role + '.infra')
    data['infra_class'] = None
    if infra.exists():
        for line in infra.read_text(errors='replace').splitlines():
            if line.startswith('class='):
                data['infra_class'] = line[len('class='):].strip() or None
    for suffix in ('prompt', 'last', 'log', 'resolved', 'cmd', 'timedout', 'infra', 'orphans'):
        if suffix == 'prompt' and not prompt_ready:
            continue
        source = root / (role + '.' + suffix)
        if source.exists():
            shutil.copyfile(source, attempt / source.name)
    write_json(path, data)
    # Include extraction and artifact-copy cost in total, even on failed attempts.
    phase(str(attempt), 'complete', rc)


# --- infrastructure failure classification --------------------------------------
# Called by forge-dispatch.sh right after the harness exits, from the LOG: $ROLE.last
# only exists later (the EXIT trap's `finish` writes it), and a harness that failed on
# quota or auth never produced a final message to read anyway.
#
# The point of the class is to tell "the model's work was bad" apart from "the model
# never got to work" so a caller can pause and retry instead of spending an attempt. A
# false positive pauses a run that should have failed; a false negative is today's
# behaviour. So every pattern is a strong phrase, and prose is never scanned when the
# harness exited 0 with a real final message.
INFRA_TAIL_BYTES = 2048
RETRY_CAP_S = 24 * 3600
DETAIL_MAX = 200

_P = re.compile
# "HTTP 401", "HTTP/1.1 401", "status code 401", `"status": 429`, "error: 401", "code 401".
_HTTP_CODE = r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[ _]code)?|error(?: code)?|code)\b\W{0,10}%s\b"
INFRA_PATTERNS = (
    ('auth', (
        _P(r"\bunauthori[sz]ed\b", re.I),
        _P(r"\binvalid[ _-]?(?:x-)?api[ _-]?key\b", re.I),
        _P(r"\binvalid (?:access |auth |bearer )?(?:token|credentials?)\b", re.I),
        _P(r"\b(?:not|never) (?:logged|signed) in\b", re.I),
        _P(r"\blog ?in required\b|\bplease (?:run )?/?log ?in\b|\brun /login\b", re.I),
        _P(r"\bauthenticat\w* (?:failed|failure|error|required)\b|\bfailed to authenticate\b|\bauthentication_error\b", re.I),
        _P(r"\bAuthenticateToken\b"),
        _P(r"\b(?:oauth |access |auth |refresh |api )?token (?:has |is |was )?(?:expired|revoked)\b|\bexpired (?:oauth |access |auth )?token\b", re.I),
        _P(r"\b(?:no|missing) api[ _-]?key\b|\bcould not resolve authentication method\b", re.I),
        _P(_HTTP_CODE % '40[13]' + r"|\b40[13]\b\W{0,3}(?:unauthori[sz]ed|forbidden)\b", re.I),
    )),
    ('quota', (
        _P(r"\busage[ _-]?limit\b", re.I),
        # "You've hit your limit", "hit your session limit", "reached the weekly limit"; but not
        # "reached the context limit", which is the model's problem rather than the account's.
        _P(r"\b(?:hit|reached|exceeded) (?:your|the) (?:(?:usage|session|weekly|daily|monthly|hourly|"
           r"5-hour|five-hour|spend\w*|credit|token|message|request|plan|org\w*) )?limits?\b", re.I),
        _P(r"(?<!context )(?<!rate )(?<!rate-)(?<!rate_)\blimit (?:reached|exceeded)\b", re.I),
        _P(r"\b(?:session|weekly|daily|monthly|5-hour|five-hour) limit\b", re.I),
        _P(r"\bquota (?:exceeded|exhausted|reached)\b|\bexceeded (?:your |the )?(?:current )?quota\b|\binsufficient[ _-]quota\b|\bout of (?:credits|quota)\b", re.I),
        _P(r"\binsufficient (?:balance|credits?|funds)\b", re.I),
        _P(r"\bcredit balance (?:is )?too low\b", re.I),
        _P(r"\bpayment required\b|\bbilling (?:hard )?limit\b|\bbilling (?:issue|error|details|not active)\b|\bcheck your (?:plan and )?billing\b", re.I),
    )),
    ('rate_limit', (
        _P(r"\brate[ _-]?limit(?:s|ed|ing)?\b", re.I),
        _P(r"\btoo many requests\b", re.I),
        _P(r"\boverloaded\b", re.I),
        _P(r"\bat capacity\b", re.I),
        _P(_HTTP_CODE % '429' + r"|\b429\b\W{0,3}too many\b", re.I),
    )),
    ('network', (
        _P(r"\b(?:ECONNRESET|ETIMEDOUT|ENOTFOUND|ECONNREFUSED|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH|ENETDOWN)\b"),
        _P(r"\bgetaddrinfo\b", re.I),
        _P(r"\bconnection (?:refused|reset|timed out)\b|\bnetwork is unreachable\b|\bsocket hang up\b", re.I),
        _P(r"\bcan['’]?t reach the api server\b|\bunable to connect\b|\bfailed to connect\b|\bfetch failed\b", re.I),
        _P(r"\btemporary failure in name resolution\b|\bname or service not known\b", re.I),
        _P(r"\bbad gateway\b|\bservice unavailable\b|\bgateway time-?out\b", re.I),
        _P(r"\binvalid peer certificate\b", re.I),
    )),
)
# api_error_status as claude reports it, used only when the text itself matched nothing.
STATUS_CLASS = {401: 'auth', 403: 'auth', 402: 'quota', 429: 'rate_limit', 529: 'rate_limit',
                502: 'network', 503: 'network', 504: 'network'}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}
_UNIT_S = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
_DURATION = r"(?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![A-Za-z])[\s,]*(?:and\s+)?)+"
_EPOCH_RE = _P(r"\|\s*(\d{9,13})\b")
_IN_RE = _P(r"\b(?:try again|retry(?:ing)?|resets?|available|wait(?:ing)?|again|back)\s+(?:again\s+)?(?:in|after|for)\s+(?P<d>%s)" % _DURATION, re.I)
_WAIT_RE = _P(r"\bwait(?:ing)?\s+(?P<d>%s)" % _DURATION, re.I)
_AFTER_RE = _P(r"\bretry[ _-]?after\W{0,4}(?P<d>%s|\d+(?:\.\d+)?)" % _DURATION, re.I)
_CLOCK_RE = _P(
    r"\b(?:resets?|try again|available again|retry)\s*(?:at|on|after)?\s*"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?:(?P<ap>[ap]\.?m\.?)(?![A-Za-z]))?"
    r"(?:\s*\((?P<tz>[A-Za-z0-9_/+-]+)\))?", re.I)


def _duration_seconds(text: str) -> float | None:
    total, found = 0.0, False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![A-Za-z])", text, re.I):
        total += float(number) * _UNIT_S[unit[0].lower()]
        found = True
    if not found and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", text):
        return float(text)       # bare "retry-after: 120" is seconds
    return total if found else None


def _clock_seconds(match: 're.Match', now: float) -> float | None:
    hour, minute, ap = int(match.group('h')), int(match.group('m') or 0), match.group('ap')
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap.lower().startswith('p') else 0)
    elif match.group('m') is None or hour > 23:
        return None              # "resets 3" with no am/pm and no minutes is not a time
    if minute > 59:
        return None
    tz = None
    if match.group('tz'):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(match.group('tz'))
        except Exception:
            tz = None            # unknown zone name or no tz database: fall back to local time
    current = datetime.datetime.fromtimestamp(now, tz)
    if match.group('mon'):
        try:
            target = current.replace(month=_MONTHS[match.group('mon').lower()[:3]], day=int(match.group('day')),
                                     hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if target <= current:
            target = target.replace(year=target.year + 1)
    else:
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += datetime.timedelta(days=1)
    return target.timestamp() - now


def retry_after(text: str, now: float | None = None) -> int | None:
    """Seconds until the limit lifts, from the message text; None when it says nothing."""
    now = time.time() if now is None else now
    found: float | None = None
    epoch = _EPOCH_RE.search(text)
    if epoch:
        stamp = float(epoch.group(1))
        found = max(0.0, (stamp / 1000 if stamp > 1e11 else stamp) - now)
    if found is None:
        for regex in (_IN_RE, _WAIT_RE, _AFTER_RE):
            match = regex.search(text)
            if match:
                found = _duration_seconds(match.group('d'))
                if found is not None:
                    break
    if found is None:
        for match in _CLOCK_RE.finditer(text):
            found = _clock_seconds(match, now)
            if found is not None:
                break
    if found is None:
        return None
    return int(min(RETRY_CAP_S, max(0, found) + 0.999))


def _one_line(text: str, limit: int = DETAIL_MAX) -> str:
    return ' '.join(text.split())[:limit]


def classify_text(text: str, status: object = None, now: float | None = None):
    """(class, retry_after_or_None, detail) for failure text, or None. auth > quota > rate_limit > network."""
    for name, patterns in INFRA_PATTERNS:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                start = text.rfind('\n', 0, match.start()) + 1
                end = text.find('\n', match.end())
                line = text[start:end if end >= 0 else len(text)]
                if len(line) > DETAIL_MAX:      # a minified blob: excerpt around the match
                    line = text[max(0, match.start() - 80):match.end() + 80]
                return name, retry_after(text, now), _one_line(line)
    if type(status) is int and status in STATUS_CLASS:
        return STATUS_CLASS[status], retry_after(text, now), _one_line(text or 'HTTP %d' % status)
    return None


def _events(text: str):
    """JSON objects in the log (JSONL, one JSON document, or a JSON array) and the non-JSON lines."""
    events, stray = [], []
    stripped = text.strip()
    if stripped[:1] in ('{', '['):
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            return [whole], []
        if isinstance(whole, list):
            return [x for x in whole if isinstance(x, dict)], []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if candidate[0] == '{':
            try:
                event = json.loads(candidate)
            except ValueError:
                continue         # a damaged JSON line is not prose either
            if isinstance(event, dict):
                events.append(event)
            continue
        if candidate[0] == '[':
            continue
        stray.append(candidate)
    return events, stray


def _codex_terminal_errors(events: list) -> list[str]:
    """Messages of error/turn.failed events with nothing but more errors after them.

    codex emits non-fatal `error` events (reconnect notices) and then carries on, so an
    error counts only when it is where the run stopped: any later item, message or
    completed turn shows it recovered.
    """
    pending: list[str] = []
    for event in events:
        kind = event.get('type')
        if kind == 'error':
            message = event.get('message')
            pending.append(message if isinstance(message, str) else json.dumps(event))
        elif kind == 'turn.failed':
            error = event.get('error')
            message = error.get('message') if isinstance(error, dict) else error
            pending.append(message if isinstance(message, str) else json.dumps(event))
        else:
            pending = []     # any later activity means the run went on past the error
    return pending


def _codex_final(events: list, last: str) -> str:
    try:
        path = Path(last) if last else None
        if path is not None and path.is_file():
            text = path.read_text(errors='replace')
            if text.strip():
                return text
    except OSError:
        pass
    final = ''
    for event in events:
        item = event.get('item')
        if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message':
            if isinstance(item.get('text'), str) and item['text'].strip():
                final = item['text']
    return final


def _tail(lines_or_text, size: int = INFRA_TAIL_BYTES) -> str:
    text = lines_or_text if isinstance(lines_or_text, str) else '\n'.join(lines_or_text)
    return text[-size:]


def classify(log: str, harness: str, rc: object, last: str = '', role: str = '', now: float | None = None):
    """(class, retry_after_or_None, detail) when this exit was infrastructure, else None."""
    try:
        rc = int(rc)
    except (TypeError, ValueError):
        rc = 1
    if harness == 'codex' and rc == 0 and _codex_final([], last).strip():
        return None      # the common case, and codex logs get large: don't even read it
    try:
        text = Path(log).read_text(errors='replace')
    except OSError:
        text = ''
    events, stray = _events(text) if harness in ('codex', 'claude', 'openclaude') else ([], [])

    if harness == 'codex':
        final = _codex_final(events, last)
        pending = _codex_terminal_errors(events)
        if rc == 0:
            if final.strip():
                return None
            if pending:
                hit = classify_text('\n'.join(pending), now=now)
                if hit:
                    return hit
            return 'empty', None, 'codex finished with exit 0 but produced no final message'
        return classify_text('\n'.join(pending + [_tail(stray)]), now=now)

    results = [e for e in events if e.get('type') == 'result'] if harness in ('claude', 'openclaude') else []
    if results:
        event = results[-1]
        body = event.get('result')
        body = body if isinstance(body, str) else ''
        if event.get('is_error') is True:
            return classify_text(body, event.get('api_error_status'), now)
        if rc == 0 and not body.strip():
            return 'empty', None, '%s finished with exit 0 but its final result is empty' % harness
        return None

    # Raw-log harness (opencode, antigravity, openclaude's plain text, or a claude that
    # died before emitting its result): the log is the message, so only the tail of a
    # failing run is evidence. A successful run's prose is never scanned.
    if rc != 0:
        return classify_text(_tail(text), now=now)
    if not text.strip():
        return 'empty', None, '%s finished with exit 0 but wrote nothing' % harness
    return None


def classify_command(log: str, harness: str, rc: str, last: str = '', role: str = '') -> None:
    try:
        hit = classify(log, harness, rc, last, role)
    except Exception:            # classification is advisory and must never change an exit code
        hit = None
    if hit:
        name, after, detail = hit
        print('%s\t%s\t%s' % (name, '' if after is None else after, detail.replace('\t', ' ')))


def report(root: str) -> None:
    records = []
    for path in sorted(Path(root).rglob('attempts/*/metrics.json')):
        record = json.loads(path.read_text())
        record['artifact'] = str(path)
        records.append(record)
    print(json.dumps({'attempts': records}, indent=2))


def main() -> None:
    command, *args = sys.argv[1:]
    try:
        if command == 'help':
            print(capability_help(*args[:3], args[3:]), end='')
        elif command == 'begin':
            print(begin(*args))
        elif command == 'phase':
            phase(*args)
        elif command == 'finish':
            finish(*args)
        elif command == 'report':
            report(*args)
        elif command == 'classify':
            classify_command(*args)
        else:
            raise ValueError('unknown runtime command: ' + command)
    except (OSError, ValueError, RuntimeError) as error:
        print('forge: ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
