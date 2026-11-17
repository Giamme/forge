"""Approve or reject one proposal of a triage (#161).

`apply` is the only writer of what a triage proposed. An approval names the proposal's hash: the
hash is recomputed from `proposals.json`, so a proposal that changed after the person read it is
refused. The effect goes through the finding record (`store`), with the actor `triage`; the person
who asked is kept in the proposal's `decided`. A `plan` proposal becomes a plan dir that
`forge-planner.py parse` has accepted; nothing is dispatched and GitHub is never contacted.

The whole of one apply (checks, effect, bookkeeping) runs under a lock file in the triage dir, so two
approvals of one proposal cannot both apply. Any failure leaves the proposal `pending` and the finding
record as it was.
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


def _effect_duplicate(ident, proposal, locked=False):
    fp, of = proposal["fp"], proposal["of"]
    return _call(lambda: store.update(
        ident, [fp], ACTOR, locked=locked, status="duplicate", duplicate_of=of,
        reason=_cut(_text(proposal, "why"))))


def _effect_fixed(ident, proposal, locked=False):
    fields = {"reason": _cut(_text(proposal, "evidence"))}
    commit = proposal.get("commit")
    if isinstance(commit, str) and COMMIT_RE.fullmatch(commit):
        fields["commit"] = commit
    return _call(lambda: store.set_status(ident, [proposal["fp"]], "fixed", ACTOR, locked=locked, **fields))


def _effect_tag(ident, proposal, locked=False):
    tags = proposal.get("tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) for tag in tags):
        raise Refused(f"{proposal.get('id')}: tags must be a list of tags")
    # add_tags cannot be told the lock is already held
    return _call(lambda: store.update(ident, _fps(proposal), ACTOR, locked=locked, add_tags=list(tags)))


def _effect_issue(ident, proposal, locked=False):
    draft = {"title": _text(proposal, "title"), "body": _text(proposal, "body")}
    return _call(lambda: store.update(
        ident, _fps(proposal), ACTOR, locked=locked, status="to-file", draft=draft))


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
    """The plan dir. Removed here when the planner fails; the caller removes it when the apply does not stick."""
    target = Path(plan_dir) if plan_dir else default_plan_dir(ident, n, pid)
    if not target.is_absolute():
        target = Path(os.path.abspath(target))
    _create_dir(target)
    try:
        _make_plan(proposal, target, dest, rows)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _note_fresh(ident, proposal, note):
    """Set `note` on findings that still have none. The caller holds `store.lock` across this read and write."""
    fresh = [fp for fp in _fps(proposal) if not ((store.get(ident, fp) or {}).get("note"))]
    if not fresh:
        return []
    return store.update(ident, fresh, ACTOR, locked=True, note=note)


# --- bookkeeping ----------------------------------------------------------------------------------

def _record_files(ident):
    record = store.record_path(ident)
    return record, record.with_name(record.name.removesuffix(".json") + ".log.jsonl")


def _capture(ident):
    """`(record bytes or None, log size or None)`, to put back if proposals.json cannot be written."""
    record, log = _record_files(ident)
    try:
        data = record.read_bytes()
    except FileNotFoundError:
        data = None
    try:
        size = log.stat().st_size
    except FileNotFoundError:
        size = None
    return data, size


def _put_bytes(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restore(ident, captured):
    """Put the record and its log back to `_capture`. The caller holds `store.lock`."""
    data, size = captured
    record, log = _record_files(ident)
    if data is None:
        try:
            record.unlink()
        except FileNotFoundError:
            pass
    else:
        _put_bytes(record, data)
    if size is None:
        try:
            log.unlink()
        except FileNotFoundError:
            pass
        return
    fd = os.open(log, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def _effect(ident, proposal, kind, target):
    """The record change. The caller holds `store.lock`."""
    if kind == "duplicate":
        return _effect_duplicate(ident, proposal, locked=True)
    if kind == "fixed":
        return _effect_fixed(ident, proposal, locked=True)
    if kind == "tag":
        return _effect_tag(ident, proposal, locked=True)
    if kind == "issue":
        return _effect_issue(ident, proposal, locked=True)
    return _note_fresh(ident, proposal, f"in plan {target}")


def _commit(ident, dest, data, proposal, actor, kind, target, nxt):
    """Write the effect and mark the proposal applied, or leave both as they were.

    One store lock covers a plan's "no note yet" read and the note write, so a note set in
    between cannot be overwritten. The record is put back when proposals.json cannot be written.
    """
    def run():
        with store.lock(ident):
            captured = _capture(ident)
            changed = _effect(ident, proposal, kind, target)
            out = {}
            if target is not None:
                out["plan_dir"] = str(target)
                out["next"] = nxt
            try:
                _decide(dest, data, proposal, "applied", actor, plan_dir=out.get("plan_dir"))
            except OSError as exc:
                proposal["state"] = "pending"
                proposal.pop("decided", None)
                proposal.pop("plan_dir", None)
                _restore(ident, captured)
                path = Path(dest) / "proposals.json"
                raise Failed(f"cannot write {path}: {exc.strerror or exc}") from exc
            return changed, out
    return _call(run)


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
        if kind not in ("duplicate", "fixed", "tag", "issue", "plan"):
            raise Refused(f"{pid}: unknown kind {kind!r}")
        target = None
        try:
            nxt = None
            if kind == "plan":
                target = _effect_plan(ident, proposal, dest, rows, n, pid, plan_dir)
                nxt = f"{PARALLEL} plan {shlex.quote(str(target))} --repo {shlex.quote(_existing_checkout(ident))}"
            changed, out = _commit(ident, dest, data, proposal, actor, kind, target, nxt)
        except BaseException:
            if target is not None:
                shutil.rmtree(target, ignore_errors=True)
            raise
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
