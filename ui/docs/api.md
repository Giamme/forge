# forge-ui data contract

This file is the contract between the three parts of forge-ui. It is fixed for tasks and
dwarves: implement it as written, and do not edit it as part of a task. Its owner revised it
deliberately for 0.3.0 (executions, findings, final reviews, the lazy endpoints and the 3D view).

```
forge_ui/reader.py      reads forge run dirs from disk, returns plain dicts (no I/O beyond reads)
forge_ui/executions.py  the attempts/<role>-<uuid>/ dirs: parsing, nesting, attempt numbers, a parse cache
forge_ui/findings.py    QA findings and gate lines, parsed by scripts/forge-contract.py
forge_ui/server.py      serves those dicts as JSON plus the static page, 127.0.0.1 only
forge_ui/static/        the page: index.html, app.js, style.css, graph.js and the scripts they load
```

Python 3.9+ standard library only. No third-party packages, no build step. The server makes
no network requests; the page loads three.js 0.160.0 from cdn.jsdelivr.net only when the 3D
view is used, and falls back to the 2D graph when it can't. The UI is read-only: nothing in
forge-ui writes to a run dir, a repo or forge's config.

## What a forge plan dir looks like

A decomposed forge run lives in a *plan dir*. Everything is optional except `tasks.tsv`;
any file may be missing, empty, truncated or malformed (runs are read while they are being
written), and the reader must never raise because of one.

| Path | Format |
|---|---|
| `tasks.tsv` | tab-separated, 7 columns: `id deps difficulty files dwarf qa title`. Lines starting with `#` and blank lines are skipped. `deps` and `files` are comma lists or `-`. `dwarf`/`qa` are model specs (`alias[:effort[:harness]]`), `-` (not yet resolved) or `UNASSIGNED` |
| `waves.tsv` | `wave<TAB>id` per line, wave is an integer from 1 |
| `results.tsv` | `id<TAB>status<TAB>branch` per line, e.g. `reader	MERGED	forge/20261016-1530/reader` |
| `run_id` | one line, e.g. `20261016-153012-ab12` |
| `repo` | one line, absolute path of the target repo |
| `goal.txt`, `request.md` | free text |
| `decompose_level` | `low`, `medium` or `high` |
| `explore`, `shadow_rate` | one number each (e.g. `0.5`) |
| `shadow_qa` | a comma list of specs |
| `timeout` | seconds |
| `verification.status` | `PASS`, `FAIL` or another word; `verification.command` the command; `verification.log` its output |
| `verification.runs.tsv` | `n<TAB>kind<TAB>exit<TAB>tree_changed<TAB>duration_s<TAB>logfile` per run of the command; `kind` is `initial`, `rerun` or `jev_retry`, `exit` may be empty, `tree_changed` is `0` or `1` |
| `verification.commit` | one line, the commit verified |
| `max_parallel`, `escalate_after`, `verify_retries` | one integer each |
| `qa_threshold` | `P0`..`P3`, the least severe finding that still blocks; `final_qa` the final review's QA spec |
| `deferrals.txt` | one line per task the wave planner deferred |
| `integration.base`, `accepted.integration` | one commit each: where the integration branch started, the revision the run accepted |
| `attempts/<role>-<uuid>/` | one *execution* (below) of the run itself: `parallel` (a `run`) or `verification` |
| `final-review-<epoch>-<pid>/` | one whole-run review: `qa.input`, `qa.last`, `verdict` (`PASS FAIL UNKNOWN INVALIDATED ACCEPTED`; absent while it runs or when nothing was judged), `qa.infra`, `qa.gate`, `known_issues.md`, `review.meta` (`key=value`: `base tip source spec threshold`, `threshold=none` when unset), `tasks.summary`, `attempts/qa-<uuid>/` |
| `final-review` | a symlink to the newest *published* review dir; `final-review.verdict` a copy of its verdict |
| `integrate-<epoch>-<pid>/` | one `integrate`: `setup.log`, `merge.log`, `verification.*`, `attempts/verification-<uuid>/`, and `final-review/` (a review dir as above) when it ran one |
| `tasks/<id>/merged` | exists when the task was merged; its mtime is when |
| `tasks/<id>/status` | one word: `RUNNING PASS FAIL ERROR INFRA TIMEOUT UNKNOWN INVALIDATED BLOCKED INTERRUPTED NOCHANGES CONFLICT` |
| `tasks/<id>/attempt` | the current attempt number |
| `tasks/<id>/attempts.tsv` | `epoch<TAB>attempt<TAB>status<TAB>infra_class` per line, one per attempt entry; status may also be `MERGED`, `infra_class` may be empty, `-` or missing |
| `tasks/<id>/escalations.tsv` | `epoch<TAB>attempt<TAB>from_spec<TAB>to_spec` per line |
| `tasks/<id>/qa.last` | the reviewer's final message; its last line matching `FORGE_VERDICT: PASS` or `FORGE_VERDICT: FAIL` is the verdict |
| `tasks/<id>/prompt.md`, `approach.md`, `retry_findings.md` | free text |
| `tasks/<id>/accepted.by` | who accepted a task over a reviewer's FAIL, and why (`key=value` lines in current forge; returned as text) |
| `tasks/<id>/qa.gate` | `threshold<TAB>decision<TAB>worst<TAB>n_confirmed<TAB>n_unlabelled<TAB>n_below` (`decision` is `accept` or `keep`, `-` for none) |
| `tasks/<id>/known_issues.md` | the confirmed findings a threshold tolerated |
| `tasks/<id>/dwarf.input`, `qa.input` | the prompts forge assembled; `<role>.delivered`, `<role>.resolved` the live dispatch files |
| `tasks/<id>/attempts/<role>-<uuid>/` | one execution of the task: `task`, `dwarf` or `qa` |
| `tasks/<id>/route.jsonl` | one JSON object per line, one per routing decision: `at` (epoch), `task`, `tier`, `epsilon`, `candidates` (list of specs), `default` (spec), `chosen` (spec), `explored` (bool), `propensity` (probability the chosen spec had), `inputs` (object), `decision` (a short hash identifying the decision). Keep unknown fields |
| `tasks/<id>/route.explored` | exists when the task's route was an exploratory pick |
| `tasks/<id>/clef.first_pass` | JSON: `p` (predicted probability the first attempt passes), `model`, `at`, `plan_table`, `assignment` (`{dwarf, qa}`) |
| `tasks/<id>/clef.retry_pass.<n>` | JSON, same shape plus `attempt` = n |
| `shadow/<id>/<attempt>-<k>/shadow.json` | JSON: `task`, `attempt`, `reviewed`, `primary`, `spec`, `pick`, `started`. A second, never-read review of attempt `<attempt>` |
| `shadow/<id>/<attempt>-<k>/shadow.status` | one word |
| `shadow/<id>/<attempt>-<k>/qa.last` | the shadow reviewer's final message, verdict as above |
| `shadow/<id>/<attempt>-<k>/attempts/qa-<uuid>/` | the shadow review's execution |

An **execution** is one dir `attempts/<role>-<uuid>/` that forge-runtime.py writes, `<role>`
one of `task dwarf qa solo verification parallel` and `<uuid>` 32 lowercase hex digits:

| Path | Format |
|---|---|
| `metrics.json` | `role`, `started` (monotonic), `wall_started` (epoch), `phase`, `phase_started` (monotonic), `elapsed_s` (seconds per phase, `total` once done), `exit_code` (`null` while in flight), `harness`, `prompt_bytes`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `infra_class`, `shadow` |
| `dataset.json` | may be absent: `parent` (the execution that started this one, e.g. `task-<uuid>`), `trigger`, `attempt_start` and `resume` (task executions) |
| `<role>.resolved`, `.prompt`, `.last`, `.timedout` | copies made when it finished: the model resolution (`key=value`), the prompt, the final message, the timeout marker |

The reader never opens an execution's `.log` or `.cmd`.

**Task status** is `MERGED` if `tasks/<id>/merged` exists, otherwise the first word of
`tasks/<id>/status`, otherwise `PENDING`.

Only the ids listed in `tasks.tsv` are tasks: `tasks/` may also hold forge's own dirs (e.g.
`tasks/_integration/`), and a task dir holds many other files (`attempts/` is a directory of
receipts, not `attempts.tsv`), all of which the reader ignores.

## What a solo run dir looks like

A solo run has no `tasks.tsv`. It is a directory whose `prompt.md` and `repo` are both regular
files. Anything else may be missing, empty, truncated or malformed, and the reader must never
raise because of one. Older solo dirs have `verdict` and not `status`.

| Path | Format |
|---|---|
| `prompt.md` | the implementation prompt |
| `repo` | one line, absolute path of the target repo |
| `goal.txt`, `request.md` | free text; the summary goal is the first line of `goal.txt` |
| `status` | one word: `RUNNING`, then `PASS`, `FAIL`, `UNKNOWN`, `NOCHANGES`, `INVALIDATED`, `TIMEOUT`, `INFRA`, `INTERRUPTED` or `ERROR` |
| `verdict` | one word, the legacy status (`PASS`, `FAIL`, `UNKNOWN`, `NOCHANGES`, `INVALIDATED`, …) |
| `dwarf.spec`, `qa.spec` | one line, a model spec (`alias[:effort[:harness]]`) |
| `dwarf.resolved`, `qa.resolved` | `key=value` lines; `alias=` and `effort=` name the model that ran |
| `attempts.tsv` | `epoch<TAB>n<TAB>status<TAB>infra class<TAB>dwarf spec`. The fifth column is recorded for forge and is not a separate attempt field |
| `attempts/<role>-<uuid>/` | the run's executions: `solo`, `dwarf`, `qa` |
| `dwarf.input`, `qa.input`, `qa.gate`, `known_issues.md` | as for a plan task |
| `dwarf.last`, `qa.last` | the role's final message. `qa.last` carries `FORGE_VERDICT:` the same way a plan task does |
| `dwarf.log`, `qa.log` | raw logs. The summary stats them and does not read them, so `updated` moves while a model writes |

**Solo status** is the first word of `status`, otherwise the first word of `verdict`, otherwise
`UNKNOWN`.

**Which dirs are runs:** `tasks.tsv` means a plan, even when `prompt.md` and `repo` are also
present. Otherwise `prompt.md` and `repo`, both regular files, means a solo run. Anything else
is not a run.

## Where runs come from

1. `--plan DIR` (repeatable): that dir, if it is a plan or a solo run.
2. `--root DIR` (repeatable): every plan dir at depth 0, 1 or 2 under DIR (DIR itself,
   its children, its grandchildren). Solo runs are not picked up, so a directory that merely
   contains `prompt.md` is ignored. Hidden dirs (`.name`) are not descended into.
3. forge's run registry, unless `--no-registry`: every `*.txt` file in
   `$XDG_CONFIG_HOME/forge/runs/` (default `~/.config/forge/runs/`). Each file is a
   `# ...` header line then one absolute path per line. Paths that no longer exist or are
   not plan or solo runs are skipped silently.

Duplicates (same `os.path.realpath`) are listed once. The first source that found a dir
wins for `source`.

## Python API (`forge_ui/reader.py`)

```python
def run_key(path) -> str
    """Stable opaque id of a run dir: sha1 of its realpath, first 12 hex chars."""

def discover(plans=(), roots=(), registry=True, registry_dir=None) -> list[tuple[Path, str]]
    """(realpath, source) for every run dir found, source in 'plan' | 'root' | 'registry'.
    `--plan` and the registry accept plan and solo dirs. `--root` yields plan dirs only.
    registry_dir overrides the registry location (tests use it)."""

def summarize(path, source='plan') -> dict      # a RunSummary, below
def detail(path, source='plan') -> dict         # a RunDetail, below
def task_detail(path, task_id, source='plan') -> dict | None       # a TaskDetail
def execution_detail(path, name, source='plan') -> dict | None     # an ExecutionDetail
def final_review_detail(path, name, source='plan') -> dict | None  # a FinalReviewDetail

class Catalog:
    def __init__(self, plans=(), roots=(), registry=True, registry_dir=None): ...
    def runs(self) -> list[dict]        # RunSummary list, newest `updated` first
    def get(self, key) -> dict | None   # RunDetail for that run_key, or None
    def task(self, key, task_id) -> dict | None
    def execution(self, key, name) -> dict | None
    def final_review(self, key, name) -> dict | None
```

`Catalog` re-discovers and re-reads on every call: runs change while they are watched. Files
that stop changing once written (`metrics.json`, `dataset.json`, `*.resolved` and `qa.last`
copies, findings of a reply) are parsed once and kept by `(dev, inode, size, mtime_ns)`; a
file is still stat'ed on every call, and one younger than 3 seconds is never kept, since a
rewrite within one mtime tick can keep its size. Every returned value is JSON-serializable
with `json.dumps(allow_nan=False)` (no `Path`, no sets, no bytes, no NaN), and a detail read
twice from unchanged files is equal: nothing depends on the clock.

Findings are parsed by `scripts/forge-contract.py` (`parse_findings`, `_LOCATION`), loaded
once on first use. `build_id()` does not hash it: a running server keeps the parser it
loaded. When it cannot be loaded, `findings_available` is `false` and every findings field
is `null`; verdicts and `gate` do not need it.

Text fields are capped so one huge file cannot blow up a response: `request`, `prompt`,
`approach`, `retry_findings` keep their first 20,000 characters; `qa_tail`, `dwarf_tail`
and `verification.log_tail` keep their last 4,000. A capped field ends (or, for tails,
starts) with `…` and the value of `truncated` lists the field names that were cut.

The lazy details cap differently: a cut text keeps its head and its tail around `\n…\n`
(two bounded reads, never the whole file). TaskDetail `texts`, FinalReviewDetail
`tasks_summary`, `qa_last` and `known_issues` keep 20,000 + 8,000 characters; ExecutionDetail
`prompt` and FinalReviewDetail `qa_input` keep 40,000 + 20,000; ExecutionDetail `last` keeps
its last 20,000 behind a leading `…`. Their `truncated` lists the field names (`prompt`,
`qa_input`, `last`, ...). A Finding's `text` keeps 20,000 characters, its `summary` 240.

## JSON API (`forge_ui/server.py`)

| Request | Response |
|---|---|
| `GET /api/runs` | `200 {"runs": [RunSummary, ...]}` |
| `GET /api/runs/<key>` | `200 RunDetail`, or `404 {"error": "not found"}` |
| `GET /api/runs/<key>/tasks/<id>` | `200 TaskDetail`; `<id>` is percent-decoded (strict UTF-8) and must be a `tasks.tsv` id (`solo` for a solo run). An id that decodes to `/`, NUL, U+FFFD, `.` or `..` is a 404 without a read |
| `GET /api/runs/<key>/executions/<role>-<uuid>` | `200 ExecutionDetail` |
| `GET /api/runs/<key>/final-reviews/<name>` | `200 FinalReviewDetail`; `<name>` is `final-review-<e>-<p>` or `integrate-<e>-<p>` |
| `GET /` | `forge_ui/static/index.html` |
| `GET /<name>` | that file from `forge_ui/static/` (`.html .js .css .svg` only, no subdirs) |
| anything else | `404 {"error": "not found"}`; any method but GET or HEAD: `405` |

The three lazy endpoints answer `404 {"error": "not found"}` for an unknown run, task,
execution or review. An execution is looked for only where executions are written:
`attempts/`, `tasks/<id>/attempts/`, `shadow/<id>/<a>-<k>/attempts/`,
`final-review-*/attempts/` and `integrate-*/final-review/attempts/`, in that order; a dir
that resolves outside the run dir (a symlinked `attempts/`, say) is ignored.

JSON responses: `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store`. The page loads `style.css`, `runmodel.js`, `graph.js`, `gantt.js`,
`panel.js`, `map3d.js` and `app.js` by those relative names, and `scene3d.js` and `iso.js`
(ES modules) only when the 3D view is used.

Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
and exactly one `Content-Security-Policy`. An `.html` file gets the page policy

```
default-src 'self'; style-src 'self'; script-src 'self' https://cdn.jsdelivr.net/npm/three@0.160.0/ <hashes>; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'
```

where `<hashes>` is `'sha256-<base64>'` for each `<script type="importmap">…</script>` in the
file as served (tag case-insensitive; CRLF and CR read as LF first, as browsers do), and
nothing when there is none. Every other response keeps
`default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`.

## RunSummary

```json
{
  "key": "3f9a0c1d2e4b",
  "path": "/Users/me/dev/forge-ui-plan",
  "source": "plan",
  "kind": "plan",
  "run_id": "20261016-153012-ab12",
  "repo": "/Users/me/dev/forge-ui",
  "goal": "first line of goal.txt",
  "decompose_level": "medium",
  "updated": 1760621412.5,
  "task_count": 5,
  "counts": {"MERGED": 3, "RUNNING": 1, "PENDING": 1},
  "verification_status": "PASS",
  "task_statuses": [{"id": "reader", "status": "MERGED"}, {"id": "page", "status": "RUNNING"}],
  "models": ["opus", "sol:xhigh"],
  "final_review": "FAIL"
}
```

- `task_statuses`: every task in `tasks.tsv` order (a solo run: `[{"id": "solo", "status": <solo status>}]`).
- `models`: the sorted distinct specs of the `dwarf` and `qa` columns (a solo run: its `dwarf`
  and `qa` specs as its task has them), nulls left out.
- `final_review`: the first word of `final-review.verdict`, else `null`; always `null` for a
  solo run. (RunDetail replaces it with an object, below.)

`kind` is `"plan"` or `"solo"`. `updated` is the newest mtime (epoch seconds, float) among
the run dir and the files the reader read. Any field it cannot determine is `null`
(`counts` is then `{}`, `task_count` 0).

For a solo run, `run_id` is the directory name, `decompose_level` and `verification_status`
are `null`, `task_count` is 1, and `counts` is `{<status>: 1}` using the solo status above.
The summary reads only `repo`, `goal.txt`, `status`, `verdict`, `dwarf.spec`, `qa.spec` and,
when a spec file is missing, `*.resolved`. It also stats `dwarf.log` and `qa.log` without
reading them, so `updated` moves while a model is writing a log. A plan summary reads
`final-review.verdict` besides the files it always read.

## RunDetail

Every RunSummary field, plus:

```json
{
  "request": "the user's request, verbatim",
  "settings": {"explore": 0.5, "shadow_rate": 0.5, "shadow_qa": ["opus", "sol:xhigh"], "timeout": 3600,
               "max_parallel": 2, "qa_threshold": "P2", "escalate_after": 3, "verify_retries": 1,
               "final_qa": "opus"},
  "verification": {"status": "PASS", "command": "python3 -m unittest", "log_tail": "...",
                   "runs": [{"n": 1, "kind": "initial", "exit": 0, "tree_changed": false,
                             "duration_s": 12.3, "log": "verification.run-1.log"}],
                   "commit": "9c1e4b7a..."},
  "waves": [["reader", "page", "graph"], ["server"], ["readme"]],
  "tasks": [Task, ...],
  "timeline": {"started": 1760620000.25, "ended": null, "runs": [Execution], "verifications": [Execution]},
  "integration": {"base": "1a2b...", "accepted": "3c4d...",
                  "merged": [{"id": "reader", "at": 1760621100.0}],
                  "integrations": [{"name": "integrate-1760622000-4300", "at": 1760622000.0,
                                    "verification_status": "PASS",
                                    "final_review": "integrate-1760622000-4300/final-review",
                                    "ended": 1760622300.0}]},
  "final_review": FinalReviewSummary,
  "final_reviews": [FinalReviewSummary, ...],
  "deferrals": ["wave 2: docs deferred (file overlap with a task already in this wave)"],
  "findings_available": true,
  "warnings": ["tasks/graph/route.jsonl line 2: not JSON"],
  "truncated": ["tasks/reader/prompt"]
}
```

- `settings` values are `null` when the file is missing or unparsable; `shadow_qa` is a
  list (empty when missing). `max_parallel`, `escalate_after`, `verify_retries` are integers,
  `qa_threshold` and `final_qa` the first line of their file. A solo run has no plan
  settings: every value is `null`, including `shadow_qa`.
- `verification` is `null` when none of `verification.status`, `.command`, `.log` exists. A
  solo run's `verification` is always `null`. `runs` comes from `verification.runs.tsv`
  (malformed rows are warned and skipped; an empty `exit` is `null`), `commit` is the first
  line of `verification.commit` or `null`.
- `timeline.started` is the earliest `started` of every execution the detail shows (tasks
  and their shadows, the listed final reviews, the run's own); `ended` is the latest `ended`,
  or `null` while any of them is `running` or when none has ended. `runs` and
  `verifications` are the run's own `parallel` and `verification` executions. A solo run's
  comes from its executions, with `runs` and `verifications` empty.
- `integration` is `null` unless `integration.base`, `accepted.integration`, a merged marker
  or an `integrate-*` dir exists. `merged` is every merged task with its marker's mtime,
  oldest first. `integrations` is every `integrate-<e>-<p>/`, newest first: `at` is `<e>`,
  `verification_status` the first word of its `verification.status`, `final_review` the
  path of its review dir or `null`, `ended` the latest end of its verification and review
  executions (`null` while one is unfinished or there is none).
- `final_review` is the newest FinalReviewSummary (review and integrate dirs together, by the
  epoch in the name), or `null`; `final_reviews` lists the newest 20, newest first.
- `deferrals`: the non-empty lines of `deferrals.txt`, `[]` when it is missing.
- `findings_available`: whether `scripts/forge-contract.py` could be loaded.
- A solo run has `integration` `null`, `final_review` `null`, `final_reviews` and
  `deferrals` `[]`.
- `waves` comes from `waves.tsv` (index 0 is wave 1); `[]` when it is missing. A solo run's
  `waves` is `[["solo"]]`.
- `tasks` keeps the order of `tasks.tsv`. A solo run has one task, described below.
- `warnings` names every file or line the reader skipped as malformed. Missing files are
  not warnings.

## Task

```json
{
  "id": "reader",
  "title": "Read plan dirs into plain dicts",
  "deps": [],
  "difficulty": "high",
  "files": ["forge_ui/reader.py", "tests/test_reader.py"],
  "dwarf": "astra",
  "qa": "opus",
  "wave": 1,
  "status": "MERGED",
  "branch": "forge/20261016-153012-ab12/reader",
  "attempt": 2,
  "attempts": [{"at": 1760620000.0, "attempt": 1, "status": "FAIL", "infra": null},
               {"at": 1760621000.0, "attempt": 2, "status": "PASS", "infra": null}],
  "escalations": [{"at": 1760620500.0, "attempt": 2, "from": "luna", "to": "sol"}],
  "verdict": "PASS",
  "accepted_by": null,
  "explored": true,
  "route": [{"at": 1760619000.0, "task": "reader", "tier": "high", "epsilon": 0.5,
             "candidates": ["astra", "opus"], "default": "opus", "chosen": "astra",
             "explored": true, "propensity": 0.25, "inputs": {}, "decision": "9c1e4b7a0d2f3e58"}],
  "clef": {"first_pass": {"p": 0.71, "model": "clef", "at": 1760619100.0,
                          "assignment": {"dwarf": "astra", "qa": "opus"}},
           "retry_pass": {"2": {"p": 0.64, "model": "clef", "at": 1760620600.0, "attempt": 2,
                                "assignment": {"dwarf": "sol", "qa": "opus"}}}},
  "shadow": [{"attempt": 1, "k": 0, "spec": "sol:xhigh", "primary": "opus",
              "status": "PASS", "verdict": "FAIL", "started": 1760620100.0}],
  "prompt": "full prompt.md text",
  "approach": null,
  "retry_findings": "...",
  "qa_tail": "last 4000 chars of qa.last",
  "dwarf_tail": "last 4000 chars of dwarf.last",
  "executions": [Execution, ...],
  "merged_at": 1760621100.0,
  "findings": FindingsSummary,
  "gate": {"threshold": "P2", "decision": "keep", "worst": "P3", "n_confirmed": 1,
           "n_unlabelled": 0, "n_below": 1},
  "known_issues": true
}
```

- `deps`, `files`: lists; `-` or empty becomes `[]`.
- `dwarf`, `qa`: the `tasks.tsv` value; `-`, empty and `UNASSIGNED` become `null`.
- `wave`: from `waves.tsv`, `null` if absent.
- `branch`: from `results.tsv`; else `forge/<run_id>/<id>` when `run_id` is known and the
  task has a `tasks/<id>/` dir; else `null`.
- `attempt`: integer from `tasks/<id>/attempt`, else the highest attempt in `attempts`,
  else `null`.
- `infra` in an attempt: `null` for empty, `-` or missing.
- `verdict`: `"PASS"`, `"FAIL"` or `null`, from `qa.last` as described above.
- `dwarf_tail`: the last 4,000 characters of `dwarf.last` (`tasks/<id>/dwarf.last` for a
  plan task, the run dir's `dwarf.last` for a solo run), or `null` when the file is missing.
  Capped like `qa_tail`.
- `explored`: `true` if `route.explored` exists or any `route` entry has `explored: true`.
- `clef.first_pass` is `null` when absent; `clef.retry_pass` is `{}` when there are none;
  its keys are the attempt numbers as strings.
- `shadow`: one entry per `shadow/<id>/<attempt>-<k>/` dir, sorted by attempt then k.
  `status` is `shadow.status`, `verdict` is parsed from that dir's `qa.last`; fields from
  `shadow.json` (`spec`, `primary`, `started`, `reviewed`, `pick`, as they are) that are
  missing are `null`, and a missing `started` falls back to the execution's. Each entry also
  has `execution` (the id of its newest `attempts/qa-*` execution, or `null`), `ended` and
  `duration_s` (that execution's), `findings` (FindingsSummary of its `qa.last`),
  `primary_verdict` (the verdict of the newest non-shadow `qa` execution of the task with the
  same attempt, or `null`) and `agreement`: `agree` or `disagree` when both verdicts are
  PASS or FAIL, else `unknown`.
- `executions`: every execution in `tasks/<id>/attempts/` (`task`, `dwarf`, `qa`); for a solo
  run, the run dir's `attempts/` (`solo`, `dwarf`, `qa`). Shadow executions are not in it.
- `merged_at`: the mtime of `tasks/<id>/merged`, or `null`.
- `findings`: FindingsSummary of the current `qa.last`, or `null`.
- `gate`: the parsed `qa.gate` (`n_*` integers, `-` is `null`), or `null` when missing or
  malformed (malformed is warned).
- `known_issues`: whether `known_issues.md` is a non-empty file.
- Text fields are `null` when the file is missing.

A solo run's one task uses this same shape. `id` is `solo` and `title` is the goal, or
`"Solo run"` when `goal.txt` has no first line. `dwarf` and `qa` come from `dwarf.spec` and
`qa.spec`, otherwise `alias[:effort]` from the matching `*.resolved` (`effort` is omitted when
it is missing or `<harness default>`). `wave` is 1, `branch` is `null`, and `attempts` come
from the run dir's `attempts.tsv`. `status` is the solo status; `attempt` is the highest `n`
in `attempts.tsv`, or `null`. `escalations`, `route` and `shadow` are empty. `verdict` and
`qa_tail` come from `qa.last`, and `prompt` comes from `prompt.md`. `deps` and `files` are
`[]`; `difficulty`, `accepted_by`, `approach` and `retry_findings` are `null`; `explored` is
false; `clef` is `{first_pass: null, retry_pass: {}}`. `request` on the detail is `request.md`.

## Execution

```json
{"id": "dwarf-0f1e2d3c4b5a69788796a5b4c3d2e1f0", "role": "dwarf",
 "parent": "task-9c1e4b7a0d2f3e589c1e4b7a0d2f3e58", "attempt": 2, "shadow": false,
 "state": "done", "started": 1760620000.25, "ended": 1760620612.75, "duration_s": 612.5,
 "phase": "complete", "phase_started": 1760620612.75,
 "phases": {"preparation": 1.2, "preflight": 0.4, "model": 590.1, "snapshot": 3.3, "verification": 0.0},
 "exit_code": 0, "infra_class": null, "timed_out": false,
 "model": {"spec": "sol:xhigh", "alias": "sol", "harness": "codex", "model": "gpt-sol", "effort": "xhigh",
           "clamped": null},
 "tokens": {"input": 120000, "cached": 90000, "output": 8000}, "prompt_bytes": 45678,
 "verdict": null, "findings": null}
```

- `started` is `wall_started`; `ended` is `started + elapsed_s.total` and `duration_s` is
  `elapsed_s.total`, both `null` until `exit_code` is set; `phase_started` is
  `wall_started + (phase_started - started)`. `phases` keeps the numeric `elapsed_s` entries
  except `total`. A value of the wrong type is dropped with a warning; a missing or invalid
  `metrics.json` leaves every field it gives `null`.
- `state`: `done` once `exit_code` is an integer. Otherwise `running` when its owner is live
  and it is the newest execution of its role in its dir, else `orphaned`. The owner is live
  when: the task's status is `RUNNING` (task executions and their children); the solo
  status is `RUNNING`; a shadow has no `shadow.status`; a final review's `state` is
  `running`; always, for the run's own `parallel` and `verification`. A `dwarf` or `qa`
  whose parent is not `running` is `orphaned`.
- `parent`: for `dwarf` and `qa`, the `task` (or `solo`) execution `dataset.json` names, else
  the latest one whose run (`started` to `ended`, open while unfinished) contains this
  start, else `null`. `task`, `solo`, `parallel` and `verification` have no parent.
- `attempt`: a `task` or `solo` execution takes the `attempts.tsv` row whose epoch is within
  120 s of its `ended` (closest first, each row once); else `dataset.json`'s
  `attempt_start + 1` (`attempt_start` when `resume` is set); else, while running, the
  task's `attempt` (a solo run: one more than its `attempts.tsv` rows). Children take their
  parent's; a shadow's is its dir's attempt; anything else is `null`.
- `model`: from the `<role>.resolved` copy (a running execution without one: the owner dir's
  live `<role>.resolved`); `spec` is `alias[:effort]`, `effort` is `null` for
  `<harness default>`. `null` for the pipeline roles and when there is no alias.
- `tokens` is `null` when all three counts are; `timed_out` is whether `<role>.timedout` exists.
- `verdict` and `findings` (FindingsSummary): `qa` only, from its own `qa.last` copy once done.
- Executions are ordered by `started` (unknown last), then `id`.

## FindingsSummary and Finding

```json
{"P0": 0, "P1": 1, "P2": 2, "P3": 0, "unlabelled": 0, "plausible": 1, "confirmed": 3, "worst": "P1"}
```

`P0`..`P3` count CONFIRMED findings by severity, `confirmed` all CONFIRMED ones, `unlabelled`
the CONFIRMED ones without a severity plus the UNSTATED ones (as the gate's `n_unlabelled`),
`plausible` the PLAUSIBLE ones; `worst` is the most severe CONFIRMED label or `null`. A
FindingsSummary is `null` when there is no text or no contract module.

```json
{"severity": "P1", "status": "CONFIRMED", "location": "src/x.py:12", "line": 7,
 "summary": "- [P1][CONFIRMED] src/x.py:12 — first line, at most 240 characters",
 "text": "the whole finding block"}
```

`status` is `CONFIRMED`, `PLAUSIBLE` or `UNSTATED`; `severity` may be `null`; `location` is the
first match of the contract's `_LOCATION` in the first line, or `null`; `line` is the
block's 1-based line in the reply. Lists of Findings keep the first 200.

## FinalReviewSummary

```json
{"name": "final-review-1760622000-4242", "source": "review", "at": 1760622000.0, "published": true,
 "verdict": "FAIL", "state": "done", "spec": "opus", "threshold": "P2", "base": "1a2b...", "tip": "3c4d...",
 "gate": {...}, "findings": FindingsSummary, "execution": "qa-...",
 "started": 1760622010.0, "ended": 1760622400.0, "duration_s": 390.0}
```

- `name` is the dir name; an integration's review lives in `integrate-<e>-<p>/final-review/`
  and is named `integrate-<e>-<p>` with `source` `integrate`. `at` is `<e>`.
- `published`: the plan's `final-review` link resolves to this dir (a link leaving the plan
  publishes nothing).
- `state`: `done` with a verdict, `infra` with `qa.infra`, `running` when it is the newest
  review and has `qa.input`, else `orphaned`.
- `spec`, `threshold`, `base`, `tip` come from `review.meta` (`threshold=none` is `null`).
- `execution` is its newest `attempts/qa-*`; `started`, `ended`, `duration_s` are that
  execution's.

## Lazy details

TaskDetail:

```json
{"run": "3f9a0c1d2e4b", "id": "reader",
 "texts": {"prompt": "...", "approach": null, "retry_findings": "...", "accepted_by": null,
           "known_issues": null, "dwarf_input": "...", "qa_input": "..."},
 "gate": {...},
 "reviews": [{"kind": "primary", "execution": "qa-...", "attempt": 1, "spec": "opus",
              "verdict": "FAIL", "state": "done", "findings": [Finding, ...]}],
 "truncated": [], "warnings": []}
```

`texts` read `prompt.md`, `approach.md`, `retry_findings.md`, `accepted.by`,
`known_issues.md`, `dwarf.input` and `qa.input` (a solo run: `prompt.md`, `known_issues.md`,
`dwarf.input`, `qa.input`; the others are `null`). `reviews` lists each non-shadow `qa`
execution with the findings of its own `qa.last` copy (`null` until it is done); then the
task's current `qa.last` as a `primary` review with `execution` `null`, only when no
execution's copy is identical to it; then one `shadow` review per shadow dir.

ExecutionDetail: the Execution, plus `resolved` (every `key=value` of `<role>.resolved`, or
`null`), `dataset` (`{trigger, parent, attempt_start, resume}` or `null`), `prompt` (the
`<role>.prompt` copy; while running without one, the owner dir's `<role>.delivered`, then
`.prompt`, then `.input`), `last` (the `<role>.last` copy), `findings` (a list of Findings for
a finished `qa`, else `null`, in place of the summary), `owner`
(`{"kind": "task" | "shadow" | "final_review" | "run", "id": ...}`: the task id (`solo` for a
solo run), `<task>/<attempt>-<k>`, the review name, or `null`), `truncated` and `warnings`.

FinalReviewDetail: the FinalReviewSummary, plus `meta` (every `review.meta` key),
`tasks_summary`, `qa_input`, `qa_last`, `known_issues` (texts or `null`), `findings` (a list
of Findings, in place of the summary), `truncated` and `warnings`.

## Status vocabulary for the page and the graph

Both the page and the graph mark a task by its status with the CSS class
`st-<status in lowercase>`, e.g. `st-merged`, `st-running`, `st-pending`. `style.css`
(owned by the page) defines, for every status above plus `pending`, a custom property
`--st` holding that status's color, e.g. `.st-merged { --st: #2f9e44; }`. No script
hard-codes status colors: `graph.js` and `gantt.js` give their SVG elements classes that
`style.css` fills with `var(--st, #888)` (the page CSP refuses style attributes, so a script
never writes one), and the 3D stage reads `--st` from a hidden `.st-<status>` probe. The
stage's own background and accent are `--stage-bg` and `--stage-accent` on `.stage-slot`.

Suggested meaning: merged and pass green, running blue, pending grey, fail, error and conflict
red, infra and timeout orange, blocked, interrupted, invalidated, unknown and nochanges muted
purple.

## The graph (`forge_ui/static/graph.js`)

A plain script (no modules) that defines one global:

```js
window.ForgeGraph = {
  render(container, run, options)   // run: a RunDetail; options: {selected, onSelect}
};
```

`render` replaces the contents of `container` with one SVG showing every task of
`run.tasks` as a node and every dependency as an edge from the dep to the dependent.
Columns are waves (`task.wave`); a task with no wave is placed one column right of its
deepest dependency (column 1 when it has none). Each node shows the task id and its
`dwarf` spec; a node with `explored: true` is visibly marked (e.g. a dashed outline and a
`*` after the spec). `options.selected` is a task id drawn highlighted.
Clicking a node calls `options.onSelect(taskId)`. It must work with 0 tasks (renders an
empty-state message), with deps that name unknown tasks (edge skipped), and with cycles
(no infinite loop).

## The page's other scripts

Plain scripts unless noted; each defines one global and none parses API text as HTML.

| File | Global | What it is |
|---|---|---|
| `runmodel.js` | `ForgeRunModel` | pure data, no DOM: the plate layout (archivision's `plate()`), the atlas, plan and execution models, forge-schedule's rules projected forward, the critical path, concurrency, the state of every node at a time `t` (replay), filters. Also a CommonJS module, for `node --test` |
| `gantt.js` | `ForgeGantt` | `render(container, run, {now, onSelect, sel, replay, critical, ghosts})`: attempts on wall-clock time in lanes; `strip(container, run, {now, replay})`: busy attempts and model calls against `max_parallel` |
| `panel.js` | `ForgePanel` | `render(host, ctx)`: the inspector for a run, task, execution, final review, verification or integration; fetches the lazy endpoints once per node |
| `map3d.js` | `ForgeMap3D` | `load()` imports `scene3d.js` (and so three.js) on first use, with a 12 s timeout; `state` is `idle`, `loading`, `ready` or `unavailable`, `message()` says why |
| `scene3d.js` | (ES module) | `createStage(host, {onSelect, onOpenRun, onBack, onMode})`: the atlas, plan and execution scenes, live status updates without rebuilding, overlays and replay |
| `iso.js` | (ES module) | the iso-glow engine ported from archivision: renderer, materials, blocks, labels, cables, camera, picking, drawn on demand |

Routes (the URL hash): `#/` the map of every run; `#/runs/<key>`; `…/tasks/<id>`;
`…/tasks/<id>/executions/<name>`; `…/executions/<name>` (an execution that belongs to no
task); `…/final-review[/<name>]`; `…/verification`; `…/integration`.
