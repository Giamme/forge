"""Default tags of a finding, derived from its file path and never stored."""
from __future__ import annotations

import fnmatch
import re

_TESTS_DIR = re.compile(r"(^|/)tests?/")
_TEST_NAMES = ("test_*", "*_test.*", "*.test.*")
_DOCS_PREFIXES = ("docs/", "site/")
_INFRA_PREFIXES = (".github/", "ci/", "scripts/forge-install")
_INFRA_ROOT_NAMES = ("*.yml", "*.yaml")
_UI_PREFIXES = ("ui/", "web/", "frontend/", "static/")
_UI_NAMES = ("*.js", "*.ts", "*.tsx", "*.css", "*.html")


def _any(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def default_tags(path) -> list[str]:
    """The tag a finding gets from its file: the first matching rule wins; no path gives `[]`.

    tests (a tests/ directory, test_*, *_test.*, *.test.*), docs (docs/, site/, *.md),
    infra (.github/, ci/, Dockerfile, root *.yml/*.yaml, install*, scripts/forge-install*),
    ui (ui/, web/, frontend/, static/, *.js *.ts *.tsx *.css *.html), else backend.
    """
    if not isinstance(path, str) or not path.strip():
        return []
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if not text:
        return []
    name = text.rsplit("/", 1)[-1]
    if _TESTS_DIR.search(text) or _any(name, _TEST_NAMES):
        return ["tests"]
    if text.startswith(_DOCS_PREFIXES) or name.endswith(".md"):
        return ["docs"]
    if (text.startswith(_INFRA_PREFIXES) or name == "Dockerfile"
            or ("/" not in text and _any(name, _INFRA_ROOT_NAMES)) or name.startswith("install")):
        return ["infra"]
    if text.startswith(_UI_PREFIXES) or _any(name, _UI_NAMES):
        return ["ui"]
    return ["backend"]


def tags_for(entry, path) -> list[str]:
    """The tags a record entry stores, else the defaults for the finding's file."""
    stored = entry.get("tags") if isinstance(entry, dict) else None
    if isinstance(stored, list) and stored:
        return [tag for tag in stored if isinstance(tag, str)]
    return default_tags(path)
