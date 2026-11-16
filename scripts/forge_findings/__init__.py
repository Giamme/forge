"""Findings: the record store (store.py), default tags (tags.py) and the one status rule (status.py).

Standard library only. `fingerprint` is the record key: a finding's identity across runs and checkouts.
"""
from __future__ import annotations

import hashlib
import re


def fingerprint(slug, file, text) -> str:
    """12 hex chars naming a finding; byte-identical to `forge-issues.py fingerprint(slug, {"file", "text"})`.

    `slug` is `owner/repo` (`forge_identity.github_slug(ident) or ""`). Keep the two in step: a test pins them.
    """
    norm = re.sub(r"\s+", " ", text.lower())[:160]
    return hashlib.sha1(f"{slug.lower()}|{file}|{norm}".encode("utf-8")).hexdigest()[:12]
