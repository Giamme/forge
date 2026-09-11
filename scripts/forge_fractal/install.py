"""Versioned, isolated installation; no shell profile or activation preference."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import time
import urllib.request

from . import ARCHIVE_SHA256, REVISION, VERSION, SCRIPTS, command, lock, read_json, runtime_python, state_root, write_json

UV_VERSION = '0.8.22'
UV_HASHES = {
    'aarch64-apple-darwin': '3f61099e261e449527141dbf125629fab33ad696468c8c90cebbac40185a306c',
    'x86_64-apple-darwin': '76638fdcfa91357858771551a1c88de1f7c3b270b33ab1866f8a0618d9e442d8',
    'aarch64-unknown-linux-gnu': '726b72a137fda33565143325f7d31c42cd30ff9ccdf067e00d124d37b4081cb2',
    'x86_64-unknown-linux-gnu': '741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf',
}


def compatible_python() -> str | None:
    for name in (sys.executable, 'python3.14', 'python3.13', 'python3.12'):
        binary = shutil.which(name)
        if binary:
            try:
                version = json.loads(command([binary, '-c', 'import sys,json; print(json.dumps(list(sys.version_info[:2])))']))
                if (3, 12) <= tuple(version) < (3, 15):
                    return binary
            except (RuntimeError, ValueError):
                pass
    return None


def tmux_install() -> list[str] | None:
    if platform.system() == 'Darwin' and shutil.which('brew'):
        return [shutil.which('brew'), 'install', 'tmux']
    release = Path('/etc/os-release')
    if release.exists() and any(v in release.read_text().lower() for v in ('debian', 'ubuntu')) and shutil.which('apt-get'):
        return ([] if os.geteuid() == 0 else ['sudo', '-n']) + ['apt-get', 'install', '-y', 'tmux']
    return None


def doctor() -> dict:
    python = runtime_python()
    data = dict(version=VERSION, revision=REVISION, python=str(python), tmux=shutil.which('tmux'), ready=False)
    provenance = state_root() / 'provenance.json'
    try:
        recorded = read_json(provenance)
        if recorded['revision'] != REVISION or recorded['archive_sha256'] != ARCHIVE_SHA256:
            raise ValueError('Managed provenance does not match the pinned revision')
        code = (f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); '
                'from forge_fractal.bridge import ForgeAgent; assert not ForgeAgent.enforces_budget; '
                'import importlib.metadata as m; from fractal.core.agent import Agent, Invocation, StreamParser, resolve; '
                'from fractal.core.node import Node; '
                'assert m.version("plasma-fractal")=="1.2.0"; print(m.version("plasma-wiki"))')
        data['wiki_version'] = command([python, '-c', code], timeout=20).decode().strip()
        if not (python.parent / 'wiki').is_file():
            raise ValueError('Managed wiki executable is missing')
        data['ready'] = bool(data['tmux'])
        if not data['ready']:
            data['error'] = 'tmux is missing'
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        data['error'] = str(error)
    data['remedy'] = 'forge fractal install --yes --with-prerequisites'
    return data


def download(url: str, path: Path, digest: str) -> None:
    with urllib.request.urlopen(url, timeout=45) as response, path.open('wb') as output:
        shutil.copyfileobj(response, output)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Checksum mismatch: ' + url + '; retained ' + str(path))


def install(args) -> int:
    root = state_root()
    uv = shutil.which('uv') or (str(root / 'bin/uv') if (root / 'bin/uv').is_file() else None)
    python = compatible_python()
    tmux = shutil.which('tmux')
    prereq = tmux_install()
    target = ('aarch64' if platform.machine() in ('arm64', 'aarch64') else platform.machine()) + (
        '-apple-darwin' if platform.system() == 'Darwin' else '-unknown-linux-gnu')
    preview = dict(destination=str(root / 'runtime'), version=VERSION, revision=REVISION,
                   downloads=[f'https://codeload.github.com/plasma-ai/fractal/tar.gz/{REVISION}',
                              'Fractal dependencies from PyPI, including plasma-wiki'],
                   uv=uv or f'Official uv {UV_VERSION} checksum-verified release for {target}',
                   python=python or 'uv python install 3.13',
                   tmux=tmux or prereq or 'Install tmux with your system package manager; on macOS install Homebrew first',
                   activation='off; each new run makes a separate choice')
    print(json.dumps(preview, indent=2))
    if args.dry_run:
        return 0
    if doctor()['ready']:
        print('Pinned managed runtime already installed; no changes.')
        return 0
    missing = not uv or not python or not tmux
    if not args.yes:
        if not sys.stdin.isatty():
            raise ValueError('Unattended installation requires --yes; prerequisites also require --with-prerequisites')
        if input('Install the managed Fractal runtime? [y/N] ').lower() != 'y':
            return 0
    if missing and not args.with_prerequisites:
        raise ValueError('Missing prerequisites shown above. Install them, or explicitly use --yes --with-prerequisites')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock(root / 'install.lock'):
        attempt = root / 'installs' / str(time.time_ns())
        attempt.mkdir(parents=True)
        write_json(attempt / 'preview.json', preview)
        try:
            if not uv:
                if target not in UV_HASHES:
                    raise ValueError('Unsupported uv bootstrap platform; install uv >=0.8 manually')
                archive = attempt / 'uv.tar.gz'
                download(f'https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{target}.tar.gz', archive, UV_HASHES[target])
                with tarfile.open(archive) as tar:
                    member = next(m for m in tar.getmembers() if m.name.endswith('/uv') and m.isfile())
                    (root / 'bin').mkdir(exist_ok=True)
                    destination = root / 'bin/uv'
                    with tar.extractfile(member) as src, destination.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    destination.chmod(0o755)
                    uv = str(destination)
            if not python:
                command([uv, 'python', 'install', '3.13'], timeout=600)
                python = command([uv, 'python', 'find', '3.13']).decode().strip()
            if not tmux:
                if prereq is None:
                    raise ValueError(str(preview['tmux']))
                try:
                    command(prereq, timeout=600)
                except RuntimeError as error:
                    raise RuntimeError(f'tmux installation failed. Run {prereq!r} in a terminal with privileges, then retry. {error}') from error
            archive = attempt / 'fractal.tar.gz'
            download(preview['downloads'][0], archive, ARCHIVE_SHA256)
            # A failed runtime is retained and never overlays unrelated environments.
            destination = root / 'runtime'
            if destination.exists():
                destination.rename(attempt / 'previous-runtime')
            command([uv, 'venv', destination, '--python', python], timeout=120)
            output = command([uv, 'pip', 'install', '--python', destination / 'bin/python', archive], timeout=600)
            (attempt / 'install.log').write_bytes(output)
            versions = command([uv, 'pip', 'freeze', '--python', destination / 'bin/python']).decode()
            write_json(root / 'provenance.json', dict(version=VERSION, revision=REVISION,
                       archive_sha256=ARCHIVE_SHA256, source=preview['downloads'][0], versions=versions,
                       uv=command([uv, '--version']).decode().strip(), installed_at=time.time()))
            result = doctor()
            if not result['ready']:
                raise RuntimeError(str(result))
            print('Installed. Fractal remains opt-in for each run.')
        except Exception as error:
            (attempt / 'error.txt').write_text(str(error))
            raise
    return 0
