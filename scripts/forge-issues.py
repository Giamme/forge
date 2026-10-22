#!/usr/bin/env python3
"""forge issues <run>: file the review findings a run left open as GitHub issues, with plain `gh`.

A run can end with findings nobody will act on: CONFIRMED findings `--qa-threshold` tolerated
(`tasks/<id>/known_issues.md`), PLAUSIBLE findings in a passed task's `qa.last` (with
`--include-plausible`), and whatever a whole-run review left open. This drafts one issue per
finding (or per task/run with `--group`) and creates them only with `--apply`.

<run> is a plan dir, a task dir, a review dir (`<plan>/final-review`), known_issues.md or qa.last.
Tasks that did not pass are skipped: their findings still block them.

Each issue carries a hidden `<!-- forge:finding fp=... -->` marker. The fingerprint is the one
gh-proman computes, and both tools skip a finding either of them already filed, in any state.
When the gh-proman skill is configured for the repository, prefer `gh-proman findings`: it also
adds the issues to the team's project board with a priority. This command is the fallback.

--json lists every finding in `findings` (fp, severity, origin, plan, task, text, `filed`, ...). --offline never
runs `gh` (no filed state, publication of commits judged from local refs only). --only FP[,FP] keeps those
fingerprints. --all-runs reads <run> as a repository (or a plan dir naming one) and collects every run in its
registry, the newest plan winning for a finding present in several. A caller filing an explicit selection
passes `--max-issues <len(selection)>`; the default is 10.

Exit codes: 0 ok, 2 usage or `gh` error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FINDING_RE = re.compile(r"^\s*[-*]\s*\[(P[0-3])\]\s*\[(CONFIRMED|PLAUSIBLE)\]\s*(\S.*?)\s+[—–]+\s+(.+?)\s*$", re.M)
# gh-proman writes `gh-proman:finding`, this command `forge:finding`; the fingerprints match.
FP_MARK_RE = re.compile(r"<!-- (?:gh-proman|forge):finding fp=([0-9a-f]{12})")
DONE = {"PASS", "MERGED"}
SEVERITIES = ["P0", "P1", "P2", "P3"]
FP_RE = re.compile(r"[0-9a-f]{12}")

TEXT = {
    "en": {
        "plausible": "To verify: ",
        "tolerated": "Forge review finding left open: it was below the blocking threshold {threshold}, so the "
                     "work was merged without fixing it.",
        "review": "Forge whole-run review finding left open: check whether it was already fixed before working on it.",
        "plausible_ctx": "PLAUSIBLE forge review finding (low confidence): verify it before fixing. If it turns out "
                         "to be wrong, close the issue saying why.",
        "group": "Forge review findings left open in run `{plan}` ({n}).",
        "group_title": "Non-blocking forge findings: {scope}",
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
        "review": "Segnalazione della review complessiva di forge rimasta aperta: verificare se è già stata "
                  "corretta prima di lavorarci.",
        "plausible_ctx": "Segnalazione PLAUSIBILE della review di forge (confidenza bassa): va verificata prima di "
                         "correggere. Se risulta infondata, chiudi l'issue spiegando perché.",
        "group": "Segnalazioni della review di forge rimaste aperte nel run `{plan}` ({n}).",
        "group_title": "Segnalazioni forge non bloccanti: {scope}",
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
        found = [{**f, **meta, "task": None if whole_run else task, "origin": "review" if f["confidence"] == "CONFIRMED" else "plausible", "threshold": "-"}
                 for f in parse_findings(read(task_dir / "qa.last"))
                 if f["confidence"] == "CONFIRMED" or include_plausible]
        return found, None
    if state not in DONE and status not in DONE:
        return [], f"{task}: status {state or 'unknown'}, its findings still block it"
    threshold = read(task_dir / "qa_threshold") or (read(plan_dir / "qa_threshold") if plan_dir else "") or "?"
    meta["threshold"] = threshold
    found = [{**f, **meta, "origin": "tolerated"} for f in parse_findings(read(task_dir / "known_issues.md"))
             if f["confidence"] == "CONFIRMED"]
    if include_plausible:
        found += [{**f, **meta, "origin": "plausible"} for f in parse_findings(read(task_dir / "qa.last"))
                  if f["confidence"] == "PLAUSIBLE"]
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from forge_registry import runs_registry
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


def collect(path: Path, include_plausible, all_runs=False):
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
            found = [{**f, **meta, "origin": "review" if f["confidence"] == "CONFIRMED" else "plausible"}
                     for f in parse_findings(read(path)) if f["confidence"] == "CONFIRMED" or include_plausible]
        else:
            found = [{**f, **meta, "origin": "tolerated" if f["confidence"] == "CONFIRMED" else "plausible"}
                     for f in parse_findings(read(path)) if f["confidence"] == "CONFIRMED" or include_plausible]
        return {"plan": plan_root.name, "repo_path": None}, found, []
    if (path / "tasks").is_dir():
        found, notes = collect_plan(path, include_plausible)
        return {"plan": path.name, "repo_path": read(path / "repo") or None}, found, notes
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
    return {"plan": (plan_dir or path).name, "repo_path": (read(plan_dir / "repo") if plan_dir else "") or None}, \
        found, [note] if note else []


# ---------------------------------------------------------------------------------------------- draft

def fingerprint(slug, f):
    """Identical to gh-proman's finding_fp: keep the two in step."""
    norm = re.sub(r"\s+", " ", f["text"].lower())[:160]
    return hashlib.sha1(f"{slug.lower()}|{f['file']}|{norm}".encode("utf-8")).hexdigest()[:12]


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


def body_for(slug, repo_path, items, lang, offline=False):
    t = TEXT[lang]
    h = t["headings"]
    first = items[0]
    plan = first["plan"]
    plausible = all(f["origin"] == "plausible" for f in items)
    if len(items) > 1:
        context = t["group"].format(plan=plan, n=len(items))
    elif plausible:
        context = t["plausible_ctx"]
    elif first["origin"] == "review":
        context = t["review"]
    else:
        context = t["tolerated"].format(threshold=first["threshold"])
    commit = f", commit `{first['commit'][:12]}`" if first.get("commit") else ""
    lines = [f"## {h[0]}", context, "",
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


# ---------------------------------------------------------------------------------------------- main

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
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.offline and args.apply:
        ap.error("--offline cannot be combined with --apply")
    args.only = [x.strip() for v in args.only for x in v.split(",")]
    if any(not FP_RE.fullmatch(x) for x in args.only):
        ap.error("--only takes 12-hex-character fingerprints, repeated or comma-separated")
    try:
        out = file_findings(args)
    except Fail as exc:
        print(f"forge issues: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_human(out, args.apply)
    return 0


def finding_entry(f, filed=None):
    return {"fp": f["fp"], "severity": f["severity"], "confidence": f["confidence"], "origin": f["origin"],
            "plan": f["plan"], "plan_dir": f["plan_dir"], "task": f["task"], "file": f["file"],
            "summary": (f["text"].splitlines() or [""])[0][:160], "text": f["text"],
            "reviewer": f["reviewer"], "commit": f.get("commit") or "",
            "filed": {"url": filed["url"], "state": filed["state"]} if filed else None}


def file_findings(args):
    offline = args.offline
    meta, found, notes = collect(Path(args.run), args.include_plausible, args.all_runs)
    repo_path = meta["repo_path"] or os.getcwd()
    slug = args.repo or github_slug(repo_path)
    if not slug:
        raise Fail(f"{repo_path} has no GitHub origin remote; pass --repo owner/repo")
    if args.min_severity:
        cut = SEVERITIES.index(args.min_severity)
        found = [f for f in found if SEVERITIES.index(f["severity"]) <= cut]
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
        entries.append(finding_entry(f, issue))
        if issue:
            already.append({"finding": f["raw"], "issue": issue["url"], "state": issue["state"]})
        else:
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
            prefix = t["plausible"] if items[0]["origin"] == "plausible" else ""
            title = f"[{items[0]['severity']}] {prefix}{t['group_title'].format(scope=scope)}"
        drafts.append({"title": title, "severity": items[0]["severity"], "origin": items[0]["origin"],
                       "findings": [f["raw"] for f in items], "fingerprints": [f["fp"] for f in items],
                       "same_title": None if offline else open_titles.get(norm_title(title)),
                       "body": body_for(slug, repo_path if meta["repo_path"] else None, items, args.lang, offline)})
    drafts.sort(key=lambda d: SEVERITIES.index(d["severity"]))
    out = {"repo": slug, "plan": meta["plan"], "notes": notes,
           "summary": {"findings": len(seen), "already_filed": len(already), "drafts": len(drafts),
                       **{o: sum(1 for f in todo if f["origin"] == o) for o in ("tolerated", "review", "plausible")}},
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
    print(f"{out['repo']} · run {out['plan']}: {s['findings']} finding(s), {s['already_filed']} already filed, "
          f"{s['drafts']} draft(s) ({s['tolerated']} tolerated, {s['review']} review, {s['plausible']} plausible)")
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
    for d in out["drafts"]:
        flag = f"  (open issue with the same title: {d['same_title']})" if d["same_title"] else ""
        print(f"  draft   {d['title']}{flag}")
    if out["drafts"]:
        print("Nothing created: add --apply to file these.")


if __name__ == "__main__":
    sys.exit(main())
