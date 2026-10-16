# forge-ui data contract

This file is the contract between the three parts of forge-ui. It is fixed: implement it
as written, and do not edit it as part of a task.

```
forge_ui/reader.py   reads forge plan dirs from disk, returns plain dicts (no I/O beyond reads)
forge_ui/server.py   serves those dicts as JSON plus the static page, 127.0.0.1 only
forge_ui/static/     index.html, app.js, style.css (the page) and graph.js (the graph)
```

Python 3.9+ standard library only. No third-party packages, no build step, no network
access, no CDN. The UI is read-only: nothing in forge-ui writes to a plan dir, a repo or
forge's config.

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
| `tasks/<id>/merged` | exists when the task was merged (content irrelevant) |
| `tasks/<id>/status` | one word: `RUNNING PASS FAIL ERROR INFRA TIMEOUT UNKNOWN INVALIDATED BLOCKED INTERRUPTED` |
| `tasks/<id>/attempt` | the current attempt number |
| `tasks/<id>/attempts.tsv` | `epoch<TAB>attempt<TAB>status<TAB>infra_class` per line, one per attempt entry; status may also be `MERGED`, `infra_class` may be empty, `-` or missing |
| `tasks/<id>/escalations.tsv` | `epoch<TAB>attempt<TAB>from_spec<TAB>to_spec` per line |
| `tasks/<id>/qa.last` | the reviewer's final message; its last line matching `FORGE_VERDICT: PASS` or `FORGE_VERDICT: FAIL` is the verdict |
| `tasks/<id>/prompt.md`, `approach.md`, `retry_findings.md` | free text |
| `tasks/<id>/accepted.by` | free text: who accepted a task over a reviewer's FAIL, and why |
| `tasks/<id>/route.jsonl` | one JSON object per line, one per routing decision: `at` (epoch), `task`, `tier`, `epsilon`, `candidates` (list of specs), `default` (spec), `chosen` (spec), `explored` (bool), `propensity` (probability the chosen spec had), `inputs` (object), `decision` (a short hash identifying the decision). Keep unknown fields |
| `tasks/<id>/route.explored` | exists when the task's route was an exploratory pick |
| `tasks/<id>/clef.first_pass` | JSON: `p` (predicted probability the first attempt passes), `model`, `at`, `plan_table`, `assignment` (`{dwarf, qa}`) |
| `tasks/<id>/clef.retry_pass.<n>` | JSON, same shape plus `attempt` = n |
| `shadow/<id>/<attempt>-<k>/shadow.json` | JSON: `task`, `attempt`, `reviewed`, `primary`, `spec`, `pick`, `started`. A second, never-read review of attempt `<attempt>` |
| `shadow/<id>/<attempt>-<k>/shadow.status` | one word |
| `shadow/<id>/<attempt>-<k>/qa.last` | the shadow reviewer's final message, verdict as above |

**Task status** is `MERGED` if `tasks/<id>/merged` exists, otherwise the first word of
`tasks/<id>/status`, otherwise `PENDING`.

**Which dirs are plan dirs:** a directory containing a `tasks.tsv` file.

Only the ids listed in `tasks.tsv` are tasks: `tasks/` may also hold forge's own dirs (e.g.
`tasks/_integration/`), and a task dir holds many other files (`attempts/` is a directory of
receipts, not `attempts.tsv`), all of which the reader ignores.

## Where runs come from

1. `--plan DIR` (repeatable): that dir, if it is a plan dir.
2. `--root DIR` (repeatable): every plan dir at depth 0, 1 or 2 under DIR (DIR itself,
   its children, its grandchildren). Hidden dirs (`.name`) are not descended into.
3. forge's run registry, unless `--no-registry`: every `*.txt` file in
   `$XDG_CONFIG_HOME/forge/runs/` (default `~/.config/forge/runs/`). Each file is a
   `# ...` header line then one absolute path per line. Paths that no longer exist or are
   not plan dirs are skipped silently.

Duplicates (same `os.path.realpath`) are listed once. The first source that found a dir
wins for `source`.

## Python API (`forge_ui/reader.py`)

```python
def run_key(path) -> str
    """Stable opaque id of a plan dir: sha1 of its realpath, first 12 hex chars."""

def discover(plans=(), roots=(), registry=True, registry_dir=None) -> list[tuple[Path, str]]
    """(realpath, source) for every plan dir found, source in 'plan' | 'root' | 'registry'.
    registry_dir overrides the registry location (tests use it)."""

def summarize(path, source='plan') -> dict      # a RunSummary, below
def detail(path, source='plan') -> dict         # a RunDetail, below

class Catalog:
    def __init__(self, plans=(), roots=(), registry=True, registry_dir=None): ...
    def runs(self) -> list[dict]        # RunSummary list, newest `updated` first
    def get(self, key) -> dict | None   # RunDetail for that run_key, or None
```

`Catalog` re-discovers and re-reads on every call (no cache): runs change while they are
watched, and plan dirs are small. Every returned value is JSON-serializable with
`json.dumps` (no `Path`, no sets, no bytes).

Text fields are capped so one huge file cannot blow up a response: `request`, `prompt`,
`approach`, `retry_findings` keep their first 20,000 characters; `qa_tail` and
`verification.log_tail` keep their last 4,000. A capped field ends (or, for tails,
starts) with `…` and the value of `truncated` lists the field names that were cut.

## JSON API (`forge_ui/server.py`)

| Request | Response |
|---|---|
| `GET /api/runs` | `200 {"runs": [RunSummary, ...]}` |
| `GET /api/runs/<key>` | `200 RunDetail`, or `404 {"error": "not found"}` |
| `GET /` | `forge_ui/static/index.html` |
| `GET /<name>` | that file from `forge_ui/static/` (`.html .js .css .svg` only, no subdirs) |
| anything else | `404 {"error": "not found"}`; any method but GET or HEAD: `405` |

JSON responses: `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store`. The page loads `style.css`, `graph.js` and `app.js` by those
relative names.

## RunSummary

```json
{
  "key": "3f9a0c1d2e4b",
  "path": "/Users/me/dev/forge-ui-plan",
  "source": "plan",
  "run_id": "20261016-153012-ab12",
  "repo": "/Users/me/dev/forge-ui",
  "goal": "first line of goal.txt",
  "decompose_level": "medium",
  "updated": 1760621412.5,
  "task_count": 5,
  "counts": {"MERGED": 3, "RUNNING": 1, "PENDING": 1},
  "verification_status": "PASS"
}
```

`updated` is the newest mtime (epoch seconds, float) among the plan dir and the files the
reader read. Any field it cannot determine is `null` (`counts` is then `{}`,
`task_count` 0).

## RunDetail

Every RunSummary field, plus:

```json
{
  "request": "the user's request, verbatim",
  "settings": {"explore": 0.5, "shadow_rate": 0.5, "shadow_qa": ["opus", "sol:xhigh"], "timeout": 3600},
  "verification": {"status": "PASS", "command": "python3 -m unittest", "log_tail": "..."},
  "waves": [["reader", "page", "graph"], ["server"], ["readme"]],
  "tasks": [Task, ...],
  "warnings": ["tasks/graph/route.jsonl line 2: not JSON"],
  "truncated": ["tasks/reader/prompt"]
}
```

- `settings` values are `null` when the file is missing or unparsable; `shadow_qa` is a
  list (empty when missing).
- `verification` is `null` when no `verification.*` file exists.
- `waves` comes from `waves.tsv` (index 0 is wave 1); `[]` when it is missing.
- `tasks` keeps the order of `tasks.tsv`.
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
  "qa_tail": "last 4000 chars of qa.last"
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
- `explored`: `true` if `route.explored` exists or any `route` entry has `explored: true`.
- `clef.first_pass` is `null` when absent; `clef.retry_pass` is `{}` when there are none;
  its keys are the attempt numbers as strings.
- `shadow`: one entry per `shadow/<id>/<attempt>-<k>/` dir, sorted by attempt then k.
  `status` is `shadow.status`, `verdict` is parsed from that dir's `qa.last`; fields from
  `shadow.json` that are missing are `null`.
- Text fields are `null` when the file is missing.

## Status vocabulary for the page and the graph

Both the page and the graph mark a task by its status with the CSS class
`st-<status in lowercase>`, e.g. `st-merged`, `st-running`, `st-pending`. `style.css`
(owned by the page) defines, for every status above plus `pending`, a custom property
`--st` holding that status's color, e.g. `.st-merged { --st: #2f9e44; }`. `graph.js`
never hard-codes status colors: it draws with `var(--st, #888)`.

Suggested meaning: merged and pass green, running blue, pending grey, fail and error red,
infra and timeout orange, blocked, interrupted, invalidated and unknown muted purple.

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
