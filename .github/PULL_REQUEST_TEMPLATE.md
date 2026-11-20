<!-- Thanks for contributing! CONTRIBUTING.md has the details behind each section. -->

## What and why

<!-- What does this change, and why? If you chose between approaches, say which and why. -->

Closes #

## How it was tested

<!-- Tests you added or changed, and anything you checked by hand. -->

CI runs `bash tests/check.sh` on Linux and Windows for this pull request. macOS only runs
after the merge, so tick this if you ran the suite there yourself.

- [ ] macOS

## Checklist

- [ ] The change comes with a test (a bug fix with one that fails without it)
- [ ] Shell stays Bash 3.2 compatible, Python stays standard library only, no new external commands
- [ ] Docs updated (`docs/`, and `SKILL.md` or `references/` if what the model is told changed)
- [ ] A `.changelog/unreleased/` fragment for a user-visible change, or none needed
- [ ] Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:` …)
