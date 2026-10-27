# forge-ui data contract

This file is the contract between the three parts of forge-ui. It is fixed for tasks and
dwarves: implement it as written, and do not edit it as part of a task. Its owner revised it
deliberately for 0.3.0 (executions, findings, final reviews, the lazy endpoints and the 3D view)
and for 0.4.0 (diffs with pinned findings, the files heatmap, model facts and Fractal child tasks).

```
forge_ui/reader.py      reads forge run dirs from disk, returns plain dicts (no I/O beyond reads)
forge_ui/executions.py  the attempts/<role>-<uuid>/ dirs: parsing, nesting, attempt numbers, a parse cache
forge_ui/findings.py    QA findings and gate lines, parsed by scripts/forge-contract.py
forge_ui/diffs.py       unified diffs: parsing, the ```diff fence of a review prompt, pinning findings, cached reads
forge_ui/models.py      per-run model facts (attempts and reviews) for the leaderboard, cached per run
forge_ui/fractal.py     Fractal child tasks under $XDG_STATE_HOME/forge/fractal, read through dir fds
forge_ui/transcripts.py an agent CLI's `<role>.log` read as one conversation, whichever harness wrote it
forge_ui/server.py      serves those dicts as JSON plus the static page, 127.0.0.1 only; authenticates writes
forge_ui/control.py     the write routes: each one is a forge CLI call (see Controls)
forge_ui/static/        the page: index.html, app.js, style.css, graph.js and the scripts they load
```

Python 3.9+ standard library only. No third-party packages, no build step. The server makes
no network requests; the page loads three.js 0.160.0 from cdn.jsdelivr.net only when the 3D
view is used, and falls back to the 2D graph when it can't. forge-ui never writes to a run dir,
a repo or forge's config itself: every change is one call of the forge CLI (see Controls (write
endpoints) below), and `--read-only` removes even that.

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
| `verification.runs.tsv` | `n<TAB>kind<TAB>exit<TAB>tree_changed<TAB>duration_s<TAB>logfile` per run of the command; `kind` is `initial`, `rerun` or `jev_retry`, `exit` may be empty, `tree_changed` is `0` or `1`. A lanes verification adds `<TAB>lane<TAB>started` (an epoch) |
| `verification.retry.log` | the output of a deterministic or Jev rerun of a single command |
| `verification.lanes.json` | a lanes verification's live and final state, written by `scripts/forge-verify.py` (read up to 4 MB); `verification.lane.<name>.log` and `verification.lane.<name>.run-<k>.log` each lane's output |
| `verification.commit` | one line, the commit verified |
| `max_parallel`, `escalate_after`, `verify_retries` | one integer each |
| `qa_threshold` | `P0`..`P3`, the least severe finding that still blocks; `final_qa` the final review's QA spec |
| `deferrals.txt` | one line per task the wave planner deferred |
| `integration.base`, `accepted.integration` | one commit each: where the integration branch started, the revision the run accepted |
| `attempts/<role>-<uuid>/` | one *execution* (below) of the run itself: `parallel` (a `run`) or `verification` |
| `final-review-<epoch>-<pid>/` | one whole-run review: `qa.input`, `qa.last`, `verdict` (`PASS FAIL UNKNOWN INVALIDATED ACCEPTED`; absent while it runs or when nothing was judged), `qa.infra`, `qa.gate`, `known_issues.md`, `review.meta` (`key=value`: `base tip source spec threshold integration`, `threshold=none` when unset; `integration` is the integration-branch commit the reviewed result contains and is absent on older runs), `tasks.summary`, `attempts/qa-<uuid>/` |
| `final-review` | a symlink to the newest *published* review dir; `final-review.verdict` a copy of its verdict |
| `integrate-<epoch>-<pid>/` | one `integrate`: `setup.log`, `merge.log`, `verification.*`, `attempts/verification-<uuid>/`, and `final-review/` (a review dir as above) when it ran one |
| `tasks/<id>/merged` | exists when the task was merged; its mtime is when |
| `tasks/<id>/status` | one word: `RUNNING PASS FAIL ERROR INFRA TIMEOUT UNKNOWN INVALIDATED BLOCKED INTERRUPTED NOCHANGES CONFLICT` |
| `tasks/<id>/attempt` | the current attempt number |
| `tasks/<id>/attempts.tsv` | `epoch<TAB>attempt<TAB>status<TAB>infra_class` per line, one per attempt entry; status may also be `MERGED`, `infra_class` may be empty, `-` or missing |
| `tasks/<id>/escalations.tsv` | `epoch<TAB>attempt<TAB>from_spec<TAB>to_spec[<TAB>source]` per line; `source` (who decided: `policy`, a human's `retry --dwarf` as `retry`, or a `pin`) is missing on older rows, which read as `policy` |
| `tasks/<id>/qa.last` | the reviewer's final message; its last line matching `FORGE_VERDICT: PASS` or `FORGE_VERDICT: FAIL` is the verdict |
| `tasks/<id>/prompt.md`, `approach.md`, `retry_findings.md` | free text |
| `tasks/<id>/accepted.by` | who accepted a task over a reviewer's FAIL, and why (`key=value`: `task`, `status`, `reason`, `by`, `at`, `epoch`, `attempt`, `reviewed_commit`; older runs are free text; returned as text) |
| `tasks/<id>/qa.gate` | `threshold<TAB>decision<TAB>worst<TAB>n_confirmed<TAB>n_unlabelled<TAB>n_below` (`decision` is `accept` or `keep`, `-` for none) |
| `tasks/<id>/known_issues.md` | the confirmed findings a threshold tolerated |
| `tasks/<id>/dwarf.input`, `qa.input` | the prompts forge assembled; `<role>.delivered`, `<role>.resolved` the live dispatch files |
| `tasks/<id>/attempts/<role>-<uuid>/` | one execution of the task: `task`, `dwarf` or `qa` |
| `tasks/<id>/changes.diff` | `git diff --binary` of the task's work since its base, cumulative, rewritten every attempt; `changes.prev.diff` the previous attempt's; `committed.diff` base..the reviewed commit |
| `tasks/<id>/drift.txt`, `conflict.files` | one path per line: files the diff changed without declaring them; files that conflicted on merge |
| `attempts/qa-<uuid>/qa.prompt` | the copy of the prompt a reviewer read; the diff it reviewed is its **last** fenced block: a line exactly ` ```diff `, the diff, a line exactly ` ``` ` (a section may follow) |
| `final-review-*/combined.diff` | the diff a final review read, absent when larger than 400 KB (then not inlined in `qa.input`) |
| `fractal-selection.json` | `{"enabled": true, "run": "run-<20 hex>"}` (or `enabled: false`): the run's Fractal choice, see Fractal below |
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

Only the transcript endpoint opens an execution's `.log` (a `dwarf`'s or `qa`'s, see
ExecutionTranscript); no read opens a `.cmd`.

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
| `changes.diff`, `existing.diff` | the run's change, and the uncommitted changes the repo already had when it started |
| `fractal-selection.json` | as for a plan |

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
def task_diff(path, task_id, which='current', source='plan') -> dict | None  # a DiffDetail
def execution_diff(path, name, source='plan') -> dict | None       # a DiffDetail
def final_review_diff(path, name, source='plan') -> dict | None    # a DiffDetail
def files_detail(path, source='plan') -> dict | None               # Files
def run_findings(path, source='plan') -> dict | None               # RunFindings
def execution_transcript(path, name, source='plan') -> dict | None # an ExecutionTranscript
def fractal_detail(path, task_id, source='plan') -> dict | None    # a TaskFractal
def fractal_node(path, task_id, node, source='plan') -> dict | None       # a FractalNodeDetail
def fractal_node_diff(path, task_id, node, source='plan') -> dict | None  # a DiffDetail
def verification_log(path, lane=None, k=None, first=None, retry=False, source='plan') -> dict | None  # a VerificationLog

class Catalog:
    def __init__(self, plans=(), roots=(), registry=True, registry_dir=None): ...
    def runs(self) -> list[dict]        # RunSummary list, newest `updated` first
    def get(self, key) -> dict | None   # RunDetail for that run_key, or None
    def task(self, key, task_id) -> dict | None
    def execution(self, key, name) -> dict | None
    def final_review(self, key, name) -> dict | None
    def diff(self, key, task_id, which='current'), execution_diff(self, key, name),
        final_review_diff(self, key, name), files(self, key), findings(self, key), transcript(self, key, name),
        fractal(self, key, task_id),
        fractal_node(self, key, task_id, node), fractal_node_diff(self, key, task_id, node),
        verification_log(self, key, lane=None, k=None, first=None) -> dict | None
    def models(self) -> dict            # Models, over every run
    def locate(self, key) -> tuple[str, str] | None  # (run dir, 'plan' | 'solo'); None for an unknown key
```

`Catalog.locate` is how a write finds its run: a client names a run by key and never by path.

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
| `GET /api/runs/<key>/tasks/<id>/diff[/<which>]` | `200 DiffDetail`; `<which>` is `current` (the default: `changes.diff`), `prev`, `committed` (plan tasks), `existing` (solo) |
| `GET /api/runs/<key>/executions/<qa-or-dwarf>/diff` | `200 DiffDetail` of the diff that review read (a dwarf: its attempt's primary review) |
| `GET /api/runs/<key>/final-reviews/<name>/diff` | `200 DiffDetail` |
| `GET /api/runs/<key>/files` | `200 Files` |
| `GET /api/runs/<key>/findings` | `200 RunFindings` |
| `GET /api/runs/<key>/executions/<qa-or-dwarf>/transcript` | `200 ExecutionTranscript` |
| `GET /api/runs/<key>/verification/log[/lines/<n>]` | `200 VerificationLog` of `verification.log`: its tail, or with `/lines/<n>` a range |
| `GET /api/runs/<key>/verification/retry/log[/lines/<n>]` | `200 VerificationLog` of `verification.retry.log`, a single command's rerun |
| `GET /api/runs/<key>/verification/lanes/<lane>/log[/lines/<n>]` | `200 VerificationLog` of `verification.lane.<lane>.log` |
| `GET /api/runs/<key>/verification/lanes/<lane>/runs/<k>/log[/lines/<n>]` | `200 VerificationLog` of `verification.lane.<lane>.run-<k>.log` |
| `GET /api/models` | `200 Models` |
| `GET /api/health` | `200 {"app": "forge-ui", "version", "build", "pid", "controls": true\|false}`; `controls` is `false` in read-only mode |
| `GET /api/runs/<key>/controls` | `200` the `forge control show --json` object of a plan run (see Controls); `404` for a solo or unknown run |
| `POST /api/runs/<key>/controls` | change one control or pin; authenticated, see Controls (write endpoints) |
| `GET /api/runs/<key>/issues?scope=run\|repo&offline=0\|1&plausible=0\|1&dismissed=0\|1&only=fp,fp` | `200` the `forge issues --json` listing, run fresh on every request (no cache); see Controls (write endpoints) |
| `POST /api/runs/<key>/issues` | file GitHub issues for chosen findings; authenticated, 300 s timeout |
| `POST /api/runs/<key>/issues/dismiss` | dismiss or (`undo`) restore findings; authenticated |
| `POST /api/runs/<key>/delete` | dry-run or delete a run with `forge runs rm`; authenticated, 900 s timeout; later requests for the key answer `404` |
| `GET /api/models/reviewers` | `200` the `forge dataset reviewers --json` report (how each reviewer's PASSed work held up); no parameters |
| `GET /api/config?run=<key>` | `200` the `forge config show --json` of that run's repo |
| `POST /api/config` | set or unset one allowed key (`repo.verify`, `repo.setup`, `repo.verify-subset`, `jev.enabled`); authenticated |
| `GET /api/runs/<key>/export?texts=0\|1&paths=0\|1` | `200` the run as one HTML file (see Export), `Content-Disposition: attachment`, `Content-Security-Policy: default-src 'none'; sandbox`; `404` for an unknown run |
| `GET /api/runs/<key>/tasks/<id>/fractal` | `200 TaskFractal` |
| `GET /api/runs/<key>/tasks/<id>/fractal/nodes/<node>[/diff]` | `200 FractalNodeDetail`, or with `/diff` a `DiffDetail` of its `candidate.diff`; `<node>` matches `[A-Za-z0-9][A-Za-z0-9_-]{0,100}` |
| `GET /` | `forge_ui/static/index.html` |
| `GET /<name>` | that file from `forge_ui/static/` (`.html .js .css .svg` only, no subdirs) |
| anything else | `404 {"error": "not found"}` (an unknown path or run); a method a path does not take (PUT, DELETE, OPTIONS, a POST to a read path): `405` with `Allow`, except that a GET to a path that only takes POST (`/api/runs/<key>/delete`, `/api/runs/<key>/issues/dismiss`) answers `404` |

The lazy endpoints answer `404 {"error": "not found"}` for an unknown run, task,
execution or review; the diff endpoints also for a `<which>` the run kind has not (`existing`
of a plan task, `prev` of a solo run), the diff and transcript endpoints for an execution that is
not `qa` or `dwarf`; the
Fractal node endpoints for an unknown node or a task whose Fractal state is not `available`. A
diff that is not there yet is a `200` with `available: false`. An execution is looked for only where executions are written:
`attempts/`, `tasks/<id>/attempts/`, `shadow/<id>/<a>-<k>/attempts/`,
`final-review-*/attempts/` and `integrate-*/final-review/attempts/`, in that order; a dir
that resolves outside the run dir (a symlinked `attempts/`, say) is ignored. The verification
log endpoints answer `404` for a solo run, a `<lane>` not matching `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`
(so no `..`), a `<k>` or `<n>` that is not a positive decimal without a leading zero, and a log
that is missing, a symlink or not a regular file.

JSON responses: `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store`. The page loads `style.css`, `runmodel.js`, `graph.js`, `gantt.js`,
`verifyview.js`, `panel.js`, `map3d.js` and `app.js` by those relative names, and `scene3d.js` and `iso.js`
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
  "created": 1760619000.0,
  "task_statuses": [{"id": "reader", "status": "MERGED", "difficulty": "high", "title": "Read plan dirs", "accepted": true},
                    {"id": "page", "status": "RUNNING", "difficulty": "medium", "title": "Draw the page", "accepted": false}],
  "models": ["opus", "sol:xhigh"],
  "dwarf_models": ["sol:xhigh"],
  "qa_models": ["opus"],
  "final_review": "FAIL",
  "final_review_outdated": false
}
```

- `task_statuses`: every task in `tasks.tsv` order, with its `difficulty` (`null` when empty),
  `title` (its first 160 characters, then `…`) and `accepted` (`true` when `tasks/<id>/accepted.by`
  is a non-empty file). A solo run: `[{"id": "solo", "status": <solo status>, "difficulty": null,
  "title": <goal or "Solo run">, "accepted": false}]`. `accepted` does not change `status` or `counts`.
- `created`: when the run began: the earliest birth time (else mtime) of the files a run writes
  once at its start (plan: `run_id`, `repo`, `request.md`, `goal.txt`, `tasks.tsv`; solo: `prompt.md`,
  `repo`, `request.md`, `goal.txt`), else `null`. These are stat'ed only, and never move `updated`.
- `dwarf_models`, `qa_models`: `models` split by role.
- `models`: the sorted distinct specs of the `dwarf` and `qa` columns (a solo run: its `dwarf`
  and `qa` specs as its task has them), nulls left out.
- `final_review`: the first word of `final-review.verdict`, else `null`; always `null` for a
  solo run. (RunDetail replaces it with an object, below.)
- `final_review_outdated`: `true`, `false` or `null`, for the published review only (the dir the
  `final-review` link points to, when that link stays inside the plan). The same rule as
  FinalReviewSummary `outdated`, below. `null` when there is no such review, it has no verdict, or
  `accepted.integration` is missing; always `null` for a solo run. RunDetail keeps this field
  (it is not replaced by the newest review's `outdated`).

`kind` is `"plan"` or `"solo"`. `updated` is the newest mtime (epoch seconds, float) among
the run dir and the files the reader read. Any field it cannot determine is `null`
(`counts` is then `{}`, `task_count` 0).

For a solo run, `run_id` is the directory name, `decompose_level` and `verification_status`
are `null`, `task_count` is 1, and `counts` is `{<status>: 1}` using the solo status above.
The summary reads only `repo`, `goal.txt`, `status`, `verdict`, `dwarf.spec`, `qa.spec` and,
when a spec file is missing, `*.resolved`. It also stats `dwarf.log` and `qa.log` without
reading them, so `updated` moves while a model is writing a log. A plan summary reads
`final-review.verdict` besides the files it always read. For `final_review_outdated` it also
reads `accepted.integration` and, through the `final-review` link, that dir's `verdict` and
`review.meta`, and stats each task's `merged` marker. Those reads count toward `updated`, the
same way as every other file the summary reads, so a newer `accepted.integration` moves the
run's activity time. It does not open a review's attempts or scan the other review dirs.

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
                             "duration_s": 12.3, "log": "verification.lane.unit.run-1.log",
                             "lane": "unit", "started": 1760622000.5}],
                   "commit": "9c1e4b7a...",
                   "state": "done", "mode": "lanes", "live": null, "mutated": false,
                   "lanes": [VerificationLane, ...],
                   "failing": [{"lane": "e2e", "name": "checkout.Pay.test_refund", "suite": "checkout",
                                "file": "e2e.xml", "message": "expected 200, got 500", "excerpt": "...",
                                "known": false, "log_line": 12}]},
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
  line of `verification.commit` or `null`. A run's lanes runs carry `lane` and `started`; a
  single command's have `null` for both. A row of neither 6 nor 8 columns is warned and skipped.
- `verification.state` is `running` when the run's newest `verification` execution (the newest
  of `timeline.verifications`) is running, `done` when it has ended, and `null` when the run has
  none. While it runs, `status` is `RUNNING`, whatever a `verification.status` left by an earlier
  verification says.
- `verification.mode` is `lanes` when `verification.lanes.json` exists and was written at or
  after the newest verification execution started (or there is none), else `single`. A lanes file
  an earlier verification left is not read.
- `verification.live` is `null` unless `state` is `running`; then `{started, step, last_line,
  last_output_at, idle_s, log}`. `started` is the execution's start. In `single` mode the live log is
  whichever of `verification.log` and `verification.retry.log` was modified last: `step` is
  `{name, started: null}` for the last `::forge step name=<x> state=start` in its last 64 KB
  that no later `state=end` closed (a line holds no time), `last_line` its last non-empty line
  without ANSI codes (at most 300 characters), and `last_output_at` its mtime. In `lanes` mode
  they are the running lane's that printed last, with its recorded `step.started`. `idle_s` is
  now minus `last_output_at` (minus `started` before any output). `log` names the live log in
  `single` mode (`verification.log` or `verification.retry.log`; `null` before either exists) and is
  `null` in `lanes` mode.
- `verification.lanes` is `null` in `single` mode and when the lanes file is unreadable, not a
  JSON object, has no `lanes` list or is over 4 MB (each warned; the request never fails). In
  `lanes` mode it is every lane:

  ```
  VerificationLane = {"name": "e2e", "command": "npm run test:e2e", "group": "browser", "first": false,
                      "retries": 2, "options": {"retries": 2, "reports": "reports"},
                      "state": "fail", "exit": 1, "started": 1760622000.5, "ended": 1760622070.0,
                      "duration_s": 69.5, "idle_s": null,
                      "runs": [{"k": 1, "kind": "initial", "started": 1760622000.5, "ended": 1760622030.0,
                                "exit": 1, "log": "verification.lane.e2e.run-1.log"}],
                      "step": {"name": "browser", "started": 1760622010.0} | null,
                      "last_line": "2 failed, 30 passed", "last_output_at": 1760622069.8,
                      "log": "verification.lane.e2e.log",
                      "tests": {"pass": 30, "fail": 2, "skip": 1, "error": 0, "failing": [...],
                                "reports": ["e2e.xml"], "notes": [], "truncated": false} | null}
  ```

  `state` is one of `waiting running pass fail flaky known skipped interrupted`; `duration_s`
  runs from `started` to `ended`, or to now while the lane runs; `idle_s` is now minus
  `last_output_at` for a running lane, else `null`. `tests` is `null` when the lane wrote no
  report; its `failing` holds at most 200 entries (`truncated` says more failed), a `message` at
  most 500 characters and an `excerpt` at most 2,000; `log_line` is the 1-based line of the
  lane's log that first names the test, or `null`.
- `verification.failing` is every lane's `tests.failing`, in lane order, each with its `lane`:
  at most 200 entries, `[]` in `single` mode.
- `verification.mutated` is `true` when the checkout changed under the verification (the lanes
  file's `mutated`, or in `single` mode a run with `tree_changed`): reruns after it were skipped.
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
  epoch in the name), or `null`; `final_reviews` lists the newest 20, newest first. That newest
  review's `outdated` is its own. `final_review_outdated` stays the published review's flag, so
  the two differ when a newer review exists than the one `final-review` links to.
- `deferrals`: the non-empty lines of `deferrals.txt`, `[]` when it is missing.
- `findings_available`: whether `scripts/forge-contract.py` could be loaded.
- `fractal`: `null` without `fractal-selection.json`, else `{"enabled", "run", "available", "reason"}`
  (Fractal below). Plan and solo runs alike.
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
  "escalations": [{"at": 1760620500.0, "attempt": 2, "from": "luna", "to": "sol", "source": "policy"}],
  "verdict": "PASS",
  "accepted_by": null,
  "accepted": false,
  "accepted_attempt": null,
  "accepted_over": null,
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
- `source` in an escalation: the 5th column of `escalations.tsv` (`policy`, `retry` or `pin`, as forge wrote it); `policy` when the row has only 4 columns or the cell is empty.
- `verdict`: `"PASS"`, `"FAIL"` or `null`, from `qa.last` as described above.
- `accepted`: `true` when `accepted.by` is a non-empty file, on `task_statuses` and on the task.
  `status` stays what the merged marker or `status` file says (`MERGED` when the marker is there)
  and `counts` do not change.
- `accepted_attempt`: the integer `attempt=` of `accepted.by`, or `null` when the key is missing
  or not an integer (not an integer is warned). On the task, not on `task_statuses`.
- `accepted_over`: `"FAIL"` or `"UNKNOWN"` when `status=` in `accepted.by` says so; `null` for any
  other or missing value, including `PASS` (the panel then says plain "accepted"). On the task,
  not on `task_statuses`. A free-text `accepted.by` from an older forge has both null.
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
`[]`; `difficulty`, `accepted_by`, `approach` and `retry_findings` are `null`; `accepted` is
false; `accepted_attempt` and `accepted_over` are `null`; `explored` is false; `clef` is
`{first_pass: null, retry_pass: {}}`. `request` on the detail is `request.md`.

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
 "integration": "3c4d...", "outdated": false, "merged_since": [],
 "gate": {...}, "findings": FindingsSummary, "execution": "qa-...",
 "started": 1760622010.0, "ended": 1760622400.0, "duration_s": 390.0}
```

- `name` is the dir name; an integration's review lives in `integrate-<e>-<p>/final-review/`
  and is named `integrate-<e>-<p>` with `source` `integrate`. `at` is `<e>`.
- `published`: the plan's `final-review` link resolves to this dir (a link leaving the plan
  publishes nothing).
- `state`: `done` with a verdict, `infra` with `qa.infra`, `running` when it is the newest
  review and has `qa.input`, else `orphaned`.
- `spec`, `threshold`, `base`, `tip`, `integration` come from `review.meta` (`threshold=none` is
  `null`). `integration` is the integration-branch commit the reviewed result contains, or `null`
  when missing, empty, or not a commit. A commit is the object name `git rev-parse` writes: 40
  or 64 lowercase hex digits. Any other `integration=` value is warned on that
  `review.meta` (`integration is not a commit`; the summary's read of the published dir
  warns `final-review/review.meta`) and is not a known commit, so freshness uses the next rule.
  `source` on this object is the dir kind, `review` or `integrate`, not `review.meta`'s
  `source=` (a worktree path).
- `outdated`: whether the plan's integration branch has moved since this review. `null` when the
  review has no verdict, or when `accepted.integration` is missing. Otherwise the first rule that
  applies:
  1. `integration` is a commit: `true` when it differs from `accepted.integration`.
  2. Else, for `source` `review`: `true` when `tip` differs from `accepted.integration`.
  3. Else (an integrate review from an older forge, which has no `integration=` commit): `true`
     when any task merged after the review started.
- `merged_since`: the ids of tasks merged after the review started, oldest merge first. `[]` when
  the review is not outdated. It may also be `[]` for an outdated review whose only later merge
  fell inside the review's starting second.
- A task merged after the review when the mtime of its `merged` marker is `>= at + 1`, where `at`
  is the whole-second epoch in the review dir's name. A merge inside that starting second is
  ambiguous and is not counted. The execution's `started` is not used.
- `outdated` means tasks merged into the plan's integration branch since the review. Changes on
  the user's own branch after an integrate are not tracked.
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

## DiffDetail (`forge_ui/diffs.py`)

```json
{"run": "3f9a0c1d2e4b",
 "source": {"kind": "task", "id": "reader", "which": "current", "execution": null, "attempt": 2,
            "file": "tasks/reader/changes.diff"},
 "sources": [{"id": "current", "label": "changes.diff", "bytes": 1234, "url": "/api/runs/…/tasks/reader/diff"},
             {"id": "prev", "label": "changes.prev.diff", "bytes": 980, "url": "…/tasks/reader/diff/prev"},
             {"id": "qa-…", "label": "reviewed diff, attempt 1", "attempt": 1, "verdict": "FAIL",
              "kind": "primary", "bytes": 980, "url": "/api/runs/…/executions/qa-…/diff"}],
 "available": true, "reason": null, "bytes": 1234, "truncated": false,
 "files": [{"old": "ui/reader.py", "new": "ui/reader.py", "status": "modified", "binary": false,
            "additions": 5, "deletions": 1, "truncated": false,
            "hunks": [{"old_start": 38, "old_lines": 7, "new_start": 38, "new_lines": 10,
                       "section": "def _tasks(reader):", "lines": [" ctx", "-old", "+new"]}]}],
 "findings": [{"id": "qa-…:0", "review": {"kind": "primary", "execution": "qa-…", "attempt": 2, "spec": "opus"},
               "severity": "P1", "status": "CONFIRMED", "location": "ui/reader.py:42",
               "summary": "…", "text": "…",
               "pin": {"file": 0, "path": "ui/reader.py", "line": 42, "side": "new", "hunk": 0, "index": 5},
               "off_diff": null}],
 "warnings": [], "truncated_fields": []}
```

| Field | Meaning |
|---|---|
| `source.kind` | `task`, `execution`, `final_review` or `fractal`; `id` the task id, execution name, review name or node |
| `source.which` | `current` `prev` `committed` `existing` (task), `reviewed` (execution, final review), `candidate` (fractal) |
| `source.execution`, `attempt` | the qa execution whose prompt held the diff (a final review: its newest), its attempt; a task's `current` and `committed` carry the task's attempt, `prev` and `existing` `null` |
| `source.file` | the file read, relative to the run dir (fractal: to the Fractal state's `runs/<run>/tasks/`); `null` when none was |
| `sources` | what else can be shown. Task (and its executions): every diff file present (`id` = `which`, `label` = file name, `bytes` its size), then every primary and shadow qa execution whose prompt has a fence (`id` the execution, `label` `reviewed diff, attempt N` / `shadow review, attempt N`, `attempt`, `verdict`, `kind` `primary`/`shadow`, `bytes` of the fenced diff). Final review: itself plus the newest 20 reviews that kept a `combined.diff` (`id` the name, `kind` `final`, `verdict`, `bytes` or `null`). Fractal: `[{"id": "candidate", "label": "candidate.diff", "bytes"}]`. Every entry but fractal's has `url`, the endpoint that shows it |
| `available`, `reason` | whether a diff was read; else why: `no changes.diff` (any missing file's name), `unreadable (see warnings)`, `no diff in the review prompt`, `no review prompt`, `no review of this attempt` (a dwarf), `not inlined (larger than 400 KB)` (final reviews), `no review prompt yet`, `no candidate diff yet` |
| `bytes` | the file's size, or the fenced diff's (UTF-8); `null` when unavailable |
| `truncated` | the read stopped at 8 MiB (at a line end), the fence never closed, more than 300 files, or a file is `truncated` |
| `files[]` | per file of the diff: `old` (`null` when added), `new` (`null` when deleted), `status` `modified added deleted renamed copied`, `binary` (`Binary files … differ` or a `GIT binary patch`, whose body is skipped), `additions`, `deletions` (of the whole file, even when cut), `truncated`, `hunks` |
| `hunks[]` | `old_start`, `old_lines`, `new_start`, `new_lines` (a count left out is 1), `section` (after the second `@@`, or `null`), `lines`: each raw line with its marker (` `, `-`, `+`, `\` for `\ No newline at end of file`) |
| `findings[]` | the Findings (as above, without `line`) of the reviews attached, each with its `id` (see RunFindings), its `review` (`kind` `primary` `shadow` `final`, `execution`, `attempt`, `spec`), `pin` and `off_diff` |
| `pin` | `file` (index in `files`), `path`, `line`, `side` (`new`; `old` for a deleted file or a renamed file's old path), `hunk` and `index` (into that hunk's `lines`), or `null` |
| `off_diff` | `null` when pinned to a line, else `no location`, `file not in this diff`, `ambiguous path` or `line outside the changed hunks` (then `pin` has `file`, `line`, `side` and `hunk`/`index` `null`) |

Which diff: a task's `changes.diff` etc. from its dir (a solo run's from the run dir). An
execution's is the last fence of its `qa.prompt` copy; a running one's, of its owner's
`qa.delivered`, `.prompt`, then `.input`; a shadow's falls back to its dir's `qa.input`; a
dwarf's is its attempt's primary review's (same parent, else same attempt). A final review's
is `combined.diff`, else the fence of its newest execution's `qa.prompt`, then of its
`qa.input`.

Which findings: a task diff gets every review (primary, the task's own `qa.last` when no copy
matches it, shadows) of the task's latest attempt; an execution diff that review's (a
primary's also brings the shadows of its attempt); a final review's its `qa.last`'s; a fractal
node's none.

Parsing: `git diff --binary` output and plain unified diffs; renames, copies, mode changes,
`/dev/null`, C-quoted paths (`"a/caf\303\251"`), CRLF, the mnemonic and no-prefix forms.
A hunk's lines are counted, so a deleted `-- x` (the line `--- x`) stays content. At most 300
files, 20,000 hunk lines in all and 2,000 characters a line (then `…`).

Pinning: the path is the Finding's `location` (`a.py:10-12` arrives as `a.py:10`); without one,
the first `path:N` of its first line that names a file of this diff (`Makefile:3`). `line N`
alone pins only in a one-file diff. Paths are matched after `\` → `/` and dropping `./`, with
and without an `a/`/`b/` prefix: exactly first, then as a `/`-suffix either way; two files
matching is `ambiguous path`. The line is looked up on the new side (old for a deleted file).

Reads stay inside the run dir after symlinks are resolved, open regular files only (a FIFO is
never opened) and read at most 8 MiB; parses are kept by `(dev, inode, size, mtime_ns)` as
other parses are.

## RunFindings

Every review's Findings in one read, for the stage's marks: what a TaskDetail or DiffDetail
lists, for all tasks and final reviews at once, without `text` or `line`.

```json
{"run": "3f9a0c1d2e4b", "available": true,
 "tasks": [{"id": "reader", "reviews": [{"kind": "primary", "execution": "qa-…", "attempt": 1, "spec": "opus",
            "verdict": "FAIL", "findings": [{"id": "qa-…:0", "severity": "P1", "status": "CONFIRMED",
                                             "location": "ui/reader.py:42", "summary": "…"}]}]}],
 "final_reviews": [{"name": "final-review-…", "execution": "qa-…", "spec": "sol", "verdict": "FAIL",
                    "outdated": true, "merged_since": ["reader"],
                    "findings": [{"id": "qa-…:0", "severity": "P2", "status": "CONFIRMED", "location": null, "summary": "…"}]}],
 "truncated": [], "warnings": []}
```

A task's `reviews` are every primary and shadow review of every attempt that listed a Finding
(a solo run has one task, `solo`); `final_reviews` the newest 20 final reviews that did. Each
of those carries `outdated` and `merged_since`, the same values as that review's
FinalReviewSummary. A Finding's `id` is `<execution>:<n>`, `n` its 0-based place in the reply (`primary:<n>` or
`final:<n>` for a reply without an execution); the DiffDetail that pins it gives it the same
`id`, so a mark on the stage opens that Finding in the diff. `available` is `false` without the
contract module, and the lists are then empty.

## ExecutionTranscript (`forge_ui/transcripts.py`)

What the agent of one `dwarf` or `qa` execution said and did, read from its `<role>.log`: the
copy in its execution dir once it has finished, the owner dir's log while it runs (up to its
last whole line, so the page can poll it). The format is told from the log itself:

| Log | Written by | Read as |
|---|---|---|
| one JSON event per line: `thread.started`, `item.*`, `turn.*` | codex `--json` | `format` `codex` |
| one message per line in Claude's shape: `system`, `assistant`, `user`, `result` | grok `--output-format streaming-messages-json`; claude and openclaude `--output-format stream-json` | `claude` |
| one event per line: `text`, `reasoning`, `tool_use`, `step_finish`, `error` | opencode `--format json` | `opencode` |
| one `result` object with a `session_id` | claude `--output-format json` | the session file, see below |
| other JSON lines | e.g. agy `--output-format stream-json` (its `{"event": "result"}` is read as the result) | `json`: each line shown as it is |
| anything else | antigravity's printed output; openclaude and opencode runs from before forge streamed them | `text`: one event |

A claude log that holds only its result names the session the conversation is in. That one
file is read, `<config dir>/projects/<one dir>/<session id>.jsonl`, the config dir being
`$CLAUDE_CONFIG_DIR` then `~/.claude`, and only when the id is a UUID; it is the one file
outside the run dir forge-ui reads. Its subagent (`isSidechain`) and meta lines are left out.
Without it, the result alone is shown with a warning.

```json
{"run": "3f9a0c1d2e4b", "execution": "dwarf-…", "role": "dwarf", "state": "done", "attempt": 1, "spec": "luna",
 "harness": "codex", "repo": "/work/worktrees/reader", "owner": {"kind": "task", "id": "reader"},
 "format": "codex", "source": "log", "session": "0199…", "model": null, "cost": null,
 "tokens": {"input": 12000, "cached": 9000, "output": 800},
 "events": [{"kind": "reasoning", "text": "…"},
            {"kind": "command", "command": "rg -n status ui/reader.py", "output": "…", "exit": 0},
            {"kind": "edit", "files": ["/repo/ui/reader.py"], "patch": "-old\n+new"},
            {"kind": "message", "text": "…"}],
 "truncated": false, "warnings": []}
```

`attempt`, `spec` and `owner` are the Execution's; `harness` and `repo` (the worktree the agent ran
in, so the page can make its absolute paths relative) come from `<role>.resolved`. `source` is
`log`, `session` (claude's session file) or `none` (no log). An event has `kind`
and the keys that kind uses: `prompt` (`text`: the task as a session file holds it), `message`,
`reasoning`, `command` (`command`, `output`, `exit`), `edit` (`files`, `patch`: `-`/`+` lines,
or `input` when there are none), `tool` (`tool`, `input`, `output`), `result` (`text`, left out
with `same: true` when the last message said it; `tokens`), `error` (`text`), `text` (`text`,
`stream: "stderr"` for lines between JSON events) and `gap` (`count` events left out). Any event
may have `t` (epoch seconds), `id` (the harness's call id), `error` (`true` for a failed call or a
non-zero exit, `false` for a call answered without one) and `running: true` (a call not finished yet). `tokens` are the run's totals
(`input` counts cached input too). A message, a reasoning or a result keeps 20,000 characters, a
command, an input or an output its first two thirds and last third of 8,000 around `… n
characters …` (`<key>_cut` holds n). A log over 32 MB is read from its end; past 5,000 events or
4,000,000 characters the middle is left out (a `gap` event, `truncated: true`). Parses are kept by
the log's `(path, size, mtime_ns)` and the session file's.

## VerificationLog

The run's own verification logs, read by path (an integration's lane logs are not served yet):

```
{"name": "verification.lane.e2e.log", "size": 220000, "mtime": 1760622070.0,
 "tail": "line 14044\n...", "tail_first_line": 14044, "truncated": true}
```

`tail` is the file's last 64 KB, from the first line that starts inside them (when no line
does, from the first whole character), decoded as UTF-8 with invalid bytes replaced.
`tail_first_line` is the 1-based number of the tail's first line, found by counting the newlines
before it, or `null` when that would read past 64 MB. `truncated` says the tail is not the whole
file. With `/lines/<n>`:

```
{"name": "verification.log", "size": 11000, "first_line": 700, "lines": ["line 00700", ...],
 "truncated": false}
```

up to 400 lines from line `n` (1-based), each without its line ending and cut to 4,000
characters (ending `…`). Lines past the end give `lines: []`. `truncated` is `true` when more
lines follow, a line was cut, or reaching them took more than 64 MB of reading.

## Files

`GET /api/runs/<key>/files`: who planned, changed, drifted into or conflicted on each file.

```json
{"run": "3f9a0c1d2e4b",
 "files": [{"path": "ui/reader.py", "dir": "ui", "planned_by": ["reader"], "touched_by": ["reader"],
            "drift_by": [], "conflict_by": []}],
 "planned_dirs": [{"path": "ui/", "planned_by": ["page"]}],
 "tasks": [{"id": "reader", "wave": 1, "status": "MERGED", "planned": ["ui/reader.py"], "planned_all": false,
            "attempts": [{"attempt": 1, "execution": "qa-…", "files": ["ui/reader.py"]}],
            "current": ["ui/reader.py"], "drift": [], "conflicts": []}],
 "overlaps": [{"a": "reader", "b": "docs", "planned": true, "touched": ["ui/reader.py"], "same_wave": false}],
 "deferrals": ["…"], "truncated": false, "warnings": []}
```

- `tasks[]`: `planned` is the `files` column, normalised (`ui/` → `ui`); `planned_all` when it
  has `.`. `attempts`: each primary qa execution whose prompt has a fence, with the paths that
  diff touched (from its headers: the new path, a deleted file's old one). `current`: those of
  `changes.diff`. `drift`, `conflicts`: the lines of `drift.txt`, `conflict.files`. A solo
  run has one task `solo` (wave 1, nothing planned) with also `existing`, the paths of
  `existing.diff`.
- `files[]`, by path: every path touched (by `current` or any attempt), drifted, conflicted, or
  planned as a file. `dir` is its dirname (`""` at the root). `planned_by`: tasks with a planned
  entry equal to it or a dir above it (a `.` task is left out: see `planned_all`);
  `touched_by`, `drift_by`, `conflict_by` in `tasks.tsv` order.
- `planned_dirs`: planned entries that end in `/` or hold a touched file, with a trailing `/`.
- `overlaps`: each pair of tasks (in `tasks.tsv` order) whose planned entries overlap by
  forge-schedule's rule (equal, one a dir above the other, or either `.`) or that touched a
  file in common (`touched`, sorted); `same_wave` from `waves.tsv`.
- At most 2,000 files, 300 tasks and 5,000 overlaps; past them `truncated` is true.

## Models

`GET /api/models`: facts about every run, newest `updated` first, for the page to aggregate.

```json
{"generated": 1760621412.5, "runs": 12, "truncated": false,
 "tasks": [{"run": "3f9a0c1d2e4b", "task": "reader", "kind": "plan", "repo": "/x/y", "difficulty": "high",
            "created": 1760619000.0, "explored": true, "propensity": 0.125, "final_status": "MERGED",
            "accepted": true,
            "attempts": [{"n": 1, "alias": "luna", "spec": "luna", "source": "execution", "outcome": "fail",
                          "reason": null, "gate": false, "model_s": 297.0, "duration_s": 300.0,
                          "tokens": {"input": 120000, "cached": 90000, "output": 8000}, "timed_out": false}],
            "escalations": [{"n": 1, "from": "luna", "to": "sol:xhigh"}]}],
 "reviews": [{"run": "3f9a0c1d2e4b", "task": "reader", "n": 1, "kind": "primary", "alias": "opus", "spec": "opus",
              "verdict": "FAIL", "findings": FindingsSummary, "duration_s": 270.0, "tokens": {...},
              "agreement": null, "primary_alias": null}],
 "warnings": ["3f9a0c1d2e4b: tasks/x/attempts.tsv line 2: invalid attempt"]}
```

- `tasks[]`: one per task (`solo` for a solo run). `explored` as Task's; `propensity` that of
  the newest `route.jsonl` decision, else `null`; `final_status` the task (or solo) status;
  `accepted` whether `accepted.by` is non-empty (`false` for a solo run). `escalations` is
  `escalations.tsv` as it is (`n` its attempt column, `from`/`to` specs).
- `attempts[]`, from `attempts.tsv` (`epoch n status infra spec`): a row with an infra class
  or status `INFRA` was refunded and is dropped; the last row of a number wins (a QA-only
  resume reuses it). `outcome`: `PASS`/`MERGED` `pass`, `FAIL` `fail`. `TIMEOUT`/`ERROR` with a
  primary qa execution of that attempt is `censored` with `reason` `reviewer_timeout`/
  `reviewer_error`; else `TIMEOUT` is a `fail` when the attempt's last dwarf execution exited 7
  or timed out, otherwise `censored`, `dwarf_*` (it exited non-zero) or `forge_*`. Any other
  status is `censored` with the status lowercased (`nochanges`, `interrupted`, ...), as
  `scripts/forge_dataset/export.py` labels them.
- `alias`/`spec`/`source`: the last dwarf execution's model (`execution`), else the fifth
  `attempts.tsv` column (`attempts.tsv`), else for attempt 1 only the `tasks.tsv` dwarf (a solo
  run's `dwarf.spec`/`dwarf.resolved`) (`plan`), else all `null`.
- `gate`: the attempt passed although its (last) primary review said FAIL. `model_s`,
  `duration_s`, `tokens`, `timed_out`: that dwarf execution's `phases.model`, `duration_s`,
  `tokens`, `timed_out`, else `null`/`false`.
- `reviews[]`: every finished primary qa execution (`n` its attempt); every shadow whose
  `shadow.status` is `done` (`alias`/`spec` its execution's model, else `shadow.json`'s spec;
  `agreement` and `primary_alias` against the primary review of its attempt); every finished
  final review (`kind` `final`, `task` and `n` `null`, `verdict` `PASS`/`FAIL` else `null`,
  timing from its newest execution).
- Per run the facts are kept, keyed by its realpath, until its RunSummary's `updated`,
  `task_count`, `counts` or `final_review` changes (4,096 runs); a run with a `RUNNING` task is
  read again after 120 s. One computation runs at a time. Past 50,000 task rows the next run
  is left out and `truncated` is true. `warnings` (first 500) are prefixed by the run key.

## Fractal (`forge_ui/fractal.py`)

A run that chose Fractal (`fractal-selection.json`) has its tasks executed by
`scripts/forge_fractal` as trees of child tasks, in a managed run under the state root:
`realpath($XDG_STATE_HOME or ~/.local/state)/forge/fractal` (unlike forge, an empty
`XDG_STATE_HOME` counts as unset). forge-ui never imports that package.

| Path under `runs/<run>/` | Read |
|---|---|
| `run.json` (≤ 1 MiB) | its `runner` must be the run dir's realpath |
| `tasks/task-<sha256(T)[:16]>/` | the Fractal task of forge task `T` = realpath of `tasks/<id>` (plan) or of the run dir (solo) |
| `…/request.json` (≤ 2 MiB) | its `output` must be `T` |
| `…/state.json`, `outcome.json` (≤ 256 KiB) | `state`, `outcome` |
| `…/events.jsonl` | its last 64 KiB |
| `…/nodes/<n>/node.json` (≤ 4 MiB) | one node; a bigger one is skipped with a warning |
| `…/nodes/<n>/candidate.diff` | the node's imported change |

Everything is opened through directory fds, one component at a time with `O_NOFOLLOW`
(files also `O_NONBLOCK`, regular only, bounded): a symlink anywhere is refused with a
warning. `nodes/` is the only dir listed, and only names matching
`[A-Za-z0-9][A-Za-z0-9_-]{0,100}`. `control/`, `nodes/*/product/`, `planning/`, `steps/` and
the ledger are never opened, nor any absolute path a node stores. Parses are kept by
signature and path.

RunDetail `fractal`: `{"enabled", "run", "available", "reason"}`. `enabled` is true only for
`"enabled": true`; `run` must match `run-[0-9a-f]{20}`. Reasons: `Fractal is off for this run`,
`invalid fractal-selection.json` (not a ≤ 64 KiB JSON object), `invalid run id`,
`managed run not found`, `invalid run.json`, `runner mismatch`, `not supported on this
platform`.

TaskFractal:

```json
{"run": "3f9a0c1d2e4b", "task": "reader", "fractal_run": "run-…", "fractal_task": "task-…",
 "available": true, "reason": null,
 "state": {"status": "active", "acceptance": "pending", "elapsed": 120.5, "started_at": 1760600020.0,
           "ended_at": null, "exit_code": null, "error": null, "qa_status": null},
 "outcome": {"acceptance": "PASS", "verification": "passed", "task_status": "PASS"},
 "nodes": [{"id": "implementation", "parent": null, "depth": 0, "goal": "…", "difficulty": "medium",
            "paths": ["ui"], "deps": [], "model": "sol:xhigh", "resolved_model": "…", "status": "waiting",
            "ui_status": "RUNNING", "phase": "implementation", "iteration": 1, "started_at": 1760600022.0,
            "ended_at": null, "cost": 0.42, "unknown_cost_steps": 0,
            "tokens": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2},
            "decomposition": {"phase": "accepted", "decision": "split", "reason": "…", "children": ["a", "b"]},
            "error": null, "has_diff": false, "reserved": false}],
 "events": [{"at": 1760600021.0, "kind": "node_created", "node": "implementation"}],
 "warnings": [], "truncated": []}
```

- `available: false` (`state`, `outcome` `null`, `nodes`, `events` `[]`) with the RunDetail
  reasons, or `Fractal is not selected for this run`, `no Fractal task for this task yet`,
  `request does not match this task`, `invalid request.json`.
- `nodes`: tree order (a parent, then its children by id), at most 500 node dirs (else
  `truncated` has `nodes`). Children a decomposition admitted (`decomposition.admission` and
  `decomposition_history[].admission`) that have no dir yet are listed with `status`
  `reserved` and `reserved: true`. `depth` counts parents. `goal` and `error` keep 240
  characters, a decision's `reason` 2,000. Values of the wrong type are `null` (lists `[]`).
  `has_diff`: `candidate.diff` is a regular file.
- `ui_status`: `pending` `PENDING`, `active`/`waiting` `RUNNING`, `paused` `BLOCKED`,
  `completed` `MERGED`, `timeout` `TIMEOUT`, `interrupted`/`stopped` `INTERRUPTED`, `reserved`
  `PENDING`, any other status `FAIL`, none `UNKNOWN`.
- `events`: the newest 50 JSON lines of `events.jsonl` with a `kind`, oldest first: `at`,
  `kind`, `node`, and those of `phase decision status acceptance exit_code iteration cost
  difficulty model reason` the line has. Other lines are counted in a warning.

FractalNodeDetail (`…/fractal/nodes/<node>`): the node as listed, plus `goal`, `error` (20,000
characters), `last` and `planning_last` (their last 20,000, behind `…`), `planning_tokens`,
`decomposition` in full (`phase decision reason attempts error`, and `admission`: `name goal
difficulty paths deps model` each), `decomposition_history` (its newest 20, each like
`decomposition`, `reason` and `error` 2,000), and `run`, `task`, `fractal_run`,
`fractal_task`, `warnings`, `truncated` (the texts cut). A reserved node has these `null` or
`[]`, with its admission's `goal`.

## Export (`forge_ui/export.py`)

`GET /api/runs/<key>/export` answers one self-contained HTML file named
`forge-run-<run_id>.html` (the run id with anything but `[A-Za-z0-9._-]` made `-`).

- It holds `style.css` (with the labels' face, `D-DINCondensed-Bold.woff2`, inlined as a `data:`
  URL) and the classic scripts the run view uses (`theme.js icons.js datatable.js shell.js
  runmodel.js cloudmodel.js graph.js gantt.js panel.js diffview.js convo.js files.js map3d.js search.js
  app.js`), inline; the import map; a `<script type="application/json" id="forge-snapshot">` with
  `{meta, api}`; and a `<script type="text/plain" id="forge-stage">` holding `iso.js`,
  `cloud3d.js`, `findings3d.js`, `stagectl.js` and `scene3d.js` bundled into one module (each
  module in its own scope, relative imports bound to the earlier ones' exports, `createStage`
  exported), which `map3d.js` imports from a `blob:` URL.
- `api` maps each path the page requests to that endpoint's response as the server would answer
  it (server.api_get): `/api/runs` (this run only), the RunDetail, every TaskDetail, every
  ExecutionDetail the RunDetail names, every FinalReviewDetail, Files, RunFindings, and the
  Fractal trees and nodes. With `texts=1` also every diff the page can open (each task's diff and its `sources`,
  each final review's, each Fractal node's), every `dwarf` and `qa` ExecutionTranscript (at most 2 MB each),
  and the VerificationLog tail of `verification.log`, of each lane's log and of each lane run's log
  (under their tail paths; line ranges are not stored, so the page shows the tail).
- `texts=0` (the default) nulls these keys wherever they hold a string, list or object: `prompt
  last request log_tail dwarf_tail qa_tail qa_last qa_input tasks_summary known_issues texts text
  planning_last approach retry_findings dwarf_input error last_line message excerpt tail lines`, and stores no
  diff and no transcript. A finding keeps its `summary`, a failing test its `name`.
- `paths=0` (the default) makes every string that is a whole absolute path (but `/api/…`, `url`s
  and `command`s) its last component, and replaces the run dir, the repo and the home dir inside other
  strings with their last component (`~` for home).
- At most 1 MB of JSON per diff and 25 MB in all; what did not fit is listed in `meta.omitted`
  (`path`, `bytes`, `why`). `meta`: `format` `forge-ui-export`, `version` 1, `app`, `key`,
  `generated`, `texts`, `paths`, `omitted`.
- The page's `<meta http-equiv="Content-Security-Policy">`: `default-src 'none'; script-src
  <sha256 of the import map and each inline script> blob: https://cdn.jsdelivr.net/npm/three@0.160.0/;
  style-src <sha256 of the style>; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none';
  form-action 'none'; object-src 'none'`. Inside a script, `</script` and `<!--` are escaped; the
  JSON escapes `<`, `>` and `&`.
- The write token (below) is never in an export: the file has no controls.
- In the page, `app.js` reads the snapshot instead of the network, never polls, shows only the
  exported run (any other route goes to it) and a banner with the export's date and what it left
  out; the cloud, the leaderboard and notifications are not there.

## Controls (write endpoints)

forge-ui changes a run only by calling the forge CLI. The server holds no code that writes a run
dir, a repo or config; a write route builds an argv, runs it, and answers with what the CLI said.
`forge_ui/control.py` holds the route table (`ROUTES`: method, path pattern, handler), the body
validation, the call and the exit-code mapping; `server.py` does the authentication and gives
each authenticated POST to the table, so a new write route is one entry in it.

**The call.** `[<forge root>/forge, ...]`, an argv list with no shell, `stdin` closed, in a process
group of its own. `<forge root>` is two directories above `forge_ui/` and can be replaced as
`forge_ui.server.forge_cli`. A call that outlives its timeout (60 s) gets SIGTERM for its whole
group, SIGKILL 5 s later if anything is left, and is reaped, all before the run's lock is
released; the answer is `504 {"error": "timeout", "detail": "the operation may have partly
completed; reload to see the current state"}`.

**Runs are named by key.** The path holds the 12-hex run key and nothing else; the server finds
the run dir with `Catalog.locate(key)`. A body field never names a path, and a field the route does
not know is a `400`.

**Authentication.** Each server process makes a random token (`secrets.token_urlsafe(32)`). `GET /`
(and `/index.html`) carries it as `<meta name="forge-ui-token" content="...">` as the first element
of `<head>`; the page script reads it and sends it back. The page's CSP is unchanged (its hash
covers only the import map). Every POST must pass all of these, checked in this order, or it is
refused before any route or CLI runs:

| Check | Refusal |
|---|---|
| `Host` is `127.0.0.1` or `localhost` (any port) | `403 {"error": "forbidden"}` |
| server is not read-only | `405` (below) |
| `X-Forge-UI-Token` equals the token (`hmac.compare_digest`) | `403` |
| `Content-Type` is `application/json` (parameters allowed) | `400` |
| `Origin`, when sent, is exactly `http://` + the `Host` header | `403` |
| `Content-Length` present, a number, no `Transfer-Encoding` | `400` |
| the body is at most 64 KiB | `413 {"error": "payload too large"}` |
| the body is UTF-8 JSON and an object | `400 {"error": "bad request"}` |

No response carries `Access-Control-*` headers and `OPTIONS` is a `405` (`Allow` lists the methods
the path takes), so a page on another origin can neither preflight nor read an answer. The token is
not in `/export`, in a log line or in any JSON response.

**Read-only mode.** `python3 -m forge_ui --read-only`, or `FORGE_UI_READONLY=1` (also `true`, `yes`,
`on`). Every POST (after the Host check) is `405 {"error": "method not allowed"}` with
`Allow: GET, HEAD`, the page has no token, and `/api/health` says `"controls": false`
(`true` otherwise). Reads, `GET .../controls` included, still work. `forge ui open` passes the mode
to the server it starts, records it in `server.json` (`read_only`) and never reuses a running server
of the other mode: it replaces it as it does one running other code. `forge ui status` shows the
mode (`read_only` in its JSON).

**The CLIs' configuration.** The server `forge ui open` starts runs without any `FORGE_*` variable
of the shell it was started from. The CLIs it calls, for reads and writes alike, still need the forge configuration, so
`open` hands them these, as that shell has them (an empty value counts as unset; a relative
`FORGE_REGISTRY` or `FORGE_DATA_HOME` is made absolute against the shell's directory, except that a
`FORGE_DATA_HOME` starting with `~` is kept as given, because its reader expands the `~`; a
`FORGE_REGISTRY` starting with `~` is not expanded by anything, so it is made absolute like any other
relative path):
`FORGE_REGISTRY`, `FORGE_DATA_HOME`, `FORGE_DATASET`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and
`XDG_STATE_HOME` (`control.CLI_ENV_NAMES`). It writes them to `cli-env.json` (mode `0600`) in its
state dir, the server loads that file once when it starts (`_serve --cli-env FILE`) and every CLI
call gets them on top of the server's own environment; the server's environment itself is not
changed. No other variable is passed, and none of these is a secret. `server.json` records them as
`cli_env`. A server started by hand (`python3 -m forge_ui`) passes its own environment unchanged.

`open` never reuses a server whose recorded `cli_env` differs from the current shell's. For a
server that takes writes it names each variable that differs and both values, says to run
`forge ui stop` and then open again, exits `5`, prints no link and leaves the server running (it may
be in the middle of another shell's write). A read-only server (`FORGE_UI_READONLY=1`, asked for
again in the same mode) has no write to cut off, so it is replaced, as is one running other code, in
the other mode or reading another registry directory: the new one gets the current shell's
configuration, `open` prints the link and exits `0`. Read-only servers still run CLIs for their GET
routes, so their configuration matters too.

**Serialization.** One write per run key at a time. A second write to a run whose write is still
running is `409 {"error": "busy"}`, without reaching the CLI; other runs are not held up.

**Errors.** For a CLI that exits non-zero the answer is `{"error", "detail", "result"?}`: `detail`
is the last line of its stderr (at most 300 characters; the rest of stderr is not echoed) and
`result` the object its stdout holds, when that parses as one.

| CLI exit | Status | `error` |
|---|---|---|
| 0 | `200` | the CLI's stdout JSON object, as it is |
| 2 (invalid input) | `400` | `invalid` |
| 3 (refused: not live, MERGED, a Fractal plan, a pin that fails doctor, the control lock busy) | `409` | `refused` |
| 4 (a failure the CLI reported) | `500` | `failed` |
| anything else, a signal, a missing CLI, or a 0 with no JSON object | `502` | `bad gateway` |
| timeout | `504` | `timeout` |

For the controls routes, unknown run keys, solo runs (they have no controls) and runs the
catalog does not hold are `404 {"error": "not found"}`. The issues, dismiss, delete and config
routes below accept plan and solo runs alike (`forge issues` and `forge runs rm` take both);
for them only unknown keys and runs the catalog does not hold are `404`. Every write that reaches a route logs
`forge-ui: control <route> <run key> -> <status>` unless the server runs with `--quiet`.

### `GET /api/runs/<key>/controls`

`forge control show <plan dir> --json`: `200` with its object (`plan`, `run_id`, `live`, `fractal`,
`run` (the plan-wide controls), `tasks` (per task: `status`, `dwarf`, `qa`, `qa_threshold`,
`retry_budget`, `retries_used`, `pin`, `escalation_stop`, `escalation_next`) and `log`). No token
is needed to read it. Of `run` the page also reads `max_parallel` (1–64 or `null`), `infra_backoff`
(the shape, `exp:60` when unset), `escalate_effort` (`on`/`off`), `drained` and `stopping`
(`graceful`, `now` or `null`) while live, and `infra_paused` while live: one
`{scope, class, harness, model, tasks, cycle, until, exhausted}` per paused scope (`scope` is
`all`, `harness:<h>` or `model:<h>/<m>`; `until` an epoch, `exhausted` true once its retries are
spent). `log` is the newest `control.log` lines first; a scheduler's answer to a queued request
is a line with `actor: "scheduler"`, the request's `request` id, `outcome` (`applied` or
`refused`) and `reason`.

### `POST /api/runs/<key>/controls`

One of these bodies. Every string is non-empty, at most 256 characters, has no control character
and does not start with `-`; `400 {"error": "invalid", "detail": ...}` otherwise, and for a wrong
shape or type, before the CLI is called. Whether a key exists, a value is in range, a task is
known or a spec runs is for the CLI to say (its `400` or `409` comes back as above).

| Body | CLI call (after `forge`) |
|---|---|
| `{"action": "set", "key": "qa-threshold", "value": "P2"}` | `control set <dir> qa-threshold P2 --actor ui --json` |
| `{"action": "set", "key": "retry-budget", "value": "inherit", "task": "reader"}` | `control set <dir> retry-budget inherit --task reader --actor ui --json` |
| `{"action": "pin", "task": "graph", "spec": "sol:high:codex"}` | `control pin <dir> graph sol:high:codex --actor ui --json` |
| `{"action": "unpin", "task": "graph"}` | `control unpin <dir> graph --actor ui --json` |
| `{"action": "drain"}` | `control drain <dir> --actor ui --json` |
| `{"action": "resume"}` | `control resume <dir> --actor ui --json` |
| `{"action": "stop"}` (or `"now": false`) | `control stop <dir> --actor ui --json` |
| `{"action": "stop", "now": true}` | `control stop <dir> --now --actor ui --json` |
| `{"action": "retry", "task": "graph"}` | `control retry <dir> graph --actor ui --json` |
| `{"action": "retry", "task": "graph", "dwarf": "sol:high:codex"}` | `control retry <dir> graph --dwarf sol:high:codex --actor ui --json` |

`value` is always a string. `task` is optional on `set`, and only for the keys the CLI allows it
for; the page sets `max-parallel`, `infra-backoff` (`none` drops it) and `escalate-effort`
(`on`/`off`) plan-wide. `now` is a boolean. `add` is not exposed: a row is added to `tasks.tsv` by
hand, so its CLI is the place to queue it. A success is
`200 {"ok": true, "changed": {...}, "applies": "...", "controls": {...}}`, where `controls` is the
`GET` object after the change; `drain`, `resume` and `stop` answer the same way and are refused
(`409`) when no scheduler is live. A `retry` only queues: `200 {"queued": true, "request": "<id>"}`.
Its refusals before queueing (not live, a MERGED task, a spec that fails doctor) are the CLI's
`409`/`400`; the scheduler applies or refuses it later, in the `log` line with that `request` id.

### `GET /api/runs/<key>/issues`

`forge issues <dir> --json [flags]`: the findings the run left open. Query (all optional, an
unknown or repeated parameter is a `400`): `scope=run|repo` (`repo` adds `--all-runs`),
`offline=0|1` (`--offline`), `plausible=0|1` (`--include-plausible`), `dismissed=0|1`
(`--include-dismissed`), `only=fp,fp` (`--only`; one to 25 fingerprints, each `^[0-9a-f]{12}$`).
`200` with the CLI's object (`findings`, ...). **Never cached**: every request is one CLI call; the
page asks for an `offline=1` listing first for speed and then for the full one. It takes no lock (a
busy CLI is its own exit 3, `409`). No token is needed. Plan and solo runs. Timeout 120 s.

### `POST /api/runs/<key>/issues`

File issues for findings. Body: `{"scope": "run"|"repo" (default run), "fingerprints": [1 to 25 of
^[0-9a-f]{12}$], "include_plausible": bool, "labels": [at most 5 texts of 1 to 50 characters, no
control character, not starting with "-"]}`; any other field is a `400`. Runs `issues <dir> --only
<fps> --max-issues <number of fps> --apply --json` plus `--all-runs`, `--include-plausible` and one
`--label` per label. `200` with the CLI's object. Under the run's write lock (`409 busy`). Timeout
300 s.

### `POST /api/runs/<key>/issues/dismiss`

Body: `{"fingerprints": [1 to 25], "reason"?: text of at most 500 characters without control characters (newline and tab are allowed), "undo"?: bool}`. Runs
`issues <dir> --json --dismiss <fps> [--reason R]`, or `--undismiss <fps>` when `undo` is true
(a reason with `undo` is a `400`). Under the run's write lock. Timeout 60 s.

### `POST /api/runs/<key>/delete`

Body: `{"dry_run": bool (false), "archive": bool (true), "worktrees": bool (false), "branches": bool
(false)}`. Runs `runs rm <dir> --json` with `--dry-run` or `--yes`, plus `--no-archive` when
`archive` is false, `--worktrees`, `--branches`. `200` with the CLI's object. The CLI prints its
JSON on failure too, so a non-zero exit answers with that object as `result` (its `steps` show what
completed): exit 2 `400`, exit 3 (busy or the run is active) `409`, exit 4 (failed during
deletion) `500`. Deletion holds the run's write lock and a server-wide lock on the run's real
directory (`409 busy` when either is held). The run key is looked up again under the lock, and
once the directory is gone every later request for that key is `404`. Timeout 900 s.

### `GET /api/models/reviewers`

Runs `dataset reviewers --json` and answers with its object: `horizon_days`, `min_landed`,
`survives_at`, `reviewers` (per reviewer alias: `landed`, `reverted`, `rewritten`,
`did_not_hold`, `enough`, and `rate` and `ci` only when `enough`, else `null` with a `note`),
`unattributed`, `skipped` and `other_horizons`. Any query parameter is a `400`. Not cached: every
request runs the CLI, and the page asks once per change of `/api/models`. Advisory: it feeds the
Reviewers table's Did not hold column and nothing else. Timeout 60 s.

### `GET /api/config?run=<key>` and `POST /api/config`

The repository is the run's own `repo` file (first line, an existing absolute directory; else
`409`), never a client field. `GET` runs `config show --repo <repo> --json` and answers with its
object. `POST` takes `{"run": <key>, "key": one of repo.verify, repo.setup, repo.verify-subset,
jev.enabled, "value": text | "unset": true}` (exactly one of the two; any other field, `repo`
included, is a `400`) and runs `config set <key> <value> --repo <repo> --json` or `config unset
<key> --repo <repo> --json`. A value is a non-empty text of at most 4096 characters without
control characters (newline and tab are allowed), not starting with `-`; `jev.enabled` takes
`true` or `false` and cannot be unset; `repo.verify-subset` must contain `{files}`. `200` with the
CLI's object (`ok`, `key`, `previous`, `value`). Writes serialize on one server-wide config lock
(`409 busy`). Unknown run `404`. Timeout 60 s.

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
| `runmodel.js` | `ForgeRunModel` | pure data, no DOM: the plate layout (archivision's `plate()`), the atlas, plan and execution models, forge-schedule's rules projected forward, the critical path, concurrency, the state of every node at a time `t` (replay, verification lanes included), the finish node's verification LEDs (`verifyLeds`: one per lane in lanes mode, else one per run), filters. Also a CommonJS module, for `node --test` |
| `gantt.js` | `ForgeGantt` | `render(container, run, {now, onSelect, sel, replay, critical, ghosts})`: attempts on wall-clock time in lanes; `strip(container, run, {now, replay})`: busy attempts and model calls against `max_parallel`. Under the attempts, the finish rows: `verify` (single-command verifications; the newest split into its runs back to back from its start, dashed because that placement is approximate), a `verify · <lane>` row per lane of a lanes verification (each run and rerun at its own time; a run with no exit is UNKNOWN once it ended, never FAIL) and `final review`. A lane or run item opens the verification inspector |
| `panel.js` | `ForgePanel` | `render(host, ctx)`: the inspector for a run, task, execution, final review, verification, integration or Fractal node; fetches the lazy endpoints once per node; `ctx.diffHash(spec)` links into the diff viewer, `ctx.onFractal(task, TaskFractal)` hands a task's tree to the stage |
| `verifyview.js` | `ForgeVerify` | `render(host, {run, now, fetchJSON, rerender})`: the verification inspector panel.js delegates to: while it runs, the elapsed time, step, last line and a flag past 5 minutes without output, and a live tail of the followed lane's log (or the single log) read again each poll; when it failed, the failing tests first, grouped by lane with new or known, each opening its lane's log at its line (in an export, the tail); a lanes table (state, time, reruns, counts). Pure helpers (`model`, `laneRow`, `groupFailing`, `idleWarning`, `openStep`, `tailLane`, `logPath`, `linesPath`, `tailLines`) are a CommonJS module too |
| `diffview.js` | `ForgeDiff` | `render(host, {run, spec, fetchJSON, rerender, href, closeHref})`: a DiffDetail with its findings under their lines; `spec` is `{kind: task, task, source, file}`, `{kind: final, name, file}` or `{kind: fractal, task, node, file}` |
| `convo.js` | `ForgeConvo` | `render(host, {run, spec: {task, exec}, brief, live, fetchJSON, rerender, diffHash, closeHref})`: an ExecutionTranscript as a timeline, with search, the errors and, while the execution runs, a read of its log every 2.5 s |
| `files.js` | `ForgeFiles` | `matrix(data, run, opts)` (pure: Files as rows of dirs and files against task columns by wave) and `render(host, data, {run, sel, onSelectTask, diffHref, prefs, savePrefs})`: the run's Files mode. Also a CommonJS module |
| `cloudmodel.js` | `ForgeCloudModel` | pure: projects, per-run facts, the cloud's filters, its five layouts (anchors from every run, dragged runs kept), label picking, layout files. Also a CommonJS module |
| `cloudui.js` | `ForgeCloudUI` | `create(root, ctx)`: the cloud's filter panel, layout and shape switches, position tools, legend and run card; a list of the same runs without 3D |
| `modelstats.js` | `ForgeModelStats` | pure: the leaderboard's aggregation of `/api/models` (`leaderboard`, `wilson`, `options`). Also a CommonJS module |
| `leaderboard.js` | `ForgeLeaderboard` | `render(host, data, {runs, prefs, savePrefs, modelHref, now})`: the `#/models` page |
| `search.js` | `ForgeSearch` | `init({runs, commands, go, cloudFilter})`: the quick jump (Ctrl/⌘ K, `/`), ranked by `ForgeRunModel.searchRank` |
| `notify.js` | `ForgeNotify` | `init({go})`, `observe(runs)` on each poll of the run list (`ForgeRunModel.statusEvents`), `open()`, `background()`: the bell, its log, the title count, the favicon dot and browser notifications |
| `map3d.js` | `ForgeMap3D` | `load()` imports `scene3d.js` (and so three.js) on first use, with a 12 s timeout, or in an export the bundled stage from a `blob:` URL; `state` is `idle`, `loading`, `ready` or `unavailable`, `message()` says why |
| `scene3d.js` | (ES module) | `createStage(host, {onSelect, onOpenRun, onBack, onMode, cloud})`: the atlas, plan and execution scenes, live status updates without rebuilding, overlays, replay, Fractal trees over opened tasks (`setFractal`), and `showCloud(runs, opts)` |
| `cloud3d.js` | (ES module) | `createCloud(engine, host, callbacks)`: the run cloud on the stage's engine (blocks or orbs, labels, frames, drag and drop kept per layout in this browser) |
| `iso.js` | (ES module) | the iso-glow engine ported from archivision: renderer, materials, blocks, labels, cables, camera (plate and free profiles), picking and grabbing, shared solids, sprites, drawn on demand |

Routes (the URL hash): `#/` the map of every run; `#/cloud`, `#/cloud/runs/<key>` (a run
selected), `#/cloud/projects/<id>` (a project in focus), `#/cloud/models/<alias>` (the cloud
filtered to that model); `#/models` the leaderboard; `#/runs/<key>`; `…/tasks/<id>`;
`…/tasks/<id>/executions/<name>`; `…/executions/<name>` (an execution that belongs to no
task); `…/final-review[/<name>]`; `…/verification`; `…/integration`; `…/tasks/<id>/fractal/<node>`;
and the diff viewer: `…/tasks/<id>/diff[/<source>][/file/<path>]` (`source` is `current`, `prev`,
`committed`, `existing` or an execution), `…/final-review/<name>/diff[/file/<path>]`,
`…/tasks/<id>/fractal/<node>/diff[/file/<path>]` (`path` percent-encoded whole).

Kept in the browser (localStorage, every access guarded): `forge-ui-view`, `-mode`, `-overlay`,
`-speed`, `-map-filter`; `forge-ui-cloud` (filter, layout, shape, panel) and
`forge-ui-cloud-layout-v1` (dragged positions per layout, at most 5,000 per layout);
`forge-ui-models` (leaderboard filters); `forge-ui-recent` (quick-jump picks); `forge-ui-notify`,
`-notify-log` (the last 100) and `-notify-seen` (ten minutes, to notify once across tabs).
