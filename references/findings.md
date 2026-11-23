# Leftover findings

A run can finish with review findings nobody will act on. This page is what the orchestrator does with
them: triage, and filing them as GitHub issues according to the user's `--file-issues` choice. The
commands themselves are in [commands](../docs/commands.md#forge-issues) and
[`forge findings`](../docs/commands.md#forge-findings).

## Contents

- [What counts as leftover](#what-counts-as-leftover)
- [Triage](#triage)
- [Which filer](#which-filer)
- [The --file-issues choice](#the---file-issues-choice)
- [With the findings gate on](#with-the-findings-gate-on)
- [Rules for both filers](#rules-for-both-filers)

## What counts as leftover

- CONFIRMED findings the threshold tolerated, from a PASS or a FAIL (`tasks/<id>/known_issues.md`);
- PLAUSIBLE findings in a passed task's `qa.last`;
- whatever a whole-run review left open.

`forge findings` holds what happened to each finding (fixed, dismissed, duplicate, to file), and a finding
marked `to-file` is offered with the others.

## Triage

Triage (`forge findings triage REPO --spec S`) needs a model the user names, never one you choose. Offer
it, never start it unprompted. Relay its proposals, and run `forge findings apply` only on the user's
explicit word for each one.

## Which filer

When there are any leftover findings, act on the user's `--file-issues` choice with one filer:

- **gh-proman**, only when it is installed and set up for the repository: `gh-proman -C <repo> config show`
  exits 0. It also puts the issues on the team's board with a priority. Run `gh-proman findings --from
  <plan or run dir> --include-plausible --json`, then follow the gh-proman skill's review-findings steps.
- **forge's own filer** otherwise (gh-proman missing, or not configured for this repo; never configure it
  yourself): `python3 <skill_dir>/scripts/forge-issues.py <plan or run dir> --include-plausible --json`,
  plain `gh issue create`. Pass `--lang` matching the repo's existing issues, and `--label bug` when the
  repo has that label. If `gh` is missing, unauthenticated or the repo has no GitHub remote, list the
  findings in the report and say why none were filed.

## The --file-issues choice

- `ask` (the default): show the dry-run summary (count by severity, titles, anything already filed) and
  ask once: **"File these N findings as GitHub issues? [all / CONFIRMED only / none]"**. Then re-run with
  `--apply`, adding `--include-plausible` only for "all".
- `auto`: file them without asking (`--apply`, PLAUSIBLE included as "to verify" issues) and report what
  was created and skipped.
- `off`: never offer.

## With the findings gate on

With the `findings` gate on (`forge decide gate findings on`), forge's filer adds `preselect` and `reason`
to each finding in the dry-run JSON, and `--apply` turns a reworded duplicate of an open issue into a
comment on it (`comments`, `commented`). `ask` shows the pre-selected findings and the reason for each one
left out, and files the ones the user keeps with `--only <fps> --max-issues <count>`. `auto` files only
the pre-selected ones the same way.

## Rules for both filers

`--file-issues` is an instruction to you: never forward it to a runner. Skip INFRA, interrupted and
still-failing runs. Both filers mark each issue with the same finding fingerprint and skip a finding
either one already filed, so offering again after a retry only brings up new ones. A draft flagged as a
possible duplicate is skipped on `--apply`: show it, and file it (`--confirm-new`) only if the user says
it is different.
