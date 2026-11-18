"""The finding record store: one JSON file per repository, keyed by repository identity.

`${XDG_STATE_HOME:-~/.local/state}/forge/findings/<identity key>.record.json` holds, per finding
fingerprint, the status the user or a forge command gave it, its tags, note, edit and issue draft. The
old per-checkout dismissals (`<checkout key>.json`, written by `forge issues`) are merged in on read as
`dismissed` and never touched. Every change is appended to `<key>.record.log.jsonl` and recorded as one
`finding_record` dataset point event (fail-open).

The store follows `forge issues`' rules without importing it: a real directory and regular files (no
symlinks), writes by temp file + fsync + rename, an empty or relative XDG_STATE_HOME counts as unset,
and `<key>.lock` (flock, O_NOFOLLOW, 10 s wait) is the same lock `forge issues --apply` takes.

Errors: `Invalid` (the input is refused, nothing was written), `Busy` (the lock was not free in time),
`Corrupt` (the store cannot be used: unreadable record, symlink, no home directory, no write access).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[1])
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import forge_identity  # noqa: E402
import forge_registry  # noqa: E402

STATUSES = ("open", "to-file", "filed", "fixed", "dismissed", "duplicate")
ACTORS = ("cli", "ui", "triage", "sweep", "issues")
SEVERITIES = ("P0", "P1", "P2", "P3")

FP_RE = re.compile(r"[0-9a-f]{12}")
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,23}")
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

MAX_TAGS = 8
MAX_NOTE = 2000
MAX_EDIT_TEXT = 4000
MAX_TITLE = 200
MAX_BODY = 20000
MAX_REASON = 500
MAX_ISSUE = 500
LOCK_WAIT = 10.0  # seconds a busy lock is retried before Busy
LOCK_POLL = 0.1
FILE_MAX = 16 << 20  # a record or an old dismissal store is a few hundred bytes per finding

CHANGE_KEYS = ("status", "reason", "commit", "issue", "duplicate_of", "note", "edit", "draft",
               "add_tags", "remove_tags")
_STATUS_FIELDS = ("reason", "commit", "issue", "duplicate_of")
_LOG_OPS = (("status", "status"), ("reason", "status"), ("commit", "status"), ("issue", "status"),
            ("duplicate_of", "status"), ("tags", "tags"), ("note", "note"), ("edit", "edit"),
            ("draft", "draft"))


class Busy(Exception):
    """Another process holds the repository lock past the wait."""


class Invalid(ValueError):
    """The input is refused. Nothing was written."""


class Corrupt(Exception):
    """The store cannot be used: an unreadable or malformed record, a symlink, or no usable location."""


# ------------------------------------------------------------------------------------------ location

def findings_dir() -> Path:
    """`${XDG_STATE_HOME:-~/.local/state}/forge/findings`; an empty or relative XDG_STATE_HOME counts as unset."""
    for candidate in (os.environ.get("XDG_STATE_HOME") or "", "~/.local/state"):
        try:
            base = Path(candidate).expanduser() if candidate else None
        except RuntimeError:  # no home directory to expand "~" against
            base = None
        if base is not None and base.is_absolute():
            return base / "forge" / "findings"
    raise Corrupt("cannot place the finding record: no home directory and no absolute XDG_STATE_HOME")


def _store_dir(create: bool):
    """The findings directory after an lstat check that it is a real directory; None when absent."""
    path = findings_dir()
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise Corrupt(f"{path}: cannot create the finding record directory: {exc.strerror or exc}")
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if create:
            raise Corrupt(f"{path}: the finding record directory vanished")
        return None
    except OSError as exc:
        raise Corrupt(f"{path}: cannot use the finding record directory: {exc.strerror}")
    if not stat.S_ISDIR(mode):
        raise Corrupt(f"{path}: the finding record directory must be a real directory, not a symlink")
    return path


def _key(ident) -> str:
    key = ident.get("key") if isinstance(ident, dict) else None
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise Invalid("identity has no usable key")
    return key


def record_path(ident) -> Path:
    """Where the repository's record file is (it may not exist yet)."""
    return findings_dir() / f"{_key(ident)}.record.json"


# --------------------------------------------------------------------------------------------- lock

@contextlib.contextmanager
def lock(ident):
    """Hold `<key>.lock` (flock) for the repository; busy for LOCK_WAIT seconds raises `Busy`.

    The lock is its own file, opened in place and never replaced, because the record is replaced on every save.
    """
    key = _key(ident)
    path = _store_dir(create=True) / f"{key}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise Corrupt(f"{path}: cannot open the repository lock ({exc.strerror}); it must not be a symlink")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Corrupt(f"{path}: the repository lock must be a regular file")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise Corrupt(f"{path}: cannot lock the repository: {exc.strerror}")
            if time.monotonic() >= deadline:
                raise Busy("busy: another change to this repository's findings is in progress")
            time.sleep(LOCK_POLL)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


# --------------------------------------------------------------------------------------- read, write

def _read_json(path: Path, noun: str):
    """The parsed JSON of a regular file, None when it does not exist. Raises Corrupt otherwise."""
    not_regular = f"{path}: the {noun} must be a regular file, not a symlink or special file"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise Corrupt(not_regular)
        # NOFOLLOW again for a swap after the lstat; NONBLOCK so a FIFO cannot hold the open
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise Corrupt(not_regular)
            raw = fh.read(FILE_MAX + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Corrupt(f"{path}: cannot read the {noun}: {exc.strerror}")
    try:
        if len(raw) > FILE_MAX:
            raise ValueError("too large")
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Corrupt(f"{path}: not a {noun} (invalid JSON); fix or remove it")


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shape_problem(entry):
    """Why a stored entry is not well formed (types, vocabularies and limits), None when it is."""
    if not isinstance(entry, dict):
        return "not an object"
    # no status = nothing recorded about its state (only tags, a note, an edit or a draft)
    if "status" in entry and entry["status"] not in STATUSES:
        return "unknown status"
    if not _is_num(entry.get("at")):
        return "no time"
    if "actor" in entry and entry["actor"] not in ACTORS:
        return "unknown actor"
    for field, limit in (("reason", MAX_REASON), ("note", MAX_NOTE), ("issue", MAX_ISSUE)):
        if field in entry and not (isinstance(entry[field], str) and len(entry[field]) <= limit):
            return f"bad {field}"
    if "commit" in entry and not (isinstance(entry["commit"], str) and COMMIT_RE.fullmatch(entry["commit"])):
        return "bad commit"
    if "duplicate_of" in entry and not (isinstance(entry["duplicate_of"], str)
                                        and FP_RE.fullmatch(entry["duplicate_of"])):
        return "bad duplicate_of"
    if "tags" in entry:
        tags = entry["tags"]
        if not (isinstance(tags, list) and len(tags) <= MAX_TAGS
                and all(isinstance(t, str) and TAG_RE.fullmatch(t) for t in tags)):
            return "bad tags"
    if "edit" in entry:
        edit = entry["edit"]
        if not (isinstance(edit, dict) and set(edit) <= {"severity", "text"}
                and edit.get("severity", "P0") in SEVERITIES
                and isinstance(edit.get("text", ""), str) and len(edit.get("text", "")) <= MAX_EDIT_TEXT):
            return "bad edit"
    if "draft" in entry:
        draft = entry["draft"]
        if not (isinstance(draft, dict) and set(draft) <= {"title", "body"}
                and isinstance(draft.get("title", ""), str) and len(draft.get("title", "")) <= MAX_TITLE
                and isinstance(draft.get("body", ""), str) and len(draft.get("body", "")) <= MAX_BODY):
            return "bad draft"
    return None


def _read_record(ident) -> dict:
    """The record file's own entries, `{}` when none yet. Corrupt when it is not a record."""
    base = _store_dir(create=False)
    if base is None:
        return {}
    path = base / f"{_key(ident)}.record.json"
    data = _read_json(path, "finding record")
    if data is None:
        return {}
    findings = data.get("findings") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("version") == 1 and isinstance(findings, dict)):
        raise Corrupt(f"{path}: not a finding record ({{version: 1, findings: {{fingerprint: entry}}}}); fix or remove it")
    for fp, entry in findings.items():
        problem = _shape_problem(entry) if FP_RE.fullmatch(fp) else "not a fingerprint"
        if problem:
            raise Corrupt(f"{path}: finding {fp}: {problem}; fix or remove the file")
    return findings


def _old_dismissals(ident) -> dict:
    """The dismissals `forge issues` kept per checkout, as record entries. Read only; unreadable files are skipped."""
    base = _store_dir(create=False)
    if base is None:
        return {}
    keys = []
    candidates = []
    with contextlib.suppress(Exception):
        candidates.extend(forge_identity.checkouts(ident))
    for checkout in candidates:
        with contextlib.suppress(Exception):
            keys.append(forge_registry.repo_key(checkout))
    keys.append(_key(ident))
    found: dict = {}
    for key in dict.fromkeys(keys):
        if not _KEY_RE.fullmatch(key):
            continue
        try:
            data = _read_json(base / f"{key}.json", "dismissal store")
        except Corrupt:
            continue
        if not isinstance(data, dict):
            continue
        for fp, value in data.items():
            if not (isinstance(fp, str) and FP_RE.fullmatch(fp) and isinstance(value, dict)
                    and isinstance(value.get("reason"), str) and _is_num(value.get("at"))):
                continue
            entry = {"status": "dismissed", "at": value["at"], "actor": "issues", "reason": value["reason"][:MAX_REASON]}
            if fp not in found or entry["at"] > found[fp]["at"]:
                found[fp] = entry
    return found


def _view(ident, own: dict) -> dict:
    """The record's entries with the old dismissals merged in for fingerprints the record does not have."""
    merged = {fp: dict(entry) for fp, entry in _old_dismissals(ident).items() if fp not in own}
    merged.update(own)
    return merged


def _write_record(ident, own: dict) -> None:
    """Replace the record atomically: a temp file in the same directory, fsynced, then renamed over it."""
    base = _store_dir(create=True)
    name = f"{_key(ident)}.record.json"
    path = base / name
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise Corrupt(f"{path}: the finding record must be a regular file, not a symlink or special file")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise Corrupt(f"{path}: cannot write the finding record: {exc.strerror}")
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{name.removesuffix('.json')}.", suffix=".tmp", dir=base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "findings": own}, fh, indent=2, sort_keys=True, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise Corrupt(f"{path}: cannot write the finding record: {exc.strerror}")


def _append_log(ident, lines: list) -> None:
    """Append the change lines to `<key>.record.log.jsonl`. The record is the truth; a log failure is not."""
    try:
        base = _store_dir(create=True)
        path = base / f"{_key(ident)}.record.log.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            payload = "".join(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n" for line in lines)
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, Corrupt):
        pass


def _emit(ident, lines: list) -> None:
    """One `finding_record` dataset point event per change. Fail-open, like every capture."""
    try:
        from forge_dataset.capture import point
        for line in lines:
            after = line["after"] or {}
            point("finding_record", None, None,
                  {"fp": line["fp"], "op": line["op"], "status": after.get("status"), "actor": line["actor"]},
                  repo=ident.get("root"))
    except Exception:
        pass


# ------------------------------------------------------------------------------------------------ API

def load(ident) -> dict:
    """`{fp: entry}`: the record plus the old dismissals for fingerprints it does not have."""
    _key(ident)
    return _view(ident, _read_record(ident))


def get(ident, fp):
    """One finding's entry, None when the store has nothing for it."""
    return load(ident).get(fp)


def _text(value, name: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value):
        raise Invalid(f"{name} must be {'a non-empty ' if not allow_empty else ''}string of at most {limit} characters")
    return value


def _check_changes(actor, changes: dict) -> None:
    if actor not in ACTORS:
        raise Invalid(f"actor must be one of {', '.join(ACTORS)}")
    unknown = sorted(set(changes) - set(CHANGE_KEYS))
    if unknown:
        raise Invalid(f"unknown change: {', '.join(unknown)}")
    if "status" in changes and changes["status"] not in STATUSES:
        raise Invalid(f"status must be one of {', '.join(STATUSES)}")
    if changes.get("reason") is not None:
        _text(changes["reason"], "reason", MAX_REASON)
    if changes.get("issue") is not None:
        _text(changes["issue"], "issue", MAX_ISSUE, allow_empty=False)
    if changes.get("commit") is not None and not (isinstance(changes["commit"], str)
                                                  and COMMIT_RE.fullmatch(changes["commit"])):
        raise Invalid("commit must be 7 to 40 lowercase hex characters")
    if changes.get("duplicate_of") is not None and not (isinstance(changes["duplicate_of"], str)
                                                        and FP_RE.fullmatch(changes["duplicate_of"])):
        raise Invalid("duplicate_of must be a 12-character fingerprint")
    if changes.get("note") is not None:
        _text(changes["note"], "note", MAX_NOTE)
    for name in ("add_tags", "remove_tags"):
        tags = changes.get(name)
        if tags is None:
            continue
        if not isinstance(tags, (list, tuple, set, frozenset)) or not all(isinstance(t, str) for t in tags):
            raise Invalid(f"{name} must be a list of tags")
        if name == "add_tags":
            for tag in tags:
                if not TAG_RE.fullmatch(tag):
                    raise Invalid(f"tag {tag!r}: lowercase letters, digits and dashes, starting with a letter, at most 24")
    edit = changes.get("edit")
    if edit:
        if not isinstance(edit, dict) or not set(edit) <= {"severity", "text"}:
            raise Invalid("edit must be {severity, text}")
        if edit.get("severity") not in (None, "", *SEVERITIES):
            raise Invalid("severity must be one of P0, P1, P2, P3")
        if edit.get("text") is not None:
            _text(edit["text"], "edit text", MAX_EDIT_TEXT)
    draft = changes.get("draft")
    if draft:
        if not isinstance(draft, dict) or not set(draft) <= {"title", "body"}:
            raise Invalid("draft must be {title, body}")
        _text(draft.get("title", ""), "draft title", MAX_TITLE)
        _text(draft.get("body", ""), "draft body", MAX_BODY)


def _apply(fp: str, before, actor: str, now: float, changes: dict) -> dict:
    """The entry `before` becomes under `changes`. Raises Invalid; touches nothing."""
    # A finding with no entry that only gets tags, a note, an edit or a draft records no status: a stored
    # `open` means "reopened" to status.effective, which would hide what forge issues knows (swept, filed).
    entry = dict(before) if before else {}
    if "status" in changes:
        # a new status starts its own fields: the old issue or commit does not follow a reopen or a dismissal
        for field in _STATUS_FIELDS:
            entry.pop(field, None)
        entry["status"] = changes["status"]
    for field in _STATUS_FIELDS:
        value = changes.get(field)
        if value is not None:
            entry[field] = value
    if "note" in changes:
        if changes["note"]:
            entry["note"] = changes["note"]
        else:
            entry.pop("note", None)
    for name in ("edit", "draft"):
        if name in changes:
            value = changes[name]
            if not value:
                entry.pop(name, None)
            elif name == "edit":
                edit = {k: v for k, v in value.items() if v}
                if edit:
                    entry["edit"] = edit
                else:
                    entry.pop("edit", None)
            else:
                entry["draft"] = {"title": value.get("title", ""), "body": value.get("body", "")}
    if "add_tags" in changes or "remove_tags" in changes:
        tags = list(entry.get("tags", []))
        for tag in changes.get("add_tags") or ():
            if tag not in tags:
                tags.append(tag)
        gone = set(changes.get("remove_tags") or ())
        tags = [tag for tag in tags if tag not in gone]
        if len(tags) > MAX_TAGS:
            raise Invalid(f"{fp}: at most {MAX_TAGS} tags")
        if tags:
            entry["tags"] = tags
        else:
            entry.pop("tags", None)
    if any(key in changes for key in ("status", *_STATUS_FIELDS)):
        _require(fp, entry)
    return entry


def _require(fp: str, entry: dict) -> None:
    status = entry.get("status")
    if status is None:
        raise Invalid(f"{fp}: a reason, commit, issue or duplicate needs a status")
    if status == "filed" and not entry.get("issue"):
        raise Invalid(f"{fp}: filed needs the issue URL")
    if status == "fixed" and not (entry.get("commit") or entry.get("reason")):
        raise Invalid(f"{fp}: fixed needs a commit and/or a reason")
    if status == "dismissed" and not entry.get("reason"):
        raise Invalid(f"{fp}: dismissed needs a reason")
    if status == "duplicate":
        if not entry.get("duplicate_of"):
            raise Invalid(f"{fp}: duplicate needs duplicate_of")
        if entry["duplicate_of"] == fp:
            raise Invalid(f"{fp}: a finding cannot be a duplicate of itself")


def _op(before, after, changes) -> str:
    """What a change touched, for the log: status, tags, note, edit, draft, joined by "+"."""
    before = before or {}
    names = []
    for field, op in _LOG_OPS:
        old, new = before.get(field), after.get(field)
        # a tag or a note on a finding the store has not seen creates it open: that is no status change
        if op == "status" and "status" not in changes and field == "status":
            continue
        if old != new and op not in names:
            names.append(op)
    return "+".join(names) or "update"


def _comparable(entry):
    return None if entry is None else {k: v for k, v in entry.items() if k not in ("at", "actor")}


@contextlib.contextmanager
def _held(ident, locked: bool):
    if locked:
        yield
    else:
        with lock(ident):
            yield


def update(ident, fps, actor, locked=False, **changes):
    """Change every finding in `fps` the same way, in one lock and one write; a log line per changed finding.

    `changes`: status, reason, commit, issue, duplicate_of, note ("" clears), edit ({severity, text}; None or
    {} clears), draft ({title, body}; None clears), add_tags, remove_tags. Giving `status` starts the entry's
    reason, commit, issue and duplicate_of afresh. An entry the change leaves as it was is not written or logged.
    `locked=True` means the caller holds `lock(ident)`. Returns `[(fp, before, after)]` for the changed findings.
    Raises Invalid before anything is written when any finding or change is refused.
    """
    _key(ident)
    if isinstance(fps, str):
        raise Invalid("fps must be a list of fingerprints")
    fps = list(dict.fromkeys(fps))
    for fp in fps:
        if not (isinstance(fp, str) and FP_RE.fullmatch(fp)):
            raise Invalid(f"{fp!r} is not a 12-character fingerprint")
    _check_changes(actor, changes)
    with _held(ident, locked):
        own = _read_record(ident)
        view = _view(ident, own)
        now = time.time()
        results = []
        pending = {}
        for fp in fps:
            before = view.get(fp)
            after = _apply(fp, before, actor, now, changes)
            if before is not None and _comparable(before) == _comparable(after):
                continue
            if before is None and "status" not in changes and not after:
                # a change that adds nothing (clearing an absent note or tag) creates no entry at all
                continue
            after["at"] = now
            after["actor"] = actor
            pending[fp] = after
            results.append((fp, before, after))
        if not results:
            return []
        own.update(pending)
        _write_record(ident, own)
        lines = [{"at": now, "actor": actor, "fp": fp, "op": _op(before, after, changes), "before": before, "after": after}
                 for fp, before, after in results]
        _append_log(ident, lines)
        _emit(ident, lines)
        return results


def set_status(ident, fps, status, actor, **fields):
    """`update` with a status and its fields (reason, commit, issue, duplicate_of)."""
    return update(ident, fps, actor, status=status, **fields)


def add_tags(ident, fps, tags, actor):
    return update(ident, fps, actor, add_tags=list(tags))


def remove_tags(ident, fps, tags, actor):
    return update(ident, fps, actor, remove_tags=list(tags))


def set_note(ident, fp, text, actor):
    """Set the finding's note; empty text clears it."""
    return update(ident, [fp], actor, note=text)


def set_edit(ident, fp, actor, severity=None, text=None):
    """Change the finding's edited severity and/or text; an empty string clears that field, None leaves it."""
    if severity is None and text is None:
        return []
    if not (isinstance(fp, str) and FP_RE.fullmatch(fp)):
        raise Invalid(f"{fp!r} is not a 12-character fingerprint")
    _check_changes(actor, {"edit": {"severity": severity, "text": text}})
    with lock(ident):
        current = dict((get(ident, fp) or {}).get("edit") or {})
        if severity is not None:
            current["severity"] = severity
        if text is not None:
            current["text"] = text
        return update(ident, [fp], actor, locked=True, edit=current)


def mark_duplicate(ident, fp, of, actor):
    """Mark `fp` a duplicate of the finding `of` (never itself)."""
    return update(ident, [fp], actor, status="duplicate", duplicate_of=of)
