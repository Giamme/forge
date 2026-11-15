# forge's website

The home page and the plain-language docs, published with GitHub Pages by
[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) on every push to `main` that
touches this folder. The full reference stays in [`docs/`](../docs/README.md); every docs page
links to its reference page.

No framework and no build dependencies, in the same spirit as forge-ui: plain HTML, CSS and
JavaScript, assembled by a standard-library Python script. three.js 0.160 loads from
cdn.jsdelivr.net through an import map, as in forge-ui; without it (or without WebGL) the page
keeps working and only the 3D is missing.

```sh
python3 site/build.py            # build into site/_site (git-ignored)
python3 site/build.py --serve    # build, then serve on http://localhost:8000
python3 site/build.py --out DIR  # build somewhere else (tests/test_site.py does)
```

The build fails on a broken internal link, and so does `tests/test_site.py` in the suite.

## Layout

```
site/
├── build.py        wraps pages in layout.html, adds the docs sidebar and route strip,
│                   copies assets, forge-ui's D-DIN font and registry.tsv as JSON
├── layout.html     header, footer and <head>, shared by every page
├── pages/
│   ├── index.html  the home page
│   └── docs/       one fragment per docs page; the sidebar order is DOCS_NAV in build.py
└── assets/
    ├── style.css   the Signal Box desk (DESIGN.md's tokens, plates, banks, lamps)
    ├── plate.js    the 3D run plate and run cloud (three.js), demo data only
    ├── home.js     the session replay, spec composer, approval table, steering desk
    ├── site.js     theme bank, copy keys, the docs route map
    └── theme.js    applies a saved theme before first paint
```

Each page starts with a header comment:

```html
<!--
title: Reviews and retries
description: One sentence for search results and link previews.
source: docs/reviews.md
station: review,retry
side: forge-ui
-->
```

`source` links the page to its full reference on GitHub. `station` lights stations on the route
strip (install, plan, approve, build, review, retry, merge, integrate) and `side` names a side
track. A new docs page also needs an entry in `DOCS_NAV`.

Nothing on the site is a screenshot: the run plate, the run cloud, the diff, the approval table
and the leaderboard are built in the page, from labelled demo data. The spec composer reads
`registry.tsv`, the version comes from `VERSION`, and the display face is forge-ui's own, so none
of them need updating here.

The design follows forge-ui's [DESIGN.md](../DESIGN.md). The site's direction contracts (built with
the Impeccable skill) are in `.impeccable/surfaces/site-pages-*.md`.
