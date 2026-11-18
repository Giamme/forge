"""Triage: a read-only agent reviews a repository's open findings and proposes what to do (#161).

`start` checks that a triage may run, allocates `<triage home>/<n>/`, and writes `findings.json`,
`prompt.txt` and `state.json` there. `run_job` dispatches the agent, parses and validates its
reply strictly, retries once with a reminder that quotes the error, and writes `proposals.json`,
then the final `state.json`. `detach` runs the job in a session of its own and writes its exit
code to `done` when it ends. Nothing here changes a finding, a repository file, a memory or an
issue: `apply` (a later step) is the only writer.

Files of one triage dir: findings.json, prompt.txt, state.json, proposals.json, triage.out,
done, and the dispatch's own side.last / side.resolved.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_flock as fcntl  # noqa: E402
import forge_proc  # noqa: E402
import forge_side  # noqa: E402

DISPATCH = SCRIPTS / "forge-dispatch.sh"
PLANNER = SCRIPTS / "forge-planner.py"

DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_FINDINGS = 400
MAX_FINDINGS_LIMIT = 2000
MAX_PROPOSALS = 200
MAX_TAGS = 8
MAX_TITLE = 200
MAX_BODY = 20000
MAX_WHY = 2000
MAX_PLAN_TASKS = 20
DOCTOR_TIMEOUT = 120

NO_BLOCK = "no-proposals-block"
SEVERITIES = ("P0", "P1", "P2", "P3")
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")
OPEN_RE = re.compile(r"^```forge-triage[ \t]*$")
CLOSE_RE = re.compile(r"^```[ \t]*$")
# Statuses that still need work: what a triage reads.
TRIAGE_STATUSES = ("open", "to-file")

KEYS = {
    "duplicate": ("kind", "fp", "of", "why"),
    "fixed": ("kind", "fp", "evidence", "commit"),
    "tag": ("kind", "fps", "tags"),
    "issue": ("kind", "fps", "title", "body"),
    "plan": ("kind", "fps", "title", "request", "plan"),
}
# Not part of what the agent wrote: set by this module (id, hash, state) or by apply (decided).
STORED_KEYS = ("id", "hash", "state", "decided")

REMINDER = (
    "Your last reply was not usable: %s.\n"
    "Reply again, ending with exactly one ```forge-triage block holding the JSON object "
    '{"proposals": [...]}, in exactly the format above.\n'
    'If there is nothing to propose, end with a block holding {"proposals": []}.\n')

PROMPT = """\
You are triaging the open review findings of one repository. You are read-only: never edit,
create or delete a file, and never run a command that changes the repository or anything else.
You may read the repository at {checkout} and run read-only commands there (`git log`,
`git show`, `git grep`, `git blame`, `ls`, `cat`). Nothing you do is applied automatically: a
person reads every proposal first, so propose few things and only what you are confident of.

The findings are in {findings} ({count} of them). Read that file first. It is JSON:
{{"findings": [...]}}, most severe and newest first. Each finding has an `fp` (its identifier;
use exactly that text wherever a proposal names a finding), a `severity`, a `file` and `line`,
a `summary` or `text`, its `status`, its `tags`, and its `record` (what was already done with it).

Look for:
- duplicates: two findings that describe the same defect;
- findings that are already fixed in the repository now (check the code, and `git log` for the commit);
- tags that would help to sort the findings, e.g. ui, backend, tests, docs, infra, enhancement, security;
- findings, or groups of related findings, worth filing as one issue;
- groups of findings that one planned change would fix together.

End your reply with exactly one fenced block whose info string is `forge-triage`, holding
one JSON object, and nothing after it:

```forge-triage
{{"proposals": [
  {{"kind": "duplicate", "fp": "<fp>", "of": "<fp of the finding it repeats>", "why": "..."}},
  {{"kind": "fixed", "fp": "<fp>", "evidence": "what shows it is fixed", "commit": "<sha or null>"}},
  {{"kind": "tag", "fps": ["<fp>"], "tags": ["ui"]}},
  {{"kind": "issue", "fps": ["<fp>"], "title": "...", "body": "..."}},
  {{"kind": "plan", "fps": ["<fp>"], "title": "...", "request": "...", "plan": {{"tasks": [...]}}}}
]}}
```

Rules for the block:
- At most {max_proposals} proposals. Few, and high-confidence. An empty list is a fine answer.
- Every `fp` and `of` is an `fp` from {findings}. A finding is never a duplicate of itself.
- `tags`: at most {max_tags} per proposal, each matching ^[a-z][a-z0-9-]{{0,23}}$.
- `title` at most {max_title} characters, `body` at most {max_body}, `why` and `evidence` at most
  {max_why}. None of them is empty.
- `commit` is the full or abbreviated hash (7 to 40 lowercase hex characters) of the commit that
  fixed the finding, or null when you do not know it.
- A `plan` proposal's `plan` holds exactly what forge's planner accepts inside a `forge-plan`
  block: {{"tasks": [...]}} with 1 to {max_tasks} tasks, each with `id` (lowercase letters, digits
  and dashes), `deps` (ids of earlier tasks), `difficulty` (low, medium or high), optionally
  `risk` (low, medium or high), `files`, `title`, `approach` and `prompt`. Every task `prompt` ends
  with a `## Acceptance criteria` section. `request` is the change in plain words.
  Example of a plan:

{example}
"""


class Refused(Exception):
    """The triage may not start (exit 3). Nothing was written, or nothing is left running."""


class Failed(Exception):
    """The triage could not be started (exit 1)."""


class Invalid(Exception):
    """A reply that breaks the contract. Carries the first reason."""


# --- small files --------------------------------------------------------------------------------

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_atomic(path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path, data) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def alive(pid) -> bool:
    return forge_proc.pid_alive(pid)


class _Lock:
    """An flock on one file of the triage dir, for the read-modify-write of state.json."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


# --- the hash ---------------------------------------------------------------------------------

def proposal_hash(proposal) -> str:
    """The first 12 hex of the sha256 of the proposal's canonical JSON, without id, hash, state and decided."""
    body = {key: value for key, value in proposal.items() if key not in STORED_KEYS}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --- the triage dirs --------------------------------------------------------------------------

def numbered(key) -> list:
    """`[(n, dir)]` of the triage dirs of one repository key, ascending."""
    home = forge_side.triage_home(key)
    found = []
    try:
        entries = list(home.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink():
            found.append((int(entry.name), entry))
    return sorted(found)


def triage_dir(key, n=None):
    """The dir of triage `n`, or of the newest one. None when there is none."""
    found = numbered(key)
    if n is None:
        return found[-1] if found else None
    for number, path in found:
        if number == n:
            return number, path
    return None


def read_state(dest) -> dict | None:
    """The state of one triage dir. `running` with no live process, or with `done` written, reads `failed: lost`."""
    dest = Path(dest)
    state = read_json(dest / "state.json")
    if state is None:
        return None
    if not isinstance(state, dict):
        return {"state": "failed", "reason": "lost"}
    if state.get("state") == "running" and ((dest / "done").exists() or not alive(state.get("pid"))):
        lost = dict(state)
        lost["state"] = "failed"
        lost["reason"] = "lost"
        return lost
    return state


# --- the findings and the prompt -------------------------------------------------------------------

def _rank(value) -> int:
    return SEVERITIES.index(value) if value in SEVERITIES else len(SEVERITIES)


def _age(row) -> float:
    plan_dir = row.get("plan_dir")
    if isinstance(plan_dir, str) and plan_dir:
        try:
            return os.stat(plan_dir).st_mtime
        except OSError:
            return 0.0
    return 0.0


def select_findings(rows, cap) -> list:
    """The rows a triage reads: open or to-file, highest severity first, newest first, at most `cap`."""
    keep = [row for row in rows
            if isinstance(row, dict) and row.get("status") in TRIAGE_STATUSES and isinstance(row.get("fp"), str)
            and row["fp"]]
    keep.sort(key=lambda row: (_rank(row.get("severity")), -_age(row)))
    return keep[:cap]


def build_prompt(findings_path, checkout, count) -> str:
    planner = _planner()
    return PROMPT.format(
        checkout=checkout, findings=findings_path, count=count, max_proposals=MAX_PROPOSALS,
        max_tags=MAX_TAGS, max_title=MAX_TITLE, max_body=MAX_BODY, max_why=MAX_WHY,
        max_tasks=MAX_PLAN_TASKS, example=planner.EXAMPLE)


# --- the reply -------------------------------------------------------------------------------

_planner_module = None


def _planner():
    """forge-planner.py, loaded by path: its file name is not importable."""
    global _planner_module
    if _planner_module is None:
        spec = importlib.util.spec_from_file_location("forge_planner", PLANNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _planner_module = module
    return _planner_module


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise Invalid(f"duplicate key {key!r} in the JSON")
    return dict(pairs)


def extract_block(reply) -> str:
    """The text of the one `forge-triage` block. Raises Invalid(NO_BLOCK) when there is none."""
    if not isinstance(reply, str):
        raise Invalid(NO_BLOCK)
    # Split on \\n only: U+2028 is legal inside a JSON string. A trailing \\r is ignored.
    lines = reply.split("\n")
    openings = [i for i, line in enumerate(lines) if OPEN_RE.match(line.rstrip("\r"))]
    if not openings:
        raise Invalid(NO_BLOCK)
    if len(openings) > 1:
        raise Invalid(f"{len(openings)} forge-triage blocks in the reply; exactly one is allowed")
    for j in range(openings[0] + 1, len(lines)):
        if CLOSE_RE.match(lines[j].rstrip("\r")):
            return "\n".join(lines[openings[0] + 1:j])
    raise Invalid("the forge-triage block is never closed")


def _text(value, where, name, limit):
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{where}: {name} must be a non-empty string")
    if len(value) > limit:
        raise Invalid(f"{where}: {name} is longer than {limit} characters")
    try:   # a lone surrogate (JSON "\\ud800") parses but cannot be stored as UTF-8
        value.encode("utf-8")
    except UnicodeError:
        raise Invalid(f"{where}: {name} has characters that cannot be stored")
    return value


def _fp(value, where, name, known):
    if not isinstance(value, str) or value not in known:
        shown = value if isinstance(value, str) and len(value) <= 80 else "(not a fingerprint)"
        raise Invalid(f"{where}: {name} {shown!r} is not an fp of findings.json")
    return value


def _fps(value, where, known):
    if not isinstance(value, list) or not value:
        raise Invalid(f"{where}: fps must be a non-empty list")
    found = []
    for item in value:
        fp = _fp(item, where, "fps entry", known)
        if fp not in found:
            found.append(fp)
    return found


def _tags(value, where):
    if not isinstance(value, list) or not value:
        raise Invalid(f"{where}: tags must be a non-empty list")
    found = []
    for item in value:
        if not isinstance(item, str) or TAG_RE.match(item) is None:
            raise Invalid(f"{where}: tag {item!r} must match ^[a-z][a-z0-9-]{{0,23}}$")
        if item not in found:
            found.append(item)
    if len(found) > MAX_TAGS:
        raise Invalid(f"{where}: at most {MAX_TAGS} tags")
    return found


def _check_plan(plan, where):
    """The plan must pass forge-planner's parse of a `forge-plan` block, with 1 to 20 tasks."""
    if not isinstance(plan, dict):
        raise Invalid(f"{where}: plan must be an object holding tasks")
    module = _planner()
    try:
        body = json.dumps(plan, ensure_ascii=False)
        tasks, _groups = module.parse_reply("```forge-plan\n" + body + "\n```")
    except module.Refusal as error:
        raise Invalid(f"{where}: plan refused by the planner: {error}")
    except (ValueError, RecursionError, UnicodeError) as error:
        raise Invalid(f"{where}: plan could not be read ({type(error).__name__})")
    if not 1 <= len(tasks) <= MAX_PLAN_TASKS:
        raise Invalid(f"{where}: plan must have 1 to {MAX_PLAN_TASKS} tasks")
    return plan


def _check_proposal(item, index, known):
    where = f"proposal {index}"
    if not isinstance(item, dict):
        raise Invalid(f"{where} is not an object")
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in KEYS:
        raise Invalid(f"{where}: kind must be one of {', '.join(KEYS)}")
    expected = KEYS[kind]
    missing = [key for key in expected if key not in item]
    extra = [key for key in item if key not in expected]
    if missing or extra:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if extra:
            parts.append("unexpected " + ", ".join(str(key) for key in extra))
        raise Invalid(f"{where} ({kind}) must have exactly the keys {', '.join(expected)} ({'; '.join(parts)})")
    out = {"kind": kind}
    if kind == "duplicate":
        out["fp"] = _fp(item["fp"], where, "fp", known)
        out["of"] = _fp(item["of"], where, "of", known)
        if out["fp"] == out["of"]:
            raise Invalid(f"{where}: a finding cannot be a duplicate of itself")
        out["why"] = _text(item["why"], where, "why", MAX_WHY)
    elif kind == "fixed":
        out["fp"] = _fp(item["fp"], where, "fp", known)
        out["evidence"] = _text(item["evidence"], where, "evidence", MAX_WHY)
        commit = item["commit"]
        out["commit"] = commit if isinstance(commit, str) and COMMIT_RE.match(commit) else None
    elif kind == "tag":
        out["fps"] = _fps(item["fps"], where, known)
        out["tags"] = _tags(item["tags"], where)
    elif kind == "issue":
        out["fps"] = _fps(item["fps"], where, known)
        out["title"] = _text(item["title"], where, "title", MAX_TITLE)
        out["body"] = _text(item["body"], where, "body", MAX_BODY)
    else:
        out["fps"] = _fps(item["fps"], where, known)
        out["title"] = _text(item["title"], where, "title", MAX_TITLE)
        out["request"] = _text(item["request"], where, "request", MAX_BODY)
        out["plan"] = _check_plan(item["plan"], where)
    return out


def parse_proposals(reply, fps):
    """`(proposals, None)` for a usable reply, else `(None, error)`; error is NO_BLOCK when there is no block.

    `fps` is every fp findings.json holds. Each proposal comes back with only the keys of its kind
    (a commit that is not 7 to 40 lowercase hex characters becomes None). Never raises.
    """
    known = set(fps)
    try:
        body = extract_block(reply)
        try:
            data = json.loads(body, object_pairs_hook=_no_duplicate_keys)
        except (ValueError, RecursionError) as error:
            raise Invalid(f"the forge-triage body is not valid JSON: {type(error).__name__}: {str(error)[:200]}")
        if not isinstance(data, dict) or list(data) != ["proposals"] or not isinstance(data["proposals"], list):
            raise Invalid('the forge-triage body must be exactly {"proposals": [...]}')
        items = data["proposals"]
        if len(items) > MAX_PROPOSALS:
            raise Invalid(f"at most {MAX_PROPOSALS} proposals ({len(items)})")
        return [_check_proposal(item, index, known) for index, item in enumerate(items, 1)], None
    except Invalid as error:
        return None, " ".join(str(error).split())
    except Exception as error:  # noqa: BLE001 -- a broken reply must not end the job
        return None, f"reply could not be read ({type(error).__name__})"


def reason_of(error) -> str:
    """The `reason` a failed state carries for a parse error."""
    return NO_BLOCK if error == NO_BLOCK else f"invalid: {error}"


# --- start ----------------------------------------------------------------------------------------

def preflight(spec) -> None:
    """`forge-dispatch.sh doctor --spec S --role side`. Raises Refused with its message."""
    argv = forge_proc.bash_cmd(DISPATCH, "doctor", "--spec", str(spec), "--role", "side")
    try:
        result = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                timeout=DOCTOR_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(f"doctor could not run: {error}")
    if result.returncode == 0:
        return
    lines = [line.strip() for line in (result.stderr + "\n" + result.stdout).splitlines() if line.strip()]
    raise Refused(lines[-1] if lines else f"doctor refused {spec} (exit {result.returncode})")


def start(ident, rows, spec, checkout, max_findings=DEFAULT_MAX_FINDINGS, actor="cli"):
    """Preflight, allocate and write the triage dir. Returns `(n, dest)`; the state is `running`.

    Raises Refused (doctor, `triage_allowed`, a triage already running, the cap, no checkout).
    """
    if not isinstance(max_findings, int) or isinstance(max_findings, bool) \
            or not 1 <= max_findings <= MAX_FINDINGS_LIMIT:
        raise Refused(f"--max-findings must be 1 to {MAX_FINDINGS_LIMIT}")
    if not isinstance(spec, str) or not spec.strip():
        raise Refused("a triage needs a model: --spec")
    if not isinstance(checkout, str) or not os.path.isdir(checkout):
        raise Refused("this repository has no checkout on this machine to read")
    preflight(spec)
    ok, reason = forge_side.triage_allowed(checkout, spec)
    if not ok:
        raise Refused(f"triage not allowed: {reason}")
    key = ident["key"]
    home = forge_side.triage_home(key)
    try:
        home.mkdir(parents=True, exist_ok=True)
        gate = _Lock(home / ".start.lock")
        gate.__enter__()
    except OSError as error:
        raise Failed(f"cannot use {home}: {error.strerror or error}")
    try:
        newest = triage_dir(key)
        if newest is not None:
            state = read_state(newest[1])
            if state is not None and state.get("state") == "running":
                raise Refused(f"a triage is running: {newest[0]}")
        try:
            dest = forge_side.allocate_triage(key)
        except forge_side.TriageCapped as error:
            raise Refused(str(error))
        except (OSError, ValueError) as error:
            raise Failed(f"cannot allocate a triage dir: {error}")
        n = int(dest.name)
        chosen = select_findings(rows, max_findings)
        findings_path = dest / "findings.json"
        write_json(findings_path, {"triage": n, "findings": chosen})
        write_atomic(dest / "prompt.txt", build_prompt(findings_path, checkout, len(chosen)))
        write_json(dest / "state.json", {
            "state": "running", "spec": spec, "started": now(), "pid": os.getpid(),
            "findings": len(chosen), "actor": actor})
    finally:
        gate.__exit__(None, None, None)
    return n, dest


def detach(dest, argv):
    """Run `argv` (the hidden job) in a session of its own. Its output goes to triage.out and its exit
    code, written atomically, to `done`. Records the job's pid in state.json while it is running."""
    dest = Path(dest)
    done = dest / "done"
    wrapper = '"${@:2}"; rc=$?; printf "%s\\n" "$rc" > "$1.tmp" && mv -f "$1.tmp" "$1"; exit "$rc"'
    with open(dest / "triage.out", "ab") as out:
        process = subprocess.Popen(forge_proc.bash_cmd("-c", wrapper, "forge-triage", str(done), *argv),
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                   close_fds=True, **forge_proc.popen_group_kwargs(detached=True))
    with _Lock(dest / "state.lock"):
        state = read_json(dest / "state.json")
        if isinstance(state, dict) and state.get("state") == "running":
            state["pid"] = process.pid
            write_json(dest / "state.json", state)
    return process.pid


def write_done(dest, code) -> None:
    write_atomic(Path(dest) / "done", f"{int(code)}\n")


# --- the job ------------------------------------------------------------------------------------

def _finish(dest, current, **fields):
    """The final state.json, under the lock that detach() takes for the pid."""
    with _Lock(Path(dest) / "state.lock"):
        final = dict(current)
        final.update(fields)
        final["finished"] = now()
        write_json(Path(dest) / "state.json", final)
    return final


def run_job(dest, checkout, n, timeout=DEFAULT_TIMEOUT):
    """The job: dispatch, validate, retry once, store. Returns the final state. Never raises.

    A triage dir whose state is not `running` is left alone.
    """
    dest = Path(dest)
    state = read_json(dest / "state.json")
    if not isinstance(state, dict) or state.get("state") != "running":
        return state if isinstance(state, dict) else {"state": "failed", "reason": "lost"}
    try:
        return _job(dest, state, checkout, n, timeout)
    except Exception as error:  # noqa: BLE001 -- the state must say it failed
        try:
            return _finish(dest, state, state="failed", reason=f"error: {type(error).__name__}")
        except Exception:  # noqa: BLE001
            return {"state": "failed", "reason": "error"}


def _job(dest, state, checkout, n, timeout):
    data = read_json(dest / "findings.json")
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        return _finish(dest, state, state="failed", reason="error: findings.json")
    fps = [row["fp"] for row in findings if isinstance(row, dict) and isinstance(row.get("fp"), str)]
    prompt = (dest / "prompt.txt").read_text(encoding="utf-8")
    spec = state.get("spec")
    reminder = ""
    proposals, error = None, NO_BLOCK
    for attempt in (1, 2):
        result = forge_side.run_triage(dest, checkout, prompt + reminder, spec, timeout=timeout)
        if not result.get("ok"):
            return _finish(dest, state, state="failed", reason=result.get("reason") or "error")
        last = result.get("last")
        if attempt == 1 and isinstance(last, str):
            try:
                write_atomic(dest / "reply.1.txt", last)
            except (OSError, UnicodeError):
                pass
        proposals, error = parse_proposals(last, fps)
        if proposals is not None:
            break
        reminder = "\n\n" + REMINDER % error
    if proposals is None:
        return _finish(dest, state, state="failed", reason=reason_of(error))
    stored = []
    for k, proposal in enumerate(proposals, 1):
        entry = dict(proposal)
        entry["id"] = f"p{k}"
        entry["hash"] = proposal_hash(proposal)
        entry["state"] = "pending"
        stored.append(entry)
    write_json(dest / "proposals.json", {"triage": n, "spec": spec, "at": now(), "proposals": stored})
    return _finish(dest, state, state="done", proposals=len(stored))
