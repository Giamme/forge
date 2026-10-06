# .changelog

Changelog entries that have not been released yet. Maintained with the changelogger skill.

- `unreleased/` holds one Markdown fragment per user-visible change, written when the change is made.
  `skipped.txt` lists commits deliberately left out of the changelog.
- `released/<version>/` keeps the fragments of the most recent release as a backup.
- `changelog.json` is the structured release history (the source for "What's new" views and release notes).
- `config.json` holds settings: version files bumped together, changelog style, tag format, ignore rules.

A fragment looks like this (file name: anything ending in `.md`):

```
---
type: fixed          # added | changed | deprecated | removed | fixed | security
breaking: false
audience: user       # user | internal (internal entries stay out of What's new)
commits: 3f2a9c1
---
Exported CSV files no longer drop the last row.
```

Write it for the people who use the software, not for the people reading the diff.
