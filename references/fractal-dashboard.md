# Forge run inspector: approved design brief

The dashboard and portable HTML report inspect one Fractal run at a time. The interface is dark-only, code-first and read-only. Fractal execution, Forge acceptance, verification and QA remain separate statuses; execution completion does not imply acceptance.

## Workspace

- A compact header identifies the run. The left pane holds searchable task and node rows with goal excerpts, status, model and parent/child nesting. Search retains ancestors of matching nodes.
- The right pane opens on the run overview, with progress and problems first. Task views are Summary, QA and History. Node views are Overview, Decomposition, Logs and Changes. Configuration and copyable terminal commands are secondary run views.
- Decomposition decisions show decision, reason, phase, planner, attempts, bounds and admitted children as fields. Logs are selected by step and file. Candidate changes are shown as line-oriented diffs, and raw text can be downloaded.
- Run, task, node and view live in the URL. The runs index is reachable even when the server was launched with a run ID. Browser Back and Forward restore selection.
- At narrow widths, the navigator and details become separate screens with an explicit back action. Keyboard focus is visible and status uses text as well as color. Reduced motion is respected.

## Refresh and data

Progress is requested about every two seconds. The page keeps its header, navigator, detail pane, selected item, tab, search, expanded rows, focus and scroll positions mounted while fields and rows reconcile by stable identifiers. It does not refocus a control during a refresh. A resource has at most one request in flight; late replies from a previous navigation are ignored.

The last successful view stays visible if a request fails. The status says stale/reconnecting and offers Retry. “Pause live updates” stops browser polling and is distinct from pausing run execution. Log follow starts off, turns off when the reader scrolls away, and can be restored with “Jump to latest.”

The live `data` endpoint supports `scope=progress`, `scope=runs`, and `scope=artifact` with a selected task and optional node. Artifacts are `task`, `node`, `qa`, `history`, `logs` and `changes`; history also takes `table=events|steps|messages` and a zero-based `offset`. An unscoped `data` request retains the original full projection for compatibility. Heavy files and ledger pages are fetched only for the selected view. The CLI capture schema and commands remain available.

The Python renderer inlines the separate CSS and JavaScript assets into each report. Portable reports retain the complete captured history and logs, escape artifact text and open from `file:` without network access. The loopback host check, ephemeral token, read-only HTTP surface and managed-file boundary remain in force. No execution engine, run-state schema or browser execution control is part of this design.
