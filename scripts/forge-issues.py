#!/usr/bin/env python3
"""forge issues <run>: file the review findings a run left open as GitHub issues, with plain `gh`.

A run can end with findings nobody will act on: CONFIRMED findings `--qa-threshold` tolerated
(`tasks/<id>/known_issues.md`), PLAUSIBLE findings in a passed task's `qa.last` (with
`--include-plausible`), and whatever a whole-run review left open. This drafts one issue per
finding (or per task/run with `--group`) and creates them only with `--apply`.

A whole-run review whose integration branch has moved is marked outdated: its findings stay
listed (a later merge may have fixed only some of them) and the issue names the tasks that
merged after it. CONFIRMED findings under `## Accepted by a human despite the reviewer's`
heading are origin `accepted` — a person accepted that verdict — not `tolerated`. A grouped
issue repeats every distinct acceptance in the group, and the outdated-review sentence of
every review in it, not only the first finding's.

<run> is a plan dir, a task dir, a review dir (`<plan>/final-review`), known_issues.md or qa.last.
Tasks that did not pass are skipped: their findings still block them.

Each issue carries a hidden `<!-- forge:finding fp=... -->` marker. The fingerprint is the one
gh-proman computes, and both tools skip a finding either of them already filed, in any state.
When the gh-proman skill is configured for the repository, prefer `gh-proman findings`: it also
adds the issues to the team's project board with a priority. This command is the fallback.

A finding a run's finding sweep (`run --sweep`) fixed stays listed, with `swept` = {task, finding} (null
otherwise), and is never drafted: the sweep task that merged and whose reviewer marked it FIXED is its record.
One the sweep did not fix is drafted as before.

--json lists every finding in `findings` (fp, severity, origin, plan, task, text, `filed`, `review`, `swept`, ...). --offline never
runs `gh` (no filed state, publication of commits judged from local refs only). --only FP[,FP] keeps those
fingerprints. --all-runs reads <run> as a repository (or a plan dir naming one) and collects every run in its
registry, the newest plan winning for a finding present in several. A caller filing an explicit selection
passes `--max-issues <len(selection)>`; the default is 10.

--dismiss FP[,FP] / --undismiss FP[,FP] (repeatable, with an optional --reason TEXT of at most 500 characters) mark
findings that need no work, or take the mark back, then exit; they need no network and accept a fingerprint that
matches no current finding. A dismissed finding stays in `findings` with `dismissed` = {at, reason} (null otherwise)
and is left out of the drafts unless --include-dismissed is given. The marks live outside the repository, in
`${XDG_STATE_HOME:-~/.local/state}/forge/findings/<repo_key>.json` (an empty or relative XDG_STATE_HOME counts as
unset); the store directory and file must not be symlinks, and the file must be a regular one. The repository they
belong to is the one <run> resolves to; --repo does not change it.

The repository is the run's own, whatever form <run> takes: a file or task dir resolves to its plan dir and that plan's
`repo` file, a solo run dir to its own `repo` file, and --all-runs to the repository it names. Listing, filing and
(un)dismissing all resolve it the same way, so a mark lands in the store a later listing of the run reads. Only a run
that records no repository falls back to the current directory, and the output says so (`warnings`).

Two processes must not file for one repository at once, or both see a fingerprint as unfiled and create it. --apply
holds `<repo_key>.lock` (next to the store, `flock`) from the `gh issue list` lookup to the last `gh issue create`,
and each --dismiss/--undismiss holds it around its read-modify-write. A busy lock is retried for 10 s. Listing
without --apply takes no lock.

Exit codes: 0 ok, 2 usage, `gh` or dismissal store error, 3 busy (another filing or dismissal holds the lock).
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_registry import repo_key, runs_registry  # noqa: E402

FINDING_RE = re.compile(r"^\s*[-*]\s*\[(P[0-3])\]\s*\[(CONFIRMED|PLAUSIBLE)\]\s*(\S.*?)\s+[—–]+\s+(.+?)\s*$", re.M)
# gh-proman writes `gh-proman:finding`, this command `forge:finding`; the fingerprints match.
FP_MARK_RE = re.compile(r"<!-- (?:gh-proman|forge):finding fp=([0-9a-f]{12})")
DONE = {"PASS", "MERGED"}
SEVERITIES = ["P0", "P1", "P2", "P3"]
FP_RE = re.compile(r"[0-9a-f]{12}")
LOCK_WAIT = 10.0  # seconds a busy repository lock is retried before `forge issues` gives up (exit 3)
LOCK_POLL = 0.1
REASON_MAX = 500
STORE_MAX = 4 << 20  # a dismissal store is a few hundred bytes per finding; anything bigger is not one

TEXT = {
    "en": {
        "plausible": "To verify: ",
        "tolerated": "Forge review finding left open: it was below the blocking threshold {threshold}, so the "
                     "work was merged without fixing it.",
        "accepted": "A person accepted the work over the reviewer's {status} verdict, so it was merged without fixing it.",
        "accepted_reason": "Reason: {reason}",
        "review": "Forge whole-run review finding left open: check whether it was already fixed before working on it.",
        "outdated": "The review is outdated: {n} {tasks} merged into the integration branch after it{ids}, "
                    "so check whether they fixed it first.",
        "plausible_ctx": "PLAUSIBLE forge review finding (low confidence): verify it before fixing. If it turns out "
                         "to be wrong, close the issue saying why.",
        "group": "Forge review findings left open in run `{plan}` ({n}).",
        "group_title": "Non-blocking forge findings: {scope}",
        "group_title_accepted": "Accepted forge findings: {scope}",
        "origin": "Origin: forge `{plan}`, task `{task}`, severity {severity}, reviewed by `{reviewer}`{commit}.",
        "headings": ["Context", "Finding", "Tasks", "Closing criteria", "References"],
        "tasks": ["Reproduce the described case", "Fix it", "Add a regression test that fails before the fix"],
        "tasks_plausible": ["Check whether the problem is real (reproduce or refute it)",
                            "If real: fix it and add a regression test"],
        "closing": ["The described case no longer reproduces"],
        "closing_plausible": ["The problem is fixed, or refuted with the reasoning in a comment"],
        "code": "Code",
        "unpublished": "(commit not pushed to GitHub yet)",
    },
    "it": {
        "plausible": "Da verificare: ",
        "tolerated": "Segnalazione della review di forge rimasta aperta: era sotto la soglia bloccante {threshold}, "
                     "quindi il lavoro è stato integrato senza correggerla.",
        "accepted": "Una persona ha accettato il lavoro nonostante il verdetto {status} del revisore, quindi è "
                    "stato integrato senza correggerlo.",
        "accepted_reason": "Motivo: {reason}",
        "review": "Segnalazione della review complessiva di forge rimasta aperta: verificare se è già stata "
                  "corretta prima di lavorarci.",
        "outdated": "La review è obsoleta: {n} {tasks} nel branch di integrazione dopo di essa{ids}, quindi "
                    "verifica prima se l'hanno già corretta.",
        "plausible_ctx": "Segnalazione PLAUSIBILE della review di forge (confidenza bassa): va verificata prima di "
                         "correggere. Se risulta infondata, chiudi l'issue spiegando perché.",
        "group": "Segnalazioni della review di forge rimaste aperte nel run `{plan}` ({n}).",
        "group_title": "Segnalazioni forge non bloccanti: {scope}",
        "group_title_accepted": "Segnalazioni forge accettate: {scope}",
        "origin": "Origine: forge `{plan}`, task `{task}`, gravità {severity}, review di `{reviewer}`{commit}.",
        "headings": ["Contesto", "Segnalazione", "Attività", "Criteri di chiusura", "Riferimenti"],
        "tasks": ["Riprodurre il caso descritto", "Correggere",
                  "Aggiungere un test di regressione che fallisce prima della correzione"],
        "tasks_plausible": ["Verificare se il problema è reale (riprodurlo o smentirlo)",
                            "Se reale: correggere e aggiungere un test di regressione"],
        "closing": ["Il caso descritto non si riproduce più"],
        "closing_plausible": ["Il problema è corretto, oppure smentito con motivazione in un commento"],
        "code": "Codice",
        "unpublished": "(commit non ancora pubblicato su GitHub)",
    },
}


class Fail(Exception):
    pass


class Busy(Fail):
    """The repository lock is held by another filing or dismissal (exit 3, not 2)."""


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def run(cmd, cwd=None, stdin=None, check=True):
    env = dict(os.environ, LC_ALL="C", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        res = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, env=env, timeout=120)
    except FileNotFoundError:
        raise Fail(f"{cmd[0]} is not installed")
    if check and res.returncode != 0:
        raise Fail(f"{' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[:400]}")
    return res


# ---------------------------------------------------------------------------------------------- collect

def clean_markdown(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"(\*\*|__|`)", "", text).strip()


def parse_findings(text):
    out = []
    for m in FINDING_RE.finditer(text or ""):
        loc = m.group(3).strip()
        link = re.fullmatch(r"\[([^\]]+)\]\([^)]*\)", loc)
        loc = (link.group(1) if link else loc).strip("` ")
        lm = re.match(r"(.+?):(\d+)(?:[-:]\d+)?$", loc)
        out.append({"severity": m.group(1), "confidence": m.group(2), "file": lm.group(1) if lm else loc,
                    "line": int(lm.group(2)) if lm else None, "text": clean_markdown(m.group(4)),
                    "raw": f"[{m.group(1)}][{m.group(2)}] {loc} — {m.group(4).strip()}"})
    return out


def reviewer_spec(task_dir: Path):
    kv = dict(line.split("=", 1) for line in read(task_dir / "qa.resolved").splitlines() if "=" in line)
    return ":".join(x for x in (kv.get("alias"), kv.get("effort"), kv.get("harness")) if x) or "?"


# `final-review-<epoch>-<pid>` or `integrate-<epoch>-<pid>`. `at` is that whole-second epoch.
_REVIEW_KIND = re.compile(r"(final-review|integrate)-([0-9]+)-([0-9]+)\Z")
# What `git rev-parse` writes for a commit: sha1 (40) or sha256 (64), lowercase hex.
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# accept appends the reviewer's reply under this heading; findings before it were threshold-tolerated.
_ACCEPTED_HEADING = re.compile(
    r"^## Accepted by a human despite the reviewer's(?:\s+(\S+))?\s+verdict\s*$", re.M)


def _epoch(text):
    if len(text) > 15:
        return None
    try:
        at = float(text)
    except (ValueError, OverflowError):
        return None
    return at if math.isfinite(at) else None


def review_clock(review_dir: Path):
    """(source, at) of a whole-run review directory.

    `final-review-<epoch>-<pid>` is source `review`. `integrate-<epoch>-<pid>/final-review` is
    source `integrate`, and `at` is the epoch in the integrate directory's name. A directory
    literally named `final-review` with no such parent is a review whose clock is unknown.
    """
    review_dir = Path(review_dir).resolve()
    if review_dir.name == "final-review":
        match = _REVIEW_KIND.fullmatch(review_dir.parent.name)
        if match and match.group(1) == "integrate":
            return "integrate", _epoch(match.group(2))
        return "review", None
    match = _REVIEW_KIND.fullmatch(review_dir.name)
    if match and match.group(1) == "final-review":
        return "review", _epoch(match.group(2))
    return "review", None


def review_relname(plan_dir, review_dir: Path):
    """The review directory's path relative to the plan, posix, after resolving links."""
    review_dir = Path(review_dir).resolve()
    if plan_dir:
        try:
            return review_dir.relative_to(Path(plan_dir).resolve()).as_posix()
        except ValueError:
            pass
    return review_dir.name


def _meta_fields(review_dir: Path):
    fields = {}
    for line in read(review_dir / "review.meta").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def _known_integration(meta):
    """The `integration=` commit, or None when it is missing or not a commit object name.

    A malformed value is not the commit the review judged, so freshness uses the next rule.
    """
    value = meta.get("integration") or None
    if not value:
        return None
    return value if _COMMIT.fullmatch(value) else None


def _task_id_ok(name: str) -> bool:
    """True for a task id the scheduler and the UI reader both accept.

    Empty, `.`, `..`, a slash, a NUL or U+FFFD are not ids. An id may start with `_`.
    """
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name and "\ufffd" not in name


def _listed_task_ids(plan_dir: Path):
    """Task ids from tasks.tsv, in file order.

    Only those rows are tasks: `tasks/` also holds forge's own directories (for example
    `tasks/_integration`), which are not tasks unless a row lists them. A row needs seven
    columns, the same minimum the UI reader requires. The first of a duplicate id wins.
    """
    found, seen = [], set()
    for line in read(Path(plan_dir) / "tasks.tsv").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        ident = parts[0]
        if not _task_id_ok(ident) or ident in seen:
            continue
        seen.add(ident)
        found.append(ident)
    return found


def _task_merges(plan_dir: Path):
    """(id, mtime) of every listed task's `merged` marker, oldest merge first, then id.

    The ids are `_listed_task_ids`: a listed `_fix` counts, and an unlisted `tasks/_integration`
    does not. This is the set whose merge times the UI uses for `merged_since`.
    """
    root = Path(plan_dir) / "tasks"
    pairs = []
    for ident in _listed_task_ids(plan_dir):
        marker = root / ident / "merged"
        try:
            if marker.is_file():
                pairs.append((ident, marker.stat().st_mtime))
        except OSError:
            continue
    pairs.sort(key=lambda item: (item[1], item[0]))
    return pairs


def _first_line(path: Path):
    text = read(path)
    return text.splitlines()[0].strip() if text else ""


def review_freshness(accepted, integration, tip, source, at, merges, verdict):
    """(outdated, merged_since) for one final review. Same rule as forge-ui's FinalReviewSummary.

    Null when the review has no verdict or the plan has no `accepted.integration`. Otherwise the
    first rule that applies: a known `integration` commit, else `tip` when `source` is `review`,
    else (an older integrate review) any task merged after `at`. A merge counts when its marker
    mtime is `>= at + 1`. `merged_since` is those ids, oldest first, and is empty unless the
    review is outdated.
    """
    later = []
    if at is not None:
        later = [item for item in merges if item[1] is not None and item[1] >= at + 1]
        later.sort(key=lambda item: (item[1], item[0]))
    ids = [task_id for task_id, _merged_at in later]
    if not verdict or not accepted:
        return None, []
    if integration:
        moved = integration != accepted
    elif source == "review":
        moved = tip != accepted
    else:
        moved = bool(ids)
    return (True, ids) if moved else (False, [])


def review_outdated(plan_dir, review_dir):
    """(outdated, merged_since) for the whole-run review directory `review_dir` in `plan_dir`."""
    if not plan_dir or not review_dir:
        return None, []
    review_dir = Path(review_dir)
    meta = _meta_fields(review_dir)
    source, at = review_clock(review_dir)
    verdict = (_first_line(review_dir / "verdict").split() or [""])[0]
    return review_freshness(_first_line(Path(plan_dir) / "accepted.integration"), _known_integration(meta),
                            meta.get("tip") or None, source, at, _task_merges(plan_dir), verdict)


def review_stamp(plan_dir, review_dir):
    """The `review` object a whole-run finding carries, or None when there is no review directory."""
    if not review_dir:
        return None
    outdated, merged_since = review_outdated(plan_dir, review_dir)
    return {"name": review_relname(plan_dir, review_dir), "outdated": outdated, "merged_since": merged_since}


def acceptance_of(task_dir):
    """(status, reason) recorded in `accepted.by`.

    Key=value (`status`, `reason`, …) is the current record. Free text, which older runs wrote,
    is a reason and no status. `reason` is cut to REASON_MAX characters.
    """
    if not task_dir:
        return "", ""
    raw = read(Path(task_dir) / "accepted.by")
    if not raw:
        return "", ""
    fields = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            return "", raw[:REASON_MAX]
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or re.search(r"\s", key):
            return "", raw[:REASON_MAX]
        fields[key] = value.strip()
    if not fields:
        return "", raw[:REASON_MAX]
    return fields.get("status", ""), fields.get("reason", "")[:REASON_MAX]


def known_findings(text, task_dir, meta):
    """CONFIRMED findings of a task's known_issues.md, split at the acceptance heading.

    Findings before `## Accepted by a human despite the reviewer's <status> verdict` stay
    `tolerated`. Findings after it are `accepted`. The heading's status is used only when
    `accepted.by` does not record one.
    """
    text = text or ""
    match = _ACCEPTED_HEADING.search(text)
    before, after = (text[:match.start()], text[match.end():]) if match else (text, "")
    found = [{**f, **meta, "origin": "tolerated", "review": None}
             for f in parse_findings(before) if f["confidence"] == "CONFIRMED"]
    if match:
        file_status, reason = acceptance_of(task_dir)
        status = file_status or match.group(1) or "UNKNOWN"
        found += [{**f, **meta, "origin": "accepted", "review": None,
                   "accepted_status": status, "accepted_reason": reason}
                  for f in parse_findings(after) if f["confidence"] == "CONFIRMED"]
    return found


def task_findings(task_dir: Path, plan_dir, include_plausible, reviewers, review=None):
    """`review` True: task_dir is a whole-run review dir (it may hold a known_issues.md when a threshold applied)."""
    status = (read(task_dir / "status").split() or [""])[0]
    results = {}
    if plan_dir and (plan_dir / "results.tsv").is_file():
        for line in read(plan_dir / "results.tsv").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                results[cols[0]] = cols[1]
    task = task_dir.name
    state = results.get(task, status)
    meta = {"task": task, "commit": read(task_dir / "merged") or read(task_dir / "reviewed.commit"),
            "reviewer": reviewers.get(task) or reviewer_spec(task_dir)}
    whole_run = review is True
    if review is None:
        review = not status and task not in results and not (task_dir / "known_issues.md").exists()
    if review:
        # A review with no task behind it (whole-run review): everything it found is still open.
        stamp = review_stamp(plan_dir, task_dir) if whole_run else None
        found = [{**f, **meta, "task": None if whole_run else task,
                  "origin": "review" if f["confidence"] == "CONFIRMED" else "plausible",
                  "threshold": "-", "review": stamp}
                 for f in parse_findings(read(task_dir / "qa.last"))
                 if f["confidence"] == "CONFIRMED" or include_plausible]
        return found, None
    if state not in DONE and status not in DONE:
        return [], f"{task}: status {state or 'unknown'}, its findings still block it"
    threshold = read(task_dir / "qa_threshold") or (read(plan_dir / "qa_threshold") if plan_dir else "") or "?"
    meta["threshold"] = threshold
    found = known_findings(read(task_dir / "known_issues.md"), task_dir, meta)
    if include_plausible:
        found += [{**f, **meta, "origin": "plausible", "review": None}
                  for f in parse_findings(read(task_dir / "qa.last")) if f["confidence"] == "PLAUSIBLE"]
    return found, None


def mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def whole_run_review(plan_dir: Path):
    """The plan's canonical latest whole-run review dir, or None.

    `review` publishes `<plan>/final-review` (a link to the newest `final-review-<epoch>-<pid>/`), and
    `integrate --final-review` writes `integrate-*/final-review/` without touching it unless it publishes.
    Any other final-review-* dir is superseded.
    """
    def usable(d):
        return d if (d / "qa.last").is_file() else None
    link = (plan_dir / "final-review").resolve() if (plan_dir / "final-review").is_dir() else None
    published = usable(link) if link else None
    # A published review without qa.last (infra stop, dispatch start failure) is still the latest attempt:
    # an integrate review must beat its own mtime, and nothing older may stand in for it.
    stamp = (mtime(published / "qa.last") if published else
             max(mtime(link / "qa.input"), mtime(link)) if link else None)
    newest = None
    for d in plan_dir.glob("integrate-*/final-review"):
        d = d.resolve()
        if usable(d) and (newest is None or mtime(d / "qa.last") > mtime(newest / "qa.last")):
            newest = d
    if newest and (stamp is None or (newest != link and mtime(newest / "qa.last") > stamp)):
        return newest
    return published


def collect_plan(plan_dir: Path, include_plausible):
    tasks = sorted(d for d in (plan_dir / "tasks").iterdir() if d.is_dir() and not d.name.startswith("_"))
    reviewers = {}
    if (plan_dir / "tasks.tsv").is_file():
        for line in read(plan_dir / "tasks.tsv").splitlines():
            cols = line.split("\t")
            if len(cols) >= 6 and not line.startswith("#"):
                reviewers[cols[0]] = cols[5]
    found, notes = [], []
    for task_dir in tasks:
        items, note = task_findings(task_dir, plan_dir, include_plausible, reviewers)
        found += items
        if note:
            notes.append(note)
    review = whole_run_review(plan_dir)
    if review:
        items, _ = task_findings(review, plan_dir, include_plausible, {}, review=True)
        found += [{**f, "task": None} for f in items]
    for f in found:
        f["plan"], f["plan_dir"] = plan_dir.name, str(plan_dir)
    return found, notes


def collect_all(path: Path, include_plausible):
    plan_file = path / "repo"
    repo = Path(read(plan_file)) if plan_file.is_file() else path
    registry = runs_registry(repo)
    lines = read(registry).splitlines() if registry.is_file() else []
    plans, notes = [], []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = Path(line)
        if not d.is_dir():
            notes.append(f"{line}: run directory is missing, skipped")
        elif not (d / "tasks").is_dir():
            notes.append(f"{line}: no tasks/ directory, skipped")
        elif d not in plans:
            plans.append(d)
    plans.sort(key=lambda d: mtime(d), reverse=True)  # newest first: the first occurrence of a fingerprint wins
    found = []
    for d in plans:
        items, plan_notes = collect_plan(d, include_plausible)
        found += items
        notes += [f"{d.name}: {n}" for n in plan_notes]
    return {"plan": f"{repo.name} (all runs)", "repo_path": str(repo)}, found, notes


def recorded_repo(run_dir: Path):
    """The repository a plan dir or solo run dir records in its `repo` file; None when it records none."""
    return read(run_dir / "repo") or None


def collect(path: Path, include_plausible, all_runs=False):
    """(meta, findings, notes); meta["repo_path"] is the run's recorded repository, None when it records none."""
    path = path.resolve()
    if all_runs:
        if not path.is_dir():
            raise Fail(f"{path}: --all-runs needs a repository or plan directory")
        return collect_all(path, include_plausible)
    if path.is_file():
        if path.name not in ("known_issues.md", "qa.last"):
            raise Fail(f"{path}: expected a forge plan or task directory, known_issues.md or qa.last")
        parent = path.parent
        is_task = parent.parent.name == "tasks"
        if is_task:
            plan_root = parent.parent.parent
        elif (parent / "tasks").is_dir():
            plan_root = parent
        elif parent.parent.name.startswith("integrate-") and (parent.parent.parent / "tasks").is_dir():
            plan_root = parent.parent.parent
        elif (parent.parent / "tasks").is_dir():
            plan_root = parent.parent
        else:
            plan_root = parent
        is_review = not is_task and not read(parent / "status")
        meta = {"task": None if is_review else parent.name, "threshold": "-" if is_review else read(parent / "qa_threshold") or "?",
                "commit": read(parent / "merged") or read(parent / "reviewed.commit"),
                "reviewer": reviewer_spec(parent)}
        meta["plan"], meta["plan_dir"] = (plan_root.name, str(plan_root))
        if is_review:
            stamp = review_stamp(plan_root, parent)
            found = [{**f, **meta, "origin": "review" if f["confidence"] == "CONFIRMED" else "plausible", "review": stamp}
                     for f in parse_findings(read(path)) if f["confidence"] == "CONFIRMED" or include_plausible]
        elif path.name == "known_issues.md":
            found = known_findings(read(path), parent, meta)
            if include_plausible:
                found += [{**f, **meta, "origin": "plausible", "review": None}
                          for f in parse_findings(read(path)) if f["confidence"] == "PLAUSIBLE"]
        else:
            found = [{**f, **meta, "origin": "tolerated" if f["confidence"] == "CONFIRMED" else "plausible", "review": None}
                     for f in parse_findings(read(path)) if f["confidence"] == "CONFIRMED" or include_plausible]
        # plan_root is the plan dir, or for a file outside any plan (a solo run's) the dir holding it
        return {"plan": plan_root.name, "repo_path": recorded_repo(plan_root)}, found, []
    if (path / "tasks").is_dir():
        found, notes = collect_plan(path, include_plausible)
        return {"plan": path.name, "repo_path": recorded_repo(path)}, found, notes
    if not ((path / "qa.last").is_file() or (path / "known_issues.md").is_file()):
        raise Fail(f"{path} is not a forge run: no tasks/, qa.last or known_issues.md")
    if path.parent.name == "tasks":
        plan_dir = path.parent.parent
    elif (path.parent / "tasks").is_dir():
        plan_dir = path.parent
    elif path.parent.name.startswith("integrate-") and (path.parent.parent / "tasks").is_dir():
        plan_dir = path.parent.parent
    else:
        plan_dir = None
    reviewers = {}
    if plan_dir and (plan_dir / "tasks.tsv").is_file():
        for line in read(plan_dir / "tasks.tsv").splitlines():
            cols = line.split("\t")
            if len(cols) >= 6 and not line.startswith("#"):
                reviewers[cols[0]] = cols[5]
    found, note = task_findings(path, plan_dir, include_plausible, reviewers,
                                review=True if path.parent.name != "tasks" and not read(path / "status") else None)
    for f in found:
        f["plan"], f["plan_dir"] = (plan_dir or path).name, str(plan_dir or path)
    # a task or review dir in a plan: the plan's repository; a solo run dir: its own
    return {"plan": (plan_dir or path).name, "repo_path": recorded_repo(plan_dir or path)}, \
        found, [note] if note else []


def run_repository(meta):
    """(repo_path, warnings): the run's recorded repository, else the current directory with a warning saying so.

    Listing, filing and (un)dismissing all go through here, so they agree on the dismissal store.
    """
    if meta["repo_path"]:
        return meta["repo_path"], []
    cwd = os.getcwd()
    return cwd, [f"run {meta['plan']} records no repository; using the current directory {cwd}"]


# ---------------------------------------------------------------------------------------------- draft

def fingerprint(slug, f):
    """Identical to gh-proman's finding_fp: keep the two in step."""
    norm = re.sub(r"\s+", " ", f["text"].lower())[:160]
    return hashlib.sha1(f"{slug.lower()}|{f['file']}|{norm}".encode("utf-8")).hexdigest()[:12]


def finding_key(f):
    """A finding's fingerprint without the repository, for what is recorded before any slug is known: the
    finding sweep (forge-sweep.py) keys its findings by it."""
    return fingerprint("", f)


def swept(plan_dir):
    """{finding_key: {task, finding}} for the findings a merged sweep task's reviewer marked FIXED."""
    fixed = {}
    tasks = Path(plan_dir) / "tasks"
    if not tasks.is_dir():
        return fixed
    for task_dir in sorted(tasks.iterdir()):
        if not (task_dir / "merged").exists() or not (task_dir / "sweep.outcome.json").is_file():
            continue
        try:
            items = json.loads(read(task_dir / "sweep.outcome.json"))["findings"]
        except (ValueError, KeyError, TypeError):
            continue
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("status") == "fixed" and isinstance(item.get("key"), str):
                fixed.setdefault(item["key"], {"task": task_dir.name, "finding": item.get("id")})
    return fixed


def mark_swept(found):
    """Set `swept` on each finding a sweep of its own plan fixed (None otherwise)."""
    plans = {}
    for f in found:
        plan_dir = f.get("plan_dir")
        if plan_dir not in plans:
            plans[plan_dir] = swept(plan_dir) if plan_dir else {}
        f["swept"] = plans[plan_dir].get(finding_key(f)) if f.get("task") else None


def github_slug(repo_path):
    res = run(["git", "-C", repo_path, "remote", "get-url", "origin"], check=False)
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", res.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def title_for(f, lang):
    first = re.split(r"(?<=[.!?])\s", f["text"], maxsplit=1)[0].rstrip(". ")
    if len(first) > 90:
        first = first[:87].rsplit(" ", 1)[0] + "…"
    prefix = TEXT[lang]["plausible"] if f["origin"] == "plausible" else ""
    return f"[{f['severity']}] {prefix}{os.path.basename(f['file'])}: {first}"


def escape(text):
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def published(slug, repo_path, commit, offline=False):
    """True when the commit is on GitHub; with `offline`, only remote-tracking refs can prove it."""
    if repo_path and run(["git", "-C", repo_path, "branch", "-r", "--contains", commit], check=False).stdout.strip():
        return True
    if offline:
        return False
    return run(["gh", "api", f"repos/{slug}/commits/{commit}", "--jq", ".sha"], check=False).returncode == 0


def merged_since_text(ids, lang):
    """Ids cut after 5, as `(a, b, c, d, e, +N more)`, or "" when there are none."""
    shown = [str(item) for item in ids[:5]]
    extra = len(ids) - len(shown)
    names = ", ".join(shown)
    if extra:
        more = f"+{extra} altri" if lang == "it" else f"+{extra} more"
        names = f"{names}, {more}" if names else more
    return f" ({names})" if names else ""


def outdated_line(lang, review):
    """The sentence an outdated whole-run review adds after the context line, or ""."""
    if not review or review.get("outdated") is not True:
        return ""
    ids = list(review.get("merged_since") or [])
    n = len(ids)
    if lang == "it":
        tasks = "task è stato integrato" if n == 1 else "task sono stati integrati"
    else:
        tasks = "task" if n == 1 else "tasks"
    return TEXT[lang]["outdated"].format(n=n, tasks=tasks, ids=merged_since_text(ids, lang))


def accepted_line(lang, finding):
    """The sentence for a finding a person accepted, including the recorded reason, or ""."""
    if not finding or finding.get("origin") != "accepted":
        return ""
    t = TEXT[lang]
    line = t["accepted"].format(status=finding.get("accepted_status") or "UNKNOWN")
    reason = finding.get("accepted_reason") or ""
    if reason:
        line += " " + t["accepted_reason"].format(reason=escape(reason))
    return line


def acceptance_lines(lang, items):
    """One sentence per distinct acceptance, in first-seen order.

    `--group run` puts every accepted finding of the plan in one issue. Tasks record their
    own verdict and reason (`accepted.by`); each distinct pair is its own sentence, so a
    later task's acceptance is not replaced by the first finding's.
    """
    lines, seen = [], set()
    for finding in items:
        line = accepted_line(lang, finding)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def outdated_lines(lang, items):
    """The outdated-review sentence for every distinct review in the group, in first-seen order.

    Severity order can put a task's PLAUSIBLE finding ahead of the outdated review's, and
    that first finding has no review. The warning still follows the context line, once per
    distinct sentence.
    """
    lines, seen = [], set()
    for finding in items:
        line = outdated_line(lang, finding.get("review") if finding else None)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def body_for(slug, repo_path, items, lang, offline=False):
    t = TEXT[lang]
    h = t["headings"]
    first = items[0]
    plan = first["plan"]
    plausible = all(f["origin"] == "plausible" for f in items)
    accepted = acceptance_lines(lang, items)
    if len(items) > 1:
        context = t["group"].format(plan=plan, n=len(items))
    elif plausible:
        context = t["plausible_ctx"]
    elif first["origin"] == "review":
        context = t["review"]
    elif accepted:
        context = accepted[0]
    else:
        context = t["tolerated"].format(threshold=first["threshold"])
    commit = f", commit `{first['commit'][:12]}`" if first.get("commit") else ""
    lines = [f"## {h[0]}", context]
    # A grouped issue's context line is the group intro, so every distinct acceptance follows it.
    if len(items) > 1:
        lines.extend(accepted)
    lines.extend(outdated_lines(lang, items))
    lines += ["",
              t["origin"].format(plan=plan, task=first["task"] or "final-review", severity=first["severity"],
                                 reviewer=first["reviewer"], commit=commit), "", f"## {h[1]}"]
    for f in items:
        lines += [f"> {escape(f['raw'])}", ""]
    lines.append(f"## {h[2]}")
    if len(items) == 1:
        lines += [f"- [ ] {x}" for x in (t["tasks_plausible"] if plausible else t["tasks"])]
    else:
        lines += [f"- [ ] [{f['severity']}] `{f['file']}{':' + str(f['line']) if f['line'] else ''}`: "
                  f"{escape(f['text'])[:160]}" for f in items]
    lines += ["", f"## {h[3]}"] + [f"- [ ] {x}" for x in (t["closing_plausible"] if plausible else t["closing"])]
    lines += ["", f"## {h[4]}"]
    for f in items:
        loc = f"{f['file']}{':' + str(f['line']) if f['line'] else ''}"
        if f.get("commit") and published(slug, repo_path, f["commit"], offline):
            anchor = f"#L{f['line']}" if f["line"] else ""
            lines.append(f"- {t['code']}: https://github.com/{slug}/blob/{f['commit']}/{f['file']}{anchor}")
        else:
            lines.append(f"- {t['code']}: `{loc}` {t['unpublished'] if f.get('commit') else ''}".rstrip())
    lines.append("")
    lines += [f"<!-- forge:finding fp={f['fp']} source=forge:{f['plan']}/{f['task'] or 'final-review'} -->" for f in items]
    return "\n".join(lines)


def existing_issues(slug):
    res = run(["gh", "issue", "list", "--repo", slug, "--state", "all", "--limit", "1000",
               "--json", "number,url,state,title,body"])
    return json.loads(res.stdout or "[]")


def norm_title(title):
    return re.sub(r"\W+", " ", re.sub(r"^\[P[0-3]\]\s*", "", title).lower()).strip()


# ---------------------------------------------------------------------------------------------- state

def findings_dir() -> Path:
    """`${XDG_STATE_HOME:-~/.local/state}/forge/findings`; an empty or relative XDG_STATE_HOME counts as unset.

    Forge keeps its own data out of the user's tree, and a relative state home would name a directory below
    the current one, usually the repository.
    """
    for candidate in (os.environ.get("XDG_STATE_HOME") or "", "~/.local/state"):
        try:
            base = Path(candidate).expanduser() if candidate else None
        except RuntimeError:  # no home directory to expand "~" against
            base = None
        if base is not None and base.is_absolute():
            return base / "forge" / "findings"
    raise Fail("cannot place the dismissal store: no home directory and no absolute XDG_STATE_HOME")


def store_dir(create):
    """The findings directory after an lstat check that it is a real directory, not a symlink; None when absent."""
    path = findings_dir()
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise Fail(f"{path}: cannot create the dismissal store directory: {exc.strerror or exc}")
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if create:
            raise Fail(f"{path}: the dismissal store directory vanished")
        return None
    except OSError as exc:
        raise Fail(f"{path}: cannot use the dismissal store directory: {exc.strerror}")
    if not stat.S_ISDIR(mode):
        raise Fail(f"{path}: the dismissal store directory must be a real directory, not a symlink")
    return path


def load_dismissed(key):
    """The repository's dismissals, `{fp: {"at": epoch, "reason": str}}`; empty when it has none yet."""
    base = store_dir(create=False)
    if base is None:
        return {}
    path = base / f"{key}.json"
    not_regular = f"{path}: the dismissal store must be a regular file, not a symlink or special file"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise Fail(not_regular)
        # NOFOLLOW again for a swap after the lstat; NONBLOCK so a FIFO cannot hold the open
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise Fail(not_regular)
            raw = fh.read(STORE_MAX + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Fail(f"{path}: cannot read the dismissal store: {exc.strerror}")
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= STORE_MAX else None
    except ValueError:
        data = None
    if not (isinstance(data, dict) and all(
            FP_RE.fullmatch(fp) and isinstance(v, dict) and isinstance(v.get("reason"), str)
            and isinstance(v.get("at"), (int, float)) and not isinstance(v["at"], bool) for fp, v in data.items())):
        raise Fail(f"{path}: not a dismissal store ({{fingerprint: {{at, reason}}}}); fix or remove it")
    return {fp: {"at": v["at"], "reason": v["reason"]} for fp, v in data.items()}


def save_dismissed(key, data):
    """Replace the store atomically: a temp file in the same directory, fsynced, then renamed over it."""
    base = store_dir(create=True)
    path = base / f"{key}.json"
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise Fail(f"{path}: cannot write the dismissal store: {exc.strerror}")


@contextlib.contextmanager
def repo_lock(key, label):
    """Hold the repository's `<key>.lock` (flock, next to the store); busy for LOCK_WAIT seconds raises Busy.

    The lock is its own file, opened in place and never replaced, because the store is: a lock on the store
    file would stay with the old inode after an atomic save.
    """
    path = store_dir(create=True) / f"{key}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise Fail(f"{path}: cannot open the repository lock ({exc.strerror}); it must not be a symlink")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Fail(f"{path}: the repository lock must be a regular file")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise Fail(f"{path}: cannot lock the repository: {exc.strerror}")
            if time.monotonic() >= deadline:
                raise Busy(f"busy: another filing or dismissal for {label} is in progress")
            time.sleep(LOCK_POLL)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def dismiss(args):
    """--dismiss / --undismiss: update the repository's store under its lock. No network: <run> is only read to
    find the repository it resolves to."""
    meta, _found, _notes = collect(Path(args.run), False, args.all_runs)
    repo_path, warnings = run_repository(meta)
    key = repo_key(repo_path)
    with repo_lock(key, args.repo or github_slug(repo_path) or repo_path):
        data = load_dismissed(key)
        before = json.dumps(data, sort_keys=True)
        now = int(time.time())
        for fp in args.dismiss:
            # dismissing again without a --reason keeps the earlier mark, with its reason and date
            if fp not in data or args.reason is not None:
                data[fp] = {"at": now, "reason": args.reason or ""}
        for fp in args.undismiss:
            data.pop(fp, None)
        if json.dumps(data, sort_keys=True) != before:
            save_dismissed(key, data)
    return {"ok": True, "dismissed": args.dismiss, "undismissed": args.undismiss, "warnings": warnings}


# ---------------------------------------------------------------------------------------------- main

def split_fps(values):
    """Repeatable, comma-separated fingerprints, deduplicated in order; None when one is not 12 hex characters."""
    fps = [x.strip() for v in values for x in v.split(",")]
    return None if any(not FP_RE.fullmatch(x) for x in fps) else list(dict.fromkeys(fps))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forge issues", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    ap.add_argument("run", help="plan dir, task dir, review dir, known_issues.md or qa.last "
                                "(with --all-runs: a repository, or a plan dir naming one)")
    ap.add_argument("--include-plausible", action="store_true", help="also file PLAUSIBLE findings, as 'To verify'")
    ap.add_argument("--min-severity", choices=SEVERITIES, help="file only findings at least this severe")
    ap.add_argument("--group", choices=["finding", "task", "run"], default="finding",
                    help="one issue per finding (default), per task, or one checklist issue per run")
    ap.add_argument("--repo", help="owner/repo (default: the run's repository, from its origin remote)")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en", help="language of the issue text (default en)")
    ap.add_argument("--label", action="append", default=[], help="label to add if the repo has it; repeatable")
    ap.add_argument("--apply", action="store_true", help="create the issues (default: show drafts only)")
    ap.add_argument("--confirm-new", action="store_true", help="create even when an issue with the same title exists")
    ap.add_argument("--max-issues", type=int, default=10,
                    help="create at most this many per call (default 10); filing a --only selection: pass its size")
    ap.add_argument("--offline", action="store_true", help="never run `gh`: no filed state, local refs only")
    ap.add_argument("--only", action="append", default=[], metavar="FP",
                    help="keep only this 12-hex fingerprint; repeatable or comma-separated")
    ap.add_argument("--all-runs", action="store_true", help="collect every run in the repository's run registry")
    ap.add_argument("--dismiss", action="append", default=[], metavar="FP",
                    help="mark this 12-hex fingerprint as needing no work, then exit; repeatable or comma-separated")
    ap.add_argument("--undismiss", action="append", default=[], metavar="FP",
                    help="take the mark back, then exit; repeatable or comma-separated")
    ap.add_argument("--reason", help=f"why --dismiss marks them (at most {REASON_MAX} characters)")
    ap.add_argument("--include-dismissed", action="store_true", help="draft dismissed findings too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.offline and args.apply:
        ap.error("--offline cannot be combined with --apply")
    only = split_fps(args.only)
    if only is None:
        ap.error("--only takes 12-hex-character fingerprints, repeated or comma-separated")
    args.only = only
    args.dismiss, args.undismiss = split_fps(args.dismiss), split_fps(args.undismiss)
    if args.dismiss is None or args.undismiss is None:
        ap.error("--dismiss and --undismiss take 12-hex-character fingerprints, repeated or comma-separated")
    dismissing = bool(args.dismiss or args.undismiss)
    if dismissing and args.apply:
        ap.error("--dismiss and --undismiss cannot be combined with --apply")
    if set(args.dismiss) & set(args.undismiss):
        ap.error("a fingerprint cannot be both --dismiss and --undismiss")
    if args.reason is not None:
        if not args.dismiss:
            ap.error("--reason goes with --dismiss")
        try:
            args.reason.encode("utf-8")
        except UnicodeEncodeError:
            ap.error("--reason must be valid UTF-8")
        if len(args.reason) > REASON_MAX:
            ap.error(f"--reason is at most {REASON_MAX} characters")
    try:
        out = dismiss(args) if dismissing else file_findings(args)
    except Fail as exc:
        print(f"forge issues: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, Busy) else 2
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif dismissing:
        for warning in out["warnings"]:
            print(f"warning: {warning}")
        for verb in ("dismissed", "undismissed"):
            if out[verb]:
                print(f"{verb} {len(out[verb])} finding(s): {', '.join(out[verb])}")
    else:
        print_human(out, args.apply)
    return 0


def finding_entry(f, filed=None, dismissed=None):
    return {"fp": f["fp"], "severity": f["severity"], "confidence": f["confidence"], "origin": f["origin"],
            "plan": f["plan"], "plan_dir": f["plan_dir"], "task": f["task"], "file": f["file"],
            "summary": (f["text"].splitlines() or [""])[0][:160], "text": f["text"],
            "reviewer": f["reviewer"], "commit": f.get("commit") or "",
            "review": f.get("review"), "swept": f.get("swept"),
            "filed": {"url": filed["url"], "state": filed["state"]} if filed else None,
            "dismissed": dismissed}


def file_findings(args):
    meta, found, notes = collect(Path(args.run), args.include_plausible, args.all_runs)
    mark_swept(found)
    repo_path, warnings = run_repository(meta)
    slug = args.repo or github_slug(repo_path)
    if not slug:
        raise Fail("; ".join(warnings + [f"{repo_path} has no GitHub origin remote; pass --repo owner/repo"]))
    if args.min_severity:
        cut = SEVERITIES.index(args.min_severity)
        found = [f for f in found if SEVERITIES.index(f["severity"]) <= cut]
    # Filing is list-then-create, so no other filing or dismissal for this repository may run in between: the
    # lock covers the lookup, the drafts (which ask `gh` whether commits are published) and every create.
    with repo_lock(repo_key(repo_path), slug) if args.apply else contextlib.nullcontext():
        return list_and_file(args, meta, found, notes, repo_path, slug, warnings)


def list_and_file(args, meta, found, notes, repo_path, slug, warnings):
    offline = args.offline
    dismissed = load_dismissed(repo_key(repo_path))
    issues = [] if offline else existing_issues(slug)
    filed = {fp: issue for issue in issues for fp in FP_MARK_RE.findall(issue.get("body") or "")}
    only = set(args.only)
    seen, todo, already, entries = set(), [], [], []
    known = set()
    for f in found:
        f["fp"] = fingerprint(slug, f)
        known.add(f["fp"])
        if f["fp"] in seen or (only and f["fp"] not in only):
            continue
        seen.add(f["fp"])
        issue = filed.get(f["fp"])
        mark = dismissed.get(f["fp"])
        entries.append(finding_entry(f, issue, mark))
        if issue:
            already.append({"finding": f["raw"], "issue": issue["url"], "state": issue["state"]})
        elif f.get("swept"):
            continue                       # fixed in the run by its finding sweep
        elif not mark or args.include_dismissed:
            todo.append(f)
    notes = notes + [f"--only {fp}: no such open finding" for fp in args.only if fp not in known]
    entries.sort(key=lambda e: SEVERITIES.index(e["severity"]))
    keyed = {}
    for f in todo:
        key = id(f) if args.group == "finding" else (f["plan"], f["origin"], f["task"] if args.group == "task" else "")
        keyed.setdefault(key, []).append(f)
    open_titles = {norm_title(i["title"]): i["url"] for i in issues if i.get("state") == "OPEN"}
    t = TEXT[args.lang]
    drafts = []
    for items in keyed.values():
        items.sort(key=lambda f: SEVERITIES.index(f["severity"]))
        if len(items) == 1:
            title = title_for(items[0], args.lang)
        else:
            scope = (items[0]["task"] or items[0]["plan"]) if args.group == "task" else items[0]["plan"]
            origin = items[0]["origin"]
            if origin == "accepted":
                title = f"[{items[0]['severity']}] {t['group_title_accepted'].format(scope=scope)}"
            else:
                prefix = t["plausible"] if origin == "plausible" else ""
                title = f"[{items[0]['severity']}] {prefix}{t['group_title'].format(scope=scope)}"
        drafts.append({"title": title, "severity": items[0]["severity"], "origin": items[0]["origin"],
                       "findings": [f["raw"] for f in items], "fingerprints": [f["fp"] for f in items],
                       "same_title": None if offline else open_titles.get(norm_title(title)),
                       "body": body_for(slug, repo_path if meta["repo_path"] else None, items, args.lang, offline)})
    drafts.sort(key=lambda d: SEVERITIES.index(d["severity"]))
    out = {"repo": slug, "plan": meta["plan"], "notes": notes, "warnings": warnings,
           "summary": {"findings": len(seen), "already_filed": len(already),
                       "dismissed": sum(1 for e in entries if e["dismissed"]),
                       "swept": sum(1 for e in entries if e["swept"]), "drafts": len(drafts),
                       **{o: sum(1 for f in todo if f["origin"] == o)
                          for o in ("tolerated", "accepted", "review", "plausible")}},
           "findings": entries, "already_filed": already, "drafts": drafts}
    if args.apply:
        out["created"] = create(slug, drafts, args)
        for d in drafts:
            d.pop("body")
    return out


def create(slug, drafts, args):
    labels = []
    if args.label:
        res = run(["gh", "label", "list", "--repo", slug, "--limit", "500", "--json", "name"], check=False)
        have = {x["name"] for x in json.loads(res.stdout or "[]")} if res.returncode == 0 else set()
        labels = [x for x in args.label if x in have]
    created = []
    for i, d in enumerate(drafts):
        if i >= args.max_issues:
            created.append({"title": d["title"], "skipped": f"over --max-issues {args.max_issues}; run again"})
            continue
        if d["same_title"] and not args.confirm_new:
            created.append({"title": d["title"], "skipped": "an open issue has the same title", "issue": d["same_title"]})
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(d["body"])
        try:
            cmd = ["gh", "issue", "create", "--repo", slug, "--title", d["title"], "--body-file", fh.name]
            for label in labels:
                cmd += ["--label", label]
            res = run(cmd)
        finally:
            os.unlink(fh.name)
        url = next((w for w in res.stdout.split() if w.startswith("https://")), res.stdout.strip())
        created.append({"title": d["title"], "url": url})
    missing = sorted(set(args.label) - set(labels))
    if missing:
        created.append({"note": f"labels not in {slug}, left off: {', '.join(missing)}"})
    return created


def print_human(out, applied):
    s = out["summary"]
    dismissed = f", {s['dismissed']} dismissed" if s["dismissed"] else ""
    dismissed += f", {s['swept']} fixed by the run's finding sweep" if s.get("swept") else ""
    print(f"{out['repo']} · run {out['plan']}: {s['findings']} finding(s), {s['already_filed']} already filed{dismissed}, "
          f"{s['drafts']} draft(s) ({s['tolerated']} tolerated, {s['accepted']} accepted, "
          f"{s['review']} review, {s['plausible']} plausible)")
    for warning in out["warnings"]:
        print(f"  warning: {warning}")
    for note in out["notes"]:
        print(f"  skipped {note}")
    for a in out["already_filed"]:
        print(f"  filed   {a['issue']} ({a['state'].lower()}): {a['finding'][:100]}")
    if applied:
        for c in out["created"]:
            if "note" in c:
                print(f"  note    {c['note']}")
            elif "url" in c:
                print(f"  created {c['url']}  {c['title']}")
            else:
                print(f"  skipped {c['title']}: {c['skipped']}{' ' + c['issue'] if c.get('issue') else ''}")
        return
    outdated = {e["fp"] for e in out["findings"] if (e.get("review") or {}).get("outdated") is True}
    for d in out["drafts"]:
        flag = f"  (open issue with the same title: {d['same_title']})" if d["same_title"] else ""
        mark = " (outdated review)" if any(fp in outdated for fp in d["fingerprints"]) else ""
        print(f"  draft   {d['title']}{mark}{flag}")
    if out["drafts"]:
        print("Nothing created: add --apply to file these.")


if __name__ == "__main__":
    sys.exit(main())
