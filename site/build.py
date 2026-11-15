#!/usr/bin/env python3
"""Build forge's website into site/_site/.

Standard library only, no build step beyond this script: each page in site/pages is an HTML
fragment with a small header, wrapped in site/layout.html. The output is plain static files,
ready for GitHub Pages.

    python3 site/build.py            # build into site/_site
    python3 site/build.py --serve    # build, then serve it on http://localhost:8000
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
REPO = SITE.parent
OUT = SITE / "_site"
GITHUB = "https://github.com/Giamme/forge"

# The docs sidebar, in reading order: (group, [(slug, label), ...]).
DOCS_NAV = [
    ("Start here", [
        ("index", "How forge works"),
        ("install", "Install"),
        ("first-run", "Your first run"),
    ]),
    ("The run", [
        ("models", "Models and CLIs"),
        ("parallel-runs", "Parallel runs"),
        ("brief", "Questions before a task"),
        ("reviews", "Reviews and retries"),
        ("verification", "Merge and verification"),
    ]),
    ("While it runs", [
        ("steering", "Steering a live run"),
        ("forge-ui", "forge-ui"),
        ("following", "From the terminal"),
    ]),
    ("Over time", [
        ("memory", "Project memory"),
        ("measuring", "Measuring models"),
        ("disk", "Disk and cleanup"),
    ]),
    ("Reference", [
        ("safety", "What forge never does"),
        ("troubleshooting", "When something goes wrong"),
        ("roadmap", "What's coming"),
        ("reference", "Full reference"),
    ]),
]

# The route strip every docs page carries as its running head: one run, start to finish.
STATIONS = [
    ("install", "Install"),
    ("plan", "Plan"),
    ("approve", "Approve"),
    ("build", "Build"),
    ("review", "Review"),
    ("retry", "Retry"),
    ("merge", "Merge"),
    ("integrate", "Integrate"),
]
STATION_PAGE = {
    "install": "install", "plan": "parallel-runs", "approve": "parallel-runs", "build": "first-run",
    "review": "reviews", "retry": "reviews", "merge": "verification", "integrate": "verification",
}

HEADER_RE = re.compile(r"\A\s*<!--(.*?)-->\s*", re.S)


def read_page(path: Path) -> tuple[dict[str, str], str]:
    """Split a page into its header fields and its HTML body."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    match = HEADER_RE.match(text)
    if match:
        for line in match.group(1).strip().splitlines():
            key, _, value = line.partition(":")
            if value:
                meta[key.strip()] = value.strip()
        text = text[match.end():]
    return meta, text


def docs_order() -> list[tuple[str, str]]:
    return [item for _, items in DOCS_NAV for item in items]


def render_sidebar(current: str, root: str) -> str:
    parts = []
    for group, items in DOCS_NAV:
        links = []
        for slug, label in items:
            attrs = ' aria-current="page"' if slug == current else ""
            links.append(f'<li><a href="{root}docs/{slug}.html"{attrs}>{html.escape(label)}</a></li>')
        parts.append(f'<div class="side-group"><p class="plate">{html.escape(group)}</p><ul>{"".join(links)}</ul></div>')
    return "".join(parts)


def render_strip(stations: list[str], side: str, root: str) -> str:
    """The route strip: the run's stations as an inlaid track, the page's own lit."""
    cells = []
    for key, label in STATIONS:
        lit = key in stations
        cls = "station is-lit" if lit else "station"
        attrs = ' aria-current="step"' if lit else ""
        cells.append(
            f'<li class="{cls}"{attrs}><a href="{root}docs/{STATION_PAGE[key]}.html">'
            f'<span class="lamp"></span><span class="station-name">{label}</span></a></li>'
        )
    side_html = ""
    if side:
        side_html = f'<p class="strip-side"><span class="lamp is-route"></span>Beside the run: {html.escape(side)}</p>'
    return (
        f'<nav class="strip" aria-label="Where this page sits in a run"><ol>{"".join(cells)}</ol>{side_html}</nav>'
    )


def render_pager(current: str, root: str) -> str:
    order = docs_order()
    slugs = [slug for slug, _ in order]
    if current not in slugs:
        return ""
    i = slugs.index(current)
    cells = []
    if i > 0:
        slug, label = order[i - 1]
        cells.append(f'<a class="pager-prev" href="{root}docs/{slug}.html"><span>Previous</span>{html.escape(label)}</a>')
    else:
        cells.append("<span></span>")
    if i < len(order) - 1:
        slug, label = order[i + 1]
        cells.append(f'<a class="pager-next" href="{root}docs/{slug}.html"><span>Next</span>{html.escape(label)}</a>')
    return f'<nav class="pager" aria-label="Docs pages">{"".join(cells)}</nav>'


def registry_json() -> list[dict[str, str]]:
    """registry.tsv as data for the home page's spec composer: forge's own aliases, never a copy."""
    rows = []
    for line in (REPO / "registry.tsv").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        rows.append({"alias": cols[0], "harness": cols[1], "model": cols[2], "effort": cols[3]})
    return rows


def build() -> int:
    layout = (SITE / "layout.html").read_text(encoding="utf-8")
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    known = {slug for slug, _ in docs_order()}
    built = 0
    for path in sorted((SITE / "pages").rglob("*.html")):
        rel = path.relative_to(SITE / "pages")
        root = "../" * (len(rel.parts) - 1)
        meta, body = read_page(path)
        is_doc = rel.parts[0] == "docs"
        slug = rel.stem

        if is_doc and slug not in known:
            print(f"warning: {rel} is not in the docs sidebar", file=sys.stderr)

        if is_doc:
            # tables scroll sideways on a narrow screen instead of squeezing their code
            body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
            stations = [s.strip() for s in meta.get("station", "").split(",") if s.strip()]
            strip = "" if slug == "index" else render_strip(stations, meta.get("side", ""), root)
            source = meta.get("source")
            ref = ""
            if source:
                ref = (
                    f'<p class="doc-ref"><span class="plate">Full reference</span> Every flag and edge case: '
                    f'<a href="{GITHUB}/blob/main/{source}">{html.escape(source)}</a></p>'
                )
            sidebar = render_sidebar(slug, root)
            content = (
                f'<div class="docs{" docs-index" if slug == "index" else ""}">'
                f'<details class="side-toggle"><summary>Docs pages</summary><nav class="side" aria-label="Docs">{sidebar}</nav></details>'
                f'<nav class="side side-desk" aria-label="Docs">{sidebar}</nav>'
                f'<div class="doc-main">{strip}<article class="doc">{body}{ref}{render_pager(slug, root)}</article></div>'
                "</div>"
            )
        else:
            content = body

        title = meta.get("title", "forge")
        full_title = f"{title} · forge docs" if is_doc else title
        rendered = (
            layout.replace("{{content}}", content)
            .replace("{{title}}", html.escape(full_title))
            .replace("{{description}}", html.escape(meta.get("description", "")))
            .replace("{{page_class}}", "page-doc" if is_doc else f"page-{slug}")
            .replace("{{nav_home}}", "" if is_doc else ' aria-current="page"')
            .replace("{{nav_docs}}", ' aria-current="page"' if is_doc else "")
            .replace("{{version}}", html.escape(version))
            .replace("{{github}}", GITHUB)
            .replace("{{root}}", root)
        )
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        built += 1

    shutil.copytree(SITE / "assets", OUT / "assets")
    # forge-ui's own display face, so the site and the viewer are set in the same letters.
    fonts = OUT / "assets" / "fonts"
    fonts.mkdir(exist_ok=True)
    shutil.copy(REPO / "ui" / "forge_ui" / "static" / "D-DINCondensed-Bold.woff2", fonts / "D-DINCondensed-Bold.woff2")
    (OUT / "assets" / "registry.json").write_text(json.dumps(registry_json(), indent=0), encoding="utf-8")
    # GitHub Pages: serve files as they are, without Jekyll.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    problems = check_links()
    for problem in problems:
        print(f"broken link: {problem}", file=sys.stderr)
    print(f"built {built} pages into {OUT.relative_to(REPO)}")
    return 1 if problems else 0


def check_links() -> list[str]:
    """Every relative href and src in the output must point at a file that exists."""
    problems = []
    for page in OUT.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:href|src)="([^"]+)"', text):
            if re.match(r"^(?:[a-z]+:|#|//)", ref):
                continue
            target = ref.split("#", 1)[0]
            if target and not (page.parent / target).resolve().exists():
                problems.append(f"{page.relative_to(OUT)} -> {ref}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serve", action="store_true", help="serve the built site on localhost")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    status = build()
    if args.serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
        print(f"serving on http://localhost:{args.port}")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return status


if __name__ == "__main__":
    sys.exit(main())
