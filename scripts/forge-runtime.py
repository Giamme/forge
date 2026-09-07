#!/usr/bin/env python3
"""Offline runtime support; no provider requests or third-party dependencies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
    for suffix in ('prompt', 'last', 'log', 'resolved', 'cmd', 'timedout'):
        if suffix == 'prompt' and not prompt_ready:
            continue
        source = root / (role + '.' + suffix)
        if source.exists():
            shutil.copyfile(source, attempt / source.name)
    write_json(path, data)
    # Include extraction and artifact-copy cost in total, even on failed attempts.
    phase(str(attempt), 'complete', rc)


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
        else:
            raise ValueError('unknown runtime command: ' + command)
    except (OSError, ValueError, RuntimeError) as error:
        print('forge: ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
