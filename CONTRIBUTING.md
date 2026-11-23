# Contributing to forge

Thanks for wanting to help. forge is a small project with one maintainer, so this page is
mostly about making a change easy to review and safe to merge.

- [Before you start](#before-you-start)
- [Set up](#set-up)
- [Run the tests](#run-the-tests)
- [Rules the code follows](#rules-the-code-follows)
- [Docs and the changelog](#docs-and-the-changelog)
- [Commits and pull requests](#commits-and-pull-requests)
- [Reporting bugs and security issues](#reporting-bugs-and-security-issues)
- [Conduct](#conduct)
- [License](#license)

## Before you start

- **Small fixes** (a typo, a broken link, an obvious bug with a test): open a pull request
  directly.
- **Anything larger** (a new flag, a new harness, a change to how runs are planned, reviewed or
  merged, a new platform): open an issue first and describe what you want to change and why.
  forge has many moving parts that depend on each other, and agreeing on the approach before
  you write it saves you a rewrite.
- Issues labelled [`good first issue`](https://github.com/Giamme/forge/labels/good%20first%20issue)
  and [`help wanted`](https://github.com/Giamme/forge/labels/help%20wanted) are a good place to
  start.

## Set up

You need bash, git and Python 3.9 or later. forge's own code uses only the Python standard
library, so there is nothing to `pip install` for the main suite. `node` is optional: when it
is on `PATH`, the checks also syntax-check forge-ui's JavaScript.

```bash
git clone https://github.com/Giamme/forge.git
cd forge
bash tests/check.sh
```

To try your checkout inside an agent CLI, install it from the clone (this links it into every
agent CLI it finds, in place of an npm install):

```bash
bash scripts/forge-install.sh
```

You don't need a model provider account to work on forge. The tests never call a real model.

[Development](docs/development.md) has the repository layout, and
[`references/`](references/) holds the contracts the skill itself reads.

## Run the tests

```bash
bash tests/check.sh
```

This syntax-checks every shell script, runs the forge suite and the forge-ui suite with every
test class in a process of its own, several at a time, and, when `node` is available,
`node --check`s the forge-ui scripts. The full run takes about 40 minutes on 4 CPU cores, and less
with more (it runs up to 8 test classes at once).

While you work, run only what you touched:

```bash
python3 tests/run_tests.py test_schedule          # classes whose id contains the pattern
cd tests && python3 -m unittest test_schedule     # one forge module (run it from tests/)
cd ui && python3 -m unittest discover -s tests -p 'test_reader.py'   # one forge-ui module (run it from ui/)
```

Then run the full `bash tests/check.sh` once before you open the pull request.

Optional suites skip when their dependencies are missing: the browser tests need Python
Playwright, and the Fractal integration tests need a pinned Fractal runtime. A green run without
them does not exercise those parts. [tests/README.md](tests/README.md) explains how to run them,
along with the fake model CLIs and the `SCENARIO` file that steers them.

### Platforms

forge supports **macOS and Linux**. macOS ships Bash 3.2, so a change can pass on Linux and
still break there.

CI ([`.github/workflows/test.yml`](.github/workflows/test.yml)) runs `bash tests/check.sh` on
every pull request, on Linux with Python 3.9, and its **Tests passed** check has to pass before
merging. To keep pull requests quick, macOS and Linux with the latest Python 3 run only after a
merge to `dev`. So if your change touches shell scripts, run the suite on macOS too if you can,
and say in the pull request if you couldn't. A pull request that only changes documentation
skips the tests. Every pull request also runs the suite on Windows (Git Bash), except the tests
listed in [`tests/windows-pending.txt`](tests/windows-pending.txt), which need parts of the
Windows port that have not landed yet. If your change makes one of them pass on Windows, delete
its line.

## Rules the code follows

These are the things reviewers check most often:

- **Bash 3.2.** Shell scripts must run on macOS's system Bash: no associative arrays,
  `mapfile`, `${var,,}`, `|&` or other Bash 4+ features.
- **Python 3.9 and the standard library only.** No third-party imports in `scripts/` or
  `ui/forge_ui/`. Python is how forge stays portable, so prefer a small Python helper over a
  long shell pipeline.
- **No new external commands.** The tests run with `PATH` restricted to fake model CLIs and an
  explicit list of utilities. Don't call `cut`, `seq`, `ps`, `find`, `perl`, `setsid` or other
  commands outside that list (see [tests/README.md](tests/README.md)).
- **Tests never spend model quota.** Use the fake CLIs. A test that would reach a real
  provider, the network, or your real `HOME` is a bug.
- **forge never touches the user's branch without approval.** Solo changes stay uncommitted,
  decomposed work lands only through `integrate --approved`, and forge never pushes. Keep it
  that way. See [Safety](docs/safety.md).
- **Platform-specific code stays behind a check** for the platform, so the code path on macOS
  and Linux is unchanged.
- **Add a test with the change.** A bug fix comes with a test that fails without it.
- **Lint clean.** CI runs [ruff](https://docs.astral.sh/ruff/) with only the pyflakes rules
  ([`ruff.toml`](ruff.toml)): unused imports and variables, undefined names, repeated dict keys.
  There are no formatting rules. To check before you push, run `ruff check .` (or
  `pipx run ruff check .`) from the repository root. ruff is a tool you run, not a dependency
  of forge.

## Docs and the changelog

- **Docs.** If a change is visible to users (a flag, a command, an output, a default), update
  the page in [`docs/`](docs/) that covers it, and [`SKILL.md`](SKILL.md) or
  [`references/`](references/) when it changes what the orchestrating model is told.
  [docs/README.md](docs/README.md) lists the pages.
- **Changelog.** For a user-visible change, add a fragment to
  [`.changelog/unreleased/`](.changelog/unreleased/) as described in
  [.changelog/README.md](.changelog/README.md). Write it for people who use forge, not for
  people reading the diff. Docs, tests, CI and refactoring need no fragment. If you're unsure,
  leave it out and say so in the pull request; the maintainer will add one.
- Don't bump `VERSION` or `package.json`. Versions are set at release time.

## Commits and pull requests

- **Commit messages** use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat: …`, `fix: …`, `docs: …`, `test: …`, `refactor: …`, `ci: …`, `chore: …`, with an
  optional scope such as `fix(route): …`. The changelog tooling reads these prefixes.
- **One topic per pull request.** Unrelated fixes are easier to review, and to revert, apart.
- **Explain why**, not only what. If you chose between approaches, say which and why. Link the
  issue it resolves (`Closes #123`).
- **Say what you tested** and on which platforms. The pull request template has a checklist.
- **Keep the pull request up to date** by rebasing on `main` or merging it in; either is fine.
- If a week passes without a review, a comment on the pull request is welcome.

Using an AI coding assistant to write a change is fine (forge is one). You are still the
author: read the diff, run the tests, and be ready to explain any line of it.

## Reporting bugs and security issues

- **Bugs:** open an [issue](https://github.com/Giamme/forge/issues/new/choose) with the
  bug report form. Include the output of `forge version` and of the doctor
  (`bash <forge>/scripts/forge-dispatch.sh doctor`), the command you ran, and what you expected.
  [Troubleshooting](docs/troubleshooting.md) may already cover it.
- **Security issues:** don't open a public issue. See [SECURITY.md](SECURITY.md).

## Conduct

Be kind and assume good intent. Keep discussion about the work, critique code rather than
people, and remember that everyone here is contributing their own time. The maintainer may
remove comments, or block people, who don't follow this.

## License

forge is released under the [MIT License](LICENSE). By contributing, you agree that your
contributions are licensed under the same terms. There is no contributor license agreement to
sign.
