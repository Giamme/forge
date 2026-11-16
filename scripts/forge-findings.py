#!/usr/bin/env python3
"""Every review finding of a repository, and what happened to it.

  show REPO [--status S] [--online]
  fix REPO FP… [--commit SHA] [--reason TEXT]
  dismiss REPO FP… --reason TEXT
  reopen REPO FP…
  to-file REPO FP… [--title T] [--body-file F]
  dup REPO FP --of FP
  tag REPO FP… -- TAG… (--add | --remove)
  note REPO FP TEXT
  edit REPO FP [--severity Pn] [--text-file F]

REPO is a checkout, a linked worktree, or a plan or solo run dir. FP is the
12-hex fingerprint `show` prints. `--actor` is cli (the default), ui or triage.
Exit 0 ok, 2 usage, 3 refused, 1 error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_identity  # noqa: E402
from forge_findings import status, store, tags  # noqa: E402

ACTORS = ("cli", "ui", "triage")
FP_RE = re.compile(r"[0-9a-f]{12}\Z")
SEVERITIES = ("P0", "P1", "P2", "P3")

# Later tasks add triage, triage-status, proposals, apply and reject here and in build_parser.
COMMANDS = {}


class Usage(Exception):
    """Exit 2. The command line is not usable. Nothing was written."""


class Refused(Exception):
    """Exit 3. The record refuses the change. Nothing was written."""


class Failed(Exception):
    """Exit 1. The command could not be carried out."""


def command(name):
    def wrap(fn):
        COMMANDS[name] = fn
        return fn
    return wrap


_issues = None


def issues():
    """forge-issues.py, loaded by path: its file name is not importable."""
    global _issues
    if _issues is None:
        spec = importlib.util.spec_from_file_location("forge_issues", SCRIPTS / "forge-issues.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _issues = module
    return _issues


def _common():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--actor", choices=ACTORS, default="cli",
                        help="who is recording this: cli (default), ui or triage")
    parent.add_argument("--json", action="store_true", help="machine-readable output")
    return parent


def build_parser():
    """The command line. Each subcommand takes --actor and --json; handlers live in COMMANDS."""
    common = _common()
    parser = argparse.ArgumentParser(prog="forge findings", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", parents=[common], help="every finding, with its state")
    show.add_argument("repo", help="checkout, linked worktree, plan dir or solo run dir")
    show.add_argument("--status", action="append", default=None, metavar="S",
                      help="keep this status; repeatable; open means open and to-file")
    show.add_argument("--online", action="store_true", help="ask GitHub which findings are already filed")

    fix = sub.add_parser("fix", parents=[common], help="mark findings fixed")
    fix.add_argument("repo")
    fix.add_argument("fp", nargs="+", metavar="FP")
    fix.add_argument("--commit", help="7 to 40 lowercase hex characters")
    fix.add_argument("--reason", help="at most 500 characters")

    dismiss = sub.add_parser("dismiss", parents=[common], help="mark findings as needing no work")
    dismiss.add_argument("repo")
    dismiss.add_argument("fp", nargs="+", metavar="FP")
    dismiss.add_argument("--reason", required=True, help="why, at most 500 characters")

    reopen = sub.add_parser("reopen", parents=[common], help="mark findings open again")
    reopen.add_argument("repo")
    reopen.add_argument("fp", nargs="+", metavar="FP")

    to_file = sub.add_parser("to-file", parents=[common], help="keep a draft issue for findings")
    to_file.add_argument("repo")
    to_file.add_argument("fp", nargs="+", metavar="FP")
    to_file.add_argument("--title", help="issue title, at most 200 characters")
    to_file.add_argument("--body-file", metavar="F", help="read the issue body from this file")

    dup = sub.add_parser("dup", parents=[common], help="mark a finding a duplicate of another")
    dup.add_argument("repo")
    dup.add_argument("fp", metavar="FP")
    dup.add_argument("--of", required=True, metavar="FP", help="the finding this one repeats")

    tag = sub.add_parser("tag", parents=[common],
                         help="add or remove tags (-- separates fingerprints from tags)")
    tag.add_argument("repo")
    tag.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    note = sub.add_parser("note", parents=[common], help="set a finding's note; empty text clears it")
    note.add_argument("repo")
    note.add_argument("fp", metavar="FP")
    note.add_argument("text", help="the note, or empty to clear it")

    edit = sub.add_parser("edit", parents=[common], help="edit a finding's severity or text")
    edit.add_argument("repo")
    edit.add_argument("fp", metavar="FP")
    edit.add_argument("--severity", choices=SEVERITIES, help="P0, P1, P2 or P3")
    edit.add_argument("--text-file", metavar="F", help="read the replacement text from this file")
    return parser


def require_ident(path):
    ident = forge_identity.identity(path)
    if ident is None:
        raise Usage(f"{path}: not a repository or run dir")
    return ident


def fingerprints(values):
    """The fingerprints of one command, in order, each once. A bad one refuses the whole command."""
    found = []
    for fp in values:
        if not isinstance(fp, str) or FP_RE.fullmatch(fp) is None:
            raise Refused(f"{fp!r} is not a 12-character fingerprint")
        if fp not in found:
            found.append(fp)
    if not found:
        raise Usage("at least one fingerprint")
    return found


def read_payload(path, label, limit):
    """The text of a file. `-` is not a file. Longer than `limit` is refused."""
    if path == "-":
        raise Usage(f"{label}: '-' is not accepted; pass a file")
    try:
        with open(path, encoding="utf-8") as handle:
            data = handle.read(limit + 1)
    except UnicodeDecodeError as exc:
        raise Failed(f"{label}: {path}: {exc}") from exc
    except OSError as exc:
        raise Failed(f"{label}: {path}: {exc.strerror or exc}") from exc
    if len(data) > limit:
        raise Refused(f"{label} must be at most {limit} characters")
    return data


def call_store(fn):
    try:
        return fn()
    except store.Invalid as exc:
        raise Refused(str(exc)) from exc
    except store.Busy as exc:
        raise Refused(str(exc)) from exc
    except store.Corrupt as exc:
        raise Failed(str(exc)) from exc


def emit(args, fps, changed):
    changed_fps = {fp for fp, _before, _after in changed}
    unchanged = [fp for fp in fps if fp not in changed_fps]
    if args.json:
        print(json.dumps({
            "ok": True,
            "op": args.command,
            "changed": [{"fp": fp, "before": before, "after": after} for fp, before, after in changed],
            "unchanged": unchanged,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"{args.command}: {len(changed)} changed, {len(unchanged)} unchanged")
    return 0


def apply(args, fps, fn):
    """One store call for the whole command. `fn(ident, fps, actor)` returns the store's changed list."""
    ident = require_ident(args.repo)
    fps = fingerprints(fps)
    return emit(args, fps, call_store(lambda: fn(ident, fps, args.actor)))


def _wanted(names):
    """Statuses `--status` keeps. `open` is every status that still needs work."""
    if not names:
        return None
    chosen = set()
    for name in names:
        if name == "open":
            chosen.update(status.OPEN)
        else:
            chosen.add(name)
    return chosen


def _recency(row):
    plan_dir = row.get("plan_dir")
    if isinstance(plan_dir, str) and plan_dir:
        try:
            return os.stat(plan_dir).st_mtime
        except OSError:
            return 0.0
    return 0.0


def _severity_rank(value):
    try:
        return SEVERITIES.index(value)
    except ValueError:
        return len(SEVERITIES)


def _place(row):
    file = row.get("file") if isinstance(row.get("file"), str) else ""
    line = row.get("line")
    if file and isinstance(line, int) and not isinstance(line, bool):
        return f"{file}:{line}"
    return file or "-"


def _show_line(row):
    found = [tag for tag in (row.get("tags") or []) if isinstance(tag, str)]
    summary = " ".join(str(row.get("summary") or "").split())
    return (f"{row.get('fp')}  {row.get('status')}  {row.get('severity') or '-'}  "
            f"{','.join(found) if found else '-'}  {_place(row)}  {summary}")


def _rows(listed, records):
    rows = []
    seen = set()
    for entry in listed.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        fp = entry.get("fp")
        if fp in seen:
            continue
        seen.add(fp)
        record = records.get(fp)
        row = dict(entry)
        row["record"] = record
        row["tags"] = tags.tags_for(record, entry.get("file"))
        row["status"] = status.effective(record, entry, None)
        rows.append(row)
    rows.sort(key=lambda row: (_severity_rank(row.get("severity")), -_recency(row)))
    return rows


@command("show")
def cmd_show(args):
    ident = require_ident(args.repo)
    records = call_store(lambda: store.load(ident))
    module = issues()
    try:
        listed = module.listing(args.repo, online=bool(args.online))
    except module.Busy as exc:
        raise Refused(str(exc)) from exc
    except module.Fail as exc:
        raise Failed(str(exc)) from exc
    rows = _rows(listed, records)
    chosen = _wanted(args.status)
    if chosen is not None:
        rows = [row for row in rows if row.get("status") in chosen]
    if args.json:
        print(json.dumps({
            "repo": listed.get("repo") or "",
            "root": ident.get("root"),
            "key": ident.get("key"),
            "findings": rows,
            "notes": list(listed.get("notes") or []),
            "warnings": list(listed.get("warnings") or []),
        }, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(_show_line(row))
    return 0


@command("fix")
def cmd_fix(args):
    if args.commit is None and args.reason is None:
        raise Refused("fixed needs a commit and/or a reason")
    fields = {}
    if args.commit is not None:
        fields["commit"] = args.commit
    if args.reason is not None:
        fields["reason"] = args.reason
    return apply(args, args.fp, lambda ident, fps, actor: store.set_status(ident, fps, "fixed", actor, **fields))


@command("dismiss")
def cmd_dismiss(args):
    return apply(args, args.fp,
                 lambda ident, fps, actor: store.set_status(ident, fps, "dismissed", actor, reason=args.reason))


@command("reopen")
def cmd_reopen(args):
    return apply(args, args.fp, lambda ident, fps, actor: store.set_status(ident, fps, "open", actor))


@command("to-file")
def cmd_to_file(args):
    kwargs = {"status": "to-file"}
    if args.title is not None or args.body_file is not None:
        body = read_payload(args.body_file, "--body-file", store.MAX_BODY) if args.body_file else ""
        kwargs["draft"] = {"title": args.title or "", "body": body}
    return apply(args, args.fp, lambda ident, fps, actor: store.update(ident, fps, actor, **kwargs))


@command("dup")
def cmd_dup(args):
    fps = fingerprints([args.fp])
    of = fingerprints([args.of])[0]
    if fps[0] == of:
        raise Refused(f"{fps[0]}: a finding cannot be a duplicate of itself")
    return apply(args, fps, lambda ident, found, actor: store.mark_duplicate(ident, found[0], of, actor))


def _take_common(args, tokens):
    """`--json` and `--actor` inside a remainder, which argparse does not parse."""
    kept = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--json":
            args.json = True
            index += 1
            continue
        if token == "--actor" or token.startswith("--actor="):
            if token == "--actor":
                if index + 1 >= len(tokens):
                    raise Usage("--actor needs one of cli, ui, triage")
                value = tokens[index + 1]
                index += 2
            else:
                value = token.split("=", 1)[1]
                index += 1
            if value not in ACTORS:
                raise Usage(f"--actor must be one of {', '.join(ACTORS)}")
            args.actor = value
            continue
        kept.append(token)
        index += 1
    return kept


def _parse_tag(tokens):
    """(fps, tags, 'add'|'remove'). `--` separates fingerprints from tags; tags may follow the flag."""
    modes = [token for token in tokens if token in ("--add", "--remove")]
    if not modes:
        raise Usage("tag needs --add or --remove")
    if len(modes) != 1:
        raise Usage("tag takes one of --add or --remove")
    mode = modes[0][2:]
    if "--" in tokens:
        dash = tokens.index("--")
        before, after = tokens[:dash], tokens[dash + 1:]
        fps = [token for token in before if token != modes[0]]
        found = [token for token in after if token != modes[0]]
    else:
        flag = tokens.index(modes[0])
        fps, found = tokens[:flag], tokens[flag + 1:]
    for token in fps + found:
        if token.startswith("-"):
            raise Usage(f"tag: unknown option {token}")
    if not fps:
        raise Usage("tag needs a fingerprint")
    if not found:
        raise Usage("tag needs a tag")
    return fps, found, mode


@command("tag")
def cmd_tag(args):
    fps, found, mode = _parse_tag(_take_common(args, list(args.rest or [])))
    if mode == "add":
        return apply(args, fps, lambda ident, items, actor: store.add_tags(ident, items, found, actor))
    return apply(args, fps, lambda ident, items, actor: store.remove_tags(ident, items, found, actor))


@command("note")
def cmd_note(args):
    return apply(args, [args.fp], lambda ident, fps, actor: store.set_note(ident, fps[0], args.text, actor))


@command("edit")
def cmd_edit(args):
    if args.severity is None and args.text_file is None:
        raise Usage("edit needs --severity or --text-file")
    text = read_payload(args.text_file, "--text-file", store.MAX_EDIT_TEXT) if args.text_file else None
    severity = args.severity
    return apply(args, [args.fp],
                 lambda ident, fps, actor: store.set_edit(ident, fps[0], actor, severity=severity, text=text))


def report(exc, code, args):
    message = " ".join(str(exc).split())
    print(f"forge findings: {message}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    return code


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except Usage as exc:
        return report(exc, 2, args)
    except Refused as exc:
        return report(exc, 3, args)
    except Failed as exc:
        return report(exc, 1, args)


if __name__ == "__main__":
    sys.exit(main())
