# Security policy

## Reporting a vulnerability

Please don't report security issues in public issues, pull requests or discussions.

Report them privately through GitHub:
[**Report a vulnerability**](https://github.com/Giamme/forge/security/advisories/new)
(the repository's Security tab → Advisories → Report a vulnerability). Include what you found,
how to reproduce it, the forge version (`forge version`) and your platform.

You'll get an acknowledgement as soon as the maintainer sees it. Once a fix is released, the
advisory is published, crediting you unless you'd rather not be named.

## Supported versions

Only the latest release gets security fixes. Update with
`npm install -g @giamme/forge@latest && forge install`.

## What counts

forge runs agent CLIs on your machine and changes your repositories, so these are in scope:

- forge changing, committing to or pushing a branch without the approval
  [Safety](docs/safety.md) says it needs
- a dwarf or reviewer getting more access than [Safety](docs/safety.md#what-each-role-can-do-per-harness)
  says it has on its harness (escaping codex's sandbox, a reviewer changing the tree it judges
  without forge noticing, a dwarf leaving its worktree), other than through the `--yolo-*` flags
  that ask for exactly that
- forge-ui serving files outside the run it was asked for, being reachable from another
  machine, or running script from run data
- the installer or the npm package writing outside the places [Install](docs/install.md)
  lists
- secrets or credentials leaking into run directories, exports, the dataset or logs

What a model does with access you deliberately gave it (for example with `--yolo-dwarf`) is not
a forge vulnerability, but a report that forge's documentation understates that access is
welcome.
