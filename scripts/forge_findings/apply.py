"""Approve or reject one proposal of a triage (#161).

`apply` is the only writer of what a triage proposed. An approval names the proposal's hash: the
hash is recomputed from `proposals.json`, so a proposal that changed after the person read it is
refused. The effect goes through the finding record (`store`), with the actor `triage`; the person
who asked is kept in the proposal's `decided`. A `plan` proposal becomes a plan dir that
`forge-planner.py parse` has accepted; nothing is dispatched and GitHub is never contacted.

The whole of one apply (checks, effect, bookkeeping) runs under a lock file in the triage dir, so two
approvals of one proposal cannot both apply. Any failure leaves the proposal `pending`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_identity  # noqa: E402
import forge_side  # noqa: E402
from forge_findings import store, triage  # noqa: E402

LOCK_NAME = ".apply.lock"
MAX_REASON = 500
PLANNER_TIMEOUT = 120
PID_RE = re.compile(r"p[0-9]+\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")
ACTOR = "triage"
PARALLEL = "bash scripts/forge-parallel.sh"


class Refused(Exception):
    """Exit 3. The proposal is not applied; nothing changed."""


class Failed(Exception):
    """Exit 1. The effect could not be carried out; the proposal stays pending."""


# --- the proposal ---------------------------------------------------------------------------------

def _load(ident, n, pid):
    """`(dir, data, proposal)` of one proposal. Refused for an unknown triage, file or proposal id."""
    if not isinstance(pid, str) or PID_RE.fullmatch(pid) is None:
        raise Refused(f"{pid!r} is not a proposal id (p1, p2, ...)")
    found = triage.triage_dir(ident["key"], n)
    if found is None:
        raise Refused(f"no triage {n}")
    dest = found[1]
    data = triage.read_json(dest / "proposals.json")
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        state = triage.read_state(dest) or {}
        raise Refused(f"triage {n} has no proposals ({state.get('state', 'no state')})")
    for item in data["proposals"]:
        if isinstance(item, dict) and item.get("id") == pid:
            return dest, data, item
    raise Refused(f"triage {n} has no proposal {pid}")


def _require_pending(n, pid, proposal):
    if proposal.get("state") != "pending":
        raise Refused(f"{pid} of triage {n} is {proposal.get('state')}, not pending")


def _fps(proposal):
    """Every fingerprint a proposal names, in order, each once."""
    kind = proposal.get("kind")
    if kind in ("duplicate", "fixed"):
        names = [proposal.get("fp")]
        if kind == "duplicate":
            names.append(proposal.get("of"))
    else:
        names = proposal.get("fps")
        if not isinstance(names, list):
            raise Refused(f"{proposal.get('id')}: fps must be a list")
    for name in names:
        if not isinstance(name, str):
            raise Refused(f"{proposal.get('id')}: {name!r} is not a fingerprint")
    return list(dict.fromkeys(names))


def _text(proposal, name):
    value = proposal.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{proposal.get('id')}: {name} must be a non-empty string")
    return value


def _check_known(proposal, rows):
    known = {row.get("fp") for row in rows if isinstance(row, dict)}
    for fp in _fps(proposal):
        if fp not in known:
            raise Refused(f"{fp}: not a finding of this repository now")
    if proposal.get("kind") == "duplicate" and proposal.get("fp") == proposal.get("of"):
        raise Refused(f"{proposal.get('fp')}: a finding cannot be a duplicate of itself")


# --- the effects ----------------------------------------------------------------------------------

def _call(fn):
    try:
        return fn()
    except store.Invalid as exc:
        raise Refused(str(exc)) from exc
    except store.Busy as exc:
        raise Refused(str(exc)) from exc
    except store.Corrupt as exc:
        raise Failed(str(exc)) from exc


def _cut(text):
    return text[:MAX_REASON]


def _effect_duplicate(ident, proposal):
    fp, of = proposal["fp"], proposal["of"]
    return _call(lambda: store.update(
        ident, [fp], ACTOR, status="duplicate", duplicate_of=of, reason=_cut(_text(proposal, "why"))))


def _effect_fixed(ident, proposal):
    fields = {"reason": _cut(_text(proposal, "evidence"))}
    commit = proposal.get("commit")
    if isinstance(commit, str) and COMMIT_RE.fullmatch(commit):
        fields["commit"] = commit
    return _call(lambda: store.set_status(ident, [proposal["fp"]], "fixed", ACTOR, **fields))


def _effect_tag(ident, proposal):
    tags = proposal.get("tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) for tag in tags):
        raise Refused(f"{proposal.get('id')}: tags must be a list of tags")
    return _call(lambda: store.add_tags(ident, _fps(proposal), tags, ACTOR))


def _effect_issue(ident, proposal):
    draft = {"title": _text(proposal, "title"), "body": _text(proposal, "body")}
    return _call(lambda: store.update(ident, _fps(proposal), ACTOR, status="to-file", draft=draft))


def _place(row):
    file = row.get("file") if isinstance(row.get("file"), str) else ""
    line = row.get("line")
    if file and isinstance(line, int) and not isinstance(line, bool):
        return f"{file}:{line}"
    return file or "-"


def _request_text(proposal, dest, rows):
    """The proposal's request, then its findings as the triage saw them (findings.json), else as they are now."""
    seen = triage.read_json(Path(dest) / "findings.json")
    by_fp = {}
    for source in ((seen.get("findings") if isinstance(seen, dict) else None) or [], rows):
        for row in source:
            if isinstance(row, dict) and isinstance(row.get("fp"), str):
                by_fp.setdefault(row["fp"], row)
    lines = []
    for fp in _fps(proposal):
        row = by_fp.get(fp, {})
        summary = " ".join(str(row.get("summary") or row.get("text") or "").split())
        lines.append(f"- {fp} [{row.get('severity') or '-'}] {_place(row)}" + (f" — {summary}" if summary else ""))
    return _text(proposal, "request").rstrip("\n") + "\n\n## Findings\n\n" + "\n".join(lines) + "\n"


def default_plan_dir(ident, n, pid) -> Path:
    try:
        root = forge_side._state_root()
    except ValueError as exc:
        raise Failed(str(exc)) from exc
    return root / "forge" / "plans" / ident["key"] / f"triage-{n}-{pid}"


def _existing_checkout(ident):
    for candidate in forge_identity.checkouts(ident):
        if candidate and os.path.isdir(candidate):
            return candidate
    checkout = ident.get("checkout")
    return checkout if isinstance(checkout, str) and checkout else ident.get("root") or "."


def _make_plan(proposal, plan_dir, dest, rows):
    """request.md, then the planner's parse. Raises Refused (planner refusal) or Failed."""
    plan = proposal.get("plan")
    if not isinstance(plan, dict):
        raise Refused(f"{proposal.get('id')}: plan must be an object")
    request = _request_text(proposal, dest, rows)
    try:
        reply = "```forge-plan\n" + json.dumps(plan, ensure_ascii=True) + "\n```\n"
    except (TypeError, ValueError) as exc:
        raise Refused(f"{proposal.get('id')}: the plan cannot be written: {exc}") from exc
    try:
        (plan_dir / "request.md").write_text(request, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Failed(f"cannot write {plan_dir / 'request.md'}: {exc}") from exc
    scratch = tempfile.mkdtemp(prefix="forge-triage-reply-")
    try:
        reply_path = Path(scratch) / "reply.md"
        reply_path.write_text(reply, encoding="utf-8")
        try:
            done = subprocess.run(
                [sys.executable, str(triage.PLANNER), "parse", "--plan", str(plan_dir), "--reply", str(reply_path)],
                capture_output=True, text=True, timeout=PLANNER_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failed(f"the planner did not run: {exc}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if done.returncode == 4:
        reason = " ".join((done.stdout or "").splitlines()[:1]) or "the planner refused the plan"
        raise Refused(reason)
    if done.returncode != 0:
        message = " ".join((done.stderr or done.stdout or "").split())[:300]
        raise Failed(f"the planner failed ({done.returncode}): {message}")


def _create_dir(plan_dir):
    """Make the plan dir; Refused when it exists. Its parents may be created and are kept."""
    if os.path.lexists(plan_dir):
        raise Refused(f"{plan_dir} exists")
    try:
        plan_dir.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(plan_dir)
    except FileExistsError as exc:
        raise Refused(f"{plan_dir} exists") from exc
    except OSError as exc:
        raise Failed(f"cannot create {plan_dir}: {exc.strerror or exc}") from exc


def _effect_plan(ident, proposal, dest, rows, n, pid, plan_dir):
    """`(changed fps, plan dir)`. The dir this call made is removed when anything fails."""
    target = Path(plan_dir) if plan_dir else default_plan_dir(ident, n, pid)
    if not target.is_absolute():
        target = Path(os.path.abspath(target))
    _create_dir(target)
    try:
        _make_plan(proposal, target, dest, rows)
        note = f"in plan {target}"
        fresh = [fp for fp in _fps(proposal) if not ((_call(lambda: store.get(ident, fp)) or {}).get("note"))]
        changed = _call(lambda: store.update(ident, fresh, ACTOR, note=note)) if fresh else []
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return changed, target


# --- bookkeeping ----------------------------------------------------------------------------------

def _decide(dest, data, proposal, state, actor, reason=None, plan_dir=None):
    decided = {"at": triage.now(), "actor": actor}
    if reason:
        decided["reason"] = reason
    proposal["state"] = state
    proposal["decided"] = decided
    if plan_dir is not None:
        proposal["plan_dir"] = str(plan_dir)
    triage.write_json(Path(dest) / "proposals.json", data)


def apply(ident, n, pid, approve, rows_fn, actor="cli", plan_dir=None):
    """Apply one proposal. `rows_fn()` returns the current `forge findings show --json` rows.

    Returns `{"proposal", "changed", "plan_dir"?, "next"?}`. Raises Refused or Failed.
    """
    found = triage.triage_dir(ident["key"], n)
    if found is None:
        raise Refused(f"no triage {n}")
    try:
        gate = triage._Lock(found[1] / LOCK_NAME)
        gate.__enter__()
    except OSError as exc:
        raise Failed(f"cannot lock {found[1]}: {exc.strerror or exc}") from exc
    try:
        dest, data, proposal = _load(ident, n, pid)
        _require_pending(n, pid, proposal)
        if not isinstance(approve, str) or approve != triage.proposal_hash(proposal):
            raise Refused(f"{pid}: the hash does not match the proposal as it is now; read it again")
        rows = rows_fn()
        _check_known(proposal, rows)
        kind = proposal.get("kind")
        out = {}
        if kind == "duplicate":
            changed = _effect_duplicate(ident, proposal)
        elif kind == "fixed":
            changed = _effect_fixed(ident, proposal)
        elif kind == "tag":
            changed = _effect_tag(ident, proposal)
        elif kind == "issue":
            changed = _effect_issue(ident, proposal)
        elif kind == "plan":
            changed, target = _effect_plan(ident, proposal, dest, rows, n, pid, plan_dir)
            out["plan_dir"] = str(target)
            out["next"] = f"{PARALLEL} plan {shlex.quote(str(target))} --repo {shlex.quote(_existing_checkout(ident))}"
        else:
            raise Refused(f"{pid}: unknown kind {kind!r}")
        try:
            _decide(dest, data, proposal, "applied", actor, plan_dir=out.get("plan_dir"))
        except OSError as exc:
            if out.get("plan_dir"):
                shutil.rmtree(out["plan_dir"], ignore_errors=True)
            raise Failed(f"cannot write {Path(dest) / 'proposals.json'}: {exc.strerror or exc}") from exc
        return {"proposal": proposal, "changed": [fp for fp, _before, _after in changed], **out}
    finally:
        gate.__exit__(None, None, None)


def reject(ident, n, pid, actor="cli", reason=None):
    """Mark one pending proposal rejected. Returns `{"proposal", "changed": []}`."""
    if reason is not None and len(reason) > MAX_REASON:
        raise Refused(f"--reason must be at most {MAX_REASON} characters")
    found = triage.triage_dir(ident["key"], n)
    if found is None:
        raise Refused(f"no triage {n}")
    try:
        gate = triage._Lock(found[1] / LOCK_NAME)
        gate.__enter__()
    except OSError as exc:
        raise Failed(f"cannot lock {found[1]}: {exc.strerror or exc}") from exc
    try:
        dest, data, proposal = _load(ident, n, pid)
        _require_pending(n, pid, proposal)
        try:
            _decide(dest, data, proposal, "rejected", actor, reason=reason)
        except OSError as exc:
            raise Failed(f"cannot write {Path(dest) / 'proposals.json'}: {exc.strerror or exc}") from exc
        return {"proposal": proposal, "changed": []}
    finally:
        gate.__exit__(None, None, None)
