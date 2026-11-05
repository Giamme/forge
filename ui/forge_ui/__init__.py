"""forge-ui: a small read-only local UI for forge runs."""
from __future__ import annotations

import hashlib
from pathlib import Path

# forge's own version: this package ships inside forge, and two numbers could only disagree. What a
# running server reports is what it started with, which an upgrade underneath it does not change.
VERSION_FILE = Path(__file__).resolve().parents[2] / 'VERSION'


def read_version(path=VERSION_FILE):
    """The first line of forge's VERSION file, or None when it cannot be read."""
    try:
        return Path(path).read_text(encoding='utf-8').split('\n', 1)[0].strip() or None
    except (OSError, ValueError):
        return None


__version__ = read_version()


def build_id():
    """sha256 hex of this package's *.py files: sorted by name, each name then its bytes.

    Static files are not part of the id. The server re-reads them on every request.
    """
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for path in sorted(root.glob('*.py'), key=lambda item: item.name):
        digest.update(path.name.encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()
