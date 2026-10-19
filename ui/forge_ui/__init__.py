"""forge-ui: a small read-only local UI for forge runs."""
from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = '0.2.0'


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
