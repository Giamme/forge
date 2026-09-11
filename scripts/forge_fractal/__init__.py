"""Opt-in Fractal adapter. Importing this package never imports Fractal."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

SCRIPTS = Path(__file__).resolve().parents[1]
REVISION = '18793200c0d7e8cdb2db369ea3abe5647a1e15e4'
VERSION = '1.2.0'
ARCHIVE_SHA256 = '3f7714ee0baec05af8d856db2b00d63398026fae2fcf50edbee542671126072c'
DEFAULTS = dict(depth=2, children=3, nodes=12, iterations=6, concurrency=3, deadline=2700)


def state_root() -> Path:
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser().resolve() / 'forge/fractal'


def runtime_python() -> Path:
    return state_root() / 'runtime/bin/python'


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _runtime.write_json(path, data)


_runtime_spec = importlib.util.spec_from_file_location('forge_runtime', SCRIPTS / 'forge-runtime.py')
_runtime = importlib.util.module_from_spec(_runtime_spec)
_runtime_spec.loader.exec_module(_runtime)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', value):
        raise ValueError('Invalid managed identifier: ' + repr(value))
    return value


def managed_run(value: str) -> Path:
    root = state_root() / 'runs'
    path = root / identifier(value)
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError('Run must be a direct managed directory')
    if not path.is_dir() or (path / 'run.json').is_symlink():
        raise ValueError('Unknown managed run: ' + value)
    return path


def safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Artifact path escapes managed data or contains a symlink')
    return path


@contextlib.contextmanager
def lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def command(argv: list, *, cwd: Path | None = None, timeout: float = 120, input: bytes | None = None) -> bytes:
    result = subprocess.run(list(map(str, argv)), cwd=cwd, input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace') or result.stdout.decode(errors='replace'))
    return result.stdout


def artifact(verb: str, *args: object) -> str:
    return command(['/bin/bash', '-c', 'source "$1"; shift; "$@"', 'forge',
                    SCRIPTS / 'forge-artifact.sh', verb, *args]).decode().strip()
