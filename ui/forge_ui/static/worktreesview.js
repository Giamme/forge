/* forge-ui Worktrees screen (#125, removals #147): every worktree forge created, in any repository, with its state
   and the command that removes it (window.ForgeWorktrees, and a CommonJS module under node:test). Route #/worktrees,
   optionally narrowed to one repository (#/worktrees/repo/<root>) or one run (#/worktrees/runs/<key>).
   With write controls the page removes worktrees through POST /api/worktrees/remove (`forge worktrees rm`): a dry run,
   then a confirmation, then the real call. Under read-only, or with no token, it only copies the commands.
   Paths, branches, reasons and commit subjects enter the page as text.
   The pure helpers (filters, totals, sizes, which rows can be removed) are tested in ui/tests/js/worktreesview.test.js.
   ctx = { filter?: {repo, run}, runHref(key), tablePrefs?(id), saveTablePrefs?(id, prefs) }. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeWorktrees = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const POLL_MS = 60000, SIZE_POLL_MS = 3000;
  const STATUSES = ['in_use', 'dirty', 'locked', 'needed', 'failed', 'finished', 'missing', 'foreign', 'unknown', 'removed'];
  const LABEL = { in_use: 'in use', dirty: 'uncommitted changes', locked: 'locked', needed: 'needed by the run', failed: 'failed task',
    finished: 'finished', missing: 'missing on disk', foreign: "not forge's", unknown: 'unknown to git', removed: 'removed' };
  const LAMP = { in_use: 'st-running', dirty: 'fnd-amber', locked: 'fnd-amber', needed: 'st-pending', failed: 'st-fail', finished: 'st-merged',
    missing: 'st-unknown', foreign: 'lamp-off', unknown: 'st-unknown', removed: 'lamp-off' };
  const KINDS = ['task', 'integration', 'candidate', 'attribute'];

  /* ---------- pure ---------- */
  // filter = { repo: '' | root, run: '' | key, status: 'all' | 'disk' (everything not removed) | one of STATUSES, kind: 'all' | KINDS | 'none' }
  function filterRows(rows, filter) {
    const f = filter || {}, status = f.status || 'all', kind = f.kind || 'all';
    return list(rows).filter(r => (!f.repo || r.repo === f.repo) && (!f.run || r.run === f.run)
      && (status === 'all' || (status === 'disk' ? r.status !== 'removed' : r.status === status))
      && (kind === 'all' || (kind === 'none' ? !r.kind : r.kind === kind)));
  }
  // {count, onDisk, bytes, pending} of the rows shown: bytes counts only the sizes known so far
  function totals(rows) {
    let onDisk = 0, bytes = 0, pending = 0;
    for (const r of list(rows)) {
      if (r.status === 'removed' || r.status === 'missing') continue;
      onDisk += 1;
      if (Number.isFinite(r.size)) bytes += r.size; else pending += 1;
    }
    return { count: list(rows).length, onDisk, bytes, pending };
  }
  function humanBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i === 0 ? String(v) : v.toFixed(v < 10 ? 1 : 0)) + ' ' + units[i];
  }
  // the part of a worktree path from `.forge-worktrees` on: the folder above it is the repository's, shown beside it
  const shortPath = p => { const s = String(p || ''), i = s.lastIndexOf('/.forge-worktrees/'); return i >= 0 ? s.slice(i + 1) : s || '—'; };
  const repoName = p => typeof p === 'string' && p ? p.replace(/\/+$/, '').split('/').pop() : '—';
  const label = status => LABEL[status] || status || '—';

  // A removal is the command the server offers: failed or finished, or an unknown folder git has already dropped.
  const BLOCK = {
    in_use: 'its run is active',
    dirty: 'uncommitted changes',
    locked: 'locked by git',
    needed: 'its run still needs it',
    missing: 'already gone (prune)',
    foreign: "not forge's",
    removed: 'already removed',
    unknown: 'not a leftover',
  };
  function removable(row) {
    if (!row || typeof row !== 'object') return false;
    if (row.status === 'failed' || row.status === 'finished') return true;
    return row.status === 'unknown' && row.leftover === true;
  }
  function blockReason(row) {
    if (!row || typeof row !== 'object' || removable(row)) return null;
    if (Object.prototype.hasOwnProperty.call(BLOCK, row.status)) return BLOCK[row.status];
    return row.status ? String(row.status) : 'cannot remove';
  }
  // Paths of the removable rows in the current filtered view, in that view's order.
  function selectable(rows, filter) {
    return filterRows(rows, filter).filter(removable).map(r => r.path).filter(p => typeof p === 'string');
  }
  function answerRows(answer) {
    return list(answer && answer.rows).filter(r => r && typeof r === 'object');
  }
  function rowBit(r) {
    const path = r.path == null ? '' : String(r.path);
    const reason = r.reason == null || r.reason === '' ? '' : String(r.reason);
    return reason ? path + ' (' + reason + ')' : path;
  }
  // One sentence for the dry run: how many would go, then each skip (and any failure) with its reason.
  function summaryText(answer) {
    const rows = answerRows(answer);
    const going = rows.filter(r => r.status === 'would-remove'), n = going.length;
    const branches = going.filter(r => r.action === 'remove+branch').length, folders = going.filter(r => r.action === 'delete-folder').length;
    const plural = (count, one, many) => count + ' ' + (count === 1 ? one : many);
    const extra = [branches && plural(branches, 'merged branch deleted', 'merged branches deleted'),
      folders && plural(folders, 'leftover folder deleted', 'leftover folders deleted')].filter(Boolean);
    let text = plural(n, 'worktree', 'worktrees') + ' would be removed' + (extra.length ? ' (' + extra.join(', ') + ')' : '') + '.';
    const skipped = rows.filter(r => r.status === 'skipped');
    const failed = rows.filter(r => r.status === 'failed');
    if (skipped.length) text += ' Skipped: ' + skipped.map(rowBit).join('; ') + '.';
    if (failed.length) text += ' Failed: ' + failed.map(rowBit).join('; ') + '.';
    return text;
  }
  function resultLines(answer) {
    return answerRows(answer).map(r => ({
      path: r.path == null ? '' : String(r.path),
      status: r.status == null ? '' : String(r.status),
      reason: r.reason == null ? '' : String(r.reason),
    }));
  }
  // The line shown for one result row: "removed", "skipped (reason)", "failed (reason)".
  function resultPhrase(line) {
    const reason = line && line.reason ? ' (' + line.reason + ')' : '';
    if (!line) return '';
    if (line.status === 'removed') return 'removed';
    if (line.status === 'skipped') return 'skipped' + reason;
    if (line.status === 'failed') return 'failed' + reason;
    return (line.status || '') + reason;
  }
  const confirmQuestion = n => n === 1 ? 'Remove 1 worktree?' : 'Remove ' + n + ' worktrees?';

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI;
    const el = (tag, text, cls) => UI.el(tag, text, cls);
    const start = ctx.filter || {};
    const s = { data: null, error: null, filter: { repo: start.repo || '', run: start.run || '', status: 'disk', kind: 'all' }, timer: null, seq: 0, loading: false,
      controls: false, selected: new Set(), branches: false, busy: false };
    // Table roots whose replaceWith remounts the select-all. ForgeTable keeps the root private and swaps it on redraw.
    const armedTables = new WeakSet();
    const LEDE_COPY = 'Every worktree forge created, in any repository it knows, with what state it is in and the command that removes it safely. Commands are only copied: this page runs nothing.';

    const head = el('div', undefined, 'wt-head');
    const refreshKey = el('button', 'Read again'); refreshKey.type = 'button';
    refreshKey.title = 'Run git status and the other reads again now, instead of waiting for the next read';
    refreshKey.addEventListener('click', () => load(true));
    const stamp = el('span', '', 'muted wt-stamp');
    head.append(el('h1', 'Worktrees', 'module-title'), refreshKey, stamp);
    const lede = el('p', LEDE_COPY, 'muted wt-lede');
    const notes = el('div', undefined, 'wt-notes muted');
    const sums = el('p', undefined, 'wt-totals'); sums.setAttribute('role', 'status');
    const filters = el('div', undefined, 'wt-filters');
    const fields = {};
    const field = (name, text) => {
      const l = el('label', undefined, 'fnd-field'), sel = el('select');
      sel.dataset.filter = name;
      sel.addEventListener('change', () => { s.filter[name] = sel.value; draw(); });
      l.append(el('span', text, 'plate'), sel);
      fields[name] = sel; filters.append(l);
    };
    field('repo', 'Repository'); field('status', 'Status'); field('kind', 'Kind');
    const runNote = el('p', undefined, 'wt-run-note'); runNote.hidden = true;
    const tableHost = el('div', undefined, 'wt-table');
    const groups = el('section', undefined, 'wt-runs');
    host.replaceChildren(head, lede, notes, sums, filters, runNote, tableHost, groups);

    function options(sel, items, value) {
      const want = items.map(([v, t]) => v + '\u0000' + t).join('\u0001');
      if (sel.dataset.items !== want) {
        sel.replaceChildren(...items.map(([v, t]) => { const o = el('option', t); o.value = v; return o; }));
        sel.dataset.items = want;
      }
      sel.value = items.some(([v]) => v === value) ? value : items[0][0];
    }
    function syncFilters() {
      const rows = list(s.data && s.data.rows);
      const repos = Array.from(new Set(rows.map(r => r.repo).filter(Boolean).concat(s.filter.repo ? [s.filter.repo] : []))).sort();
      options(fields.repo, [['', 'all']].concat(repos.map(r => [r, repoName(r)])), s.filter.repo);
      const counts = (s.data && s.data.totals && s.data.totals.by_status) || {};
      options(fields.status, [['all', 'all'], ['disk', 'on disk']].concat(STATUSES.map(x => [x, `${label(x)} (${counts[x] || 0})`])), s.filter.status);
      options(fields.kind, [['all', 'all']].concat(KINDS.map(k => [k, k]), [['none', 'none']]), s.filter.kind);
      if (s.filter.run) {
        const group = list(s.data && s.data.runs).find(g => g.key === s.filter.run);
        runNote.replaceChildren(el('span', 'Only the worktrees of run '), el('strong', group ? group.run_id : s.filter.run, 'mono'), el('span', '. '));
        const all = el('a', 'Show every run'); all.href = '#/worktrees';
        runNote.append(all);
        runNote.hidden = false;
      } else runNote.hidden = true;
    }
    function copyButton(command) {
      const b = el('button', 'Copy', 'wt-copy'); b.type = 'button';
      b.setAttribute('aria-label', 'Copy: ' + command);
      b.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); b.textContent = 'Copied'; }
        catch (_) { b.textContent = 'Copy failed: select it'; }
        setTimeout(() => { b.textContent = 'Copy'; }, 2500);
      });
      return b;
    }
    function commandBox(c) {
      const box = el('div', undefined, 'wt-cmd');
      const code = el('code', c.command, 'mono'); code.tabIndex = 0; code.title = c.label;
      box.append(code, copyButton(c.command));
      return box;
    }
    const when = t => Number.isFinite(t) ? new Date(t * 1000).toLocaleString() : '—';
    const statusCell = r => {
      const box = el('span', undefined, 'fnd-status wt-status wt-' + r.status);
      box.append(el('i', undefined, 'lamp ' + (LAMP[r.status] || 'lamp-off')), el('span', label(r.status)));
      if (r.derived) box.append(el('span', 'derived', 'chip wt-derived'));
      if (r.note) box.title = r.note;
      return box;
    };
    const commandCell = r => {
      const box = el('div', undefined, 'wt-cmds');
      for (const c of list(r.commands)) box.append(commandBox(c));
      if (r.note) box.append(el('p', r.note, 'muted wt-note'));
      return box;
    };
    const runCell = r => {
      if (!r.run_id) return '—';
      if (!r.run) return el('span', r.run_id, 'mono');
      const a = el('a', r.run_id, 'mono'); a.href = ctx.runHref ? ctx.runHref(r.run) : '#/runs/' + encodeURIComponent(r.run);
      return a;
    };
    const commitCell = r => {
      if (!r.commit) return r.head ? el('span', String(r.head).slice(0, 12), 'mono') : '—';
      const box = el('span', undefined, 'wt-commit');
      box.title = `${r.commit.sha}\n${when(r.commit.time)}`;
      box.append(el('span', String(r.commit.sha).slice(0, 7), 'mono'), el('span', ' ' + r.commit.subject));
      return box;
    };
    const pathCell = r => { const n = el('span', shortPath(r.path), 'mono'); n.title = r.path; return n; };
    function selectCell(r) {
      const wrap = el('span');
      const box = el('input'); box.type = 'checkbox';
      if (removable(r)) {
        box.checked = s.selected.has(r.path);
        box.disabled = s.busy;
        box._path = r.path;
        box.setAttribute('aria-label', 'Select ' + String(r.path));
        box.addEventListener('change', () => {
          if (box.checked) s.selected.add(r.path); else s.selected.delete(r.path);
          syncSelectAll(); syncBar();
        });
        const go = el('button', 'Remove'); go.type = 'button'; go.disabled = s.busy;
        go.addEventListener('click', () => removePaths([r.path]));
        wrap.append(box, go);
      } else {
        const why = blockReason(r) || 'cannot remove';
        box.disabled = true; box.title = why;
        box.setAttribute('aria-label', why);
        wrap.append(box, el('span', why, 'muted wt-note'));
      }
      return wrap;
    }
    const selectColumn = { id: 'select', label: 'Select', sortable: false, hideable: false, cls: 'wt-command-col', cell: selectCell };
    const columns = [
      { id: 'status', label: 'Status', hideable: false, value: r => STATUSES.indexOf(r.status), cell: statusCell },
      { id: 'run', label: 'Run', value: r => r.run_id, cell: runCell },
      { id: 'task', label: 'Task', value: r => r.task, mono: true, cell: r => r.task || (r.kind && r.kind !== 'task' ? r.kind : '—') },
      { id: 'command', label: 'Command', sortable: false, cls: 'wt-command-col', cell: commandCell },
      { id: 'repo', label: 'Repository', value: r => repoName(r.repo), cell: r => { const n = el('span', repoName(r.repo)); if (r.repo) n.title = r.repo; return n; } },
      { id: 'branch', label: 'Branch', value: r => r.branch, mono: true, cell: r => r.branch || 'detached' },
      { id: 'merged', label: 'Merged', title: 'whether the branch is merged into the repository\'s checked-out branch', value: r => r.merged === null || r.merged === undefined ? null : r.merged ? 1 : 0,
        cell: r => r.merged === true ? 'yes' : r.merged === false ? 'no' : '—' },
      { id: 'commit', label: 'Last commit', value: r => r.commit ? r.commit.time : null, cell: commitCell },
      { id: 'size', label: 'Size', value: r => r.size, cell: r => r.status === 'removed' || r.status === 'missing' ? '—' : Number.isFinite(r.size) ? humanBytes(r.size) : 'measuring…' },
      { id: 'removed', label: 'Removed', value: r => r.removed, cell: r => r.status !== 'removed' ? '—' : Number.isFinite(r.removed) ? when(r.removed) : 'date unknown' },
      { id: 'created', label: 'Created', value: r => r.created, cell: r => when(r.created), hidden: true },
      { id: 'path', label: 'Path', value: r => r.path, cell: pathCell, hidden: true },
      { id: 'kind', label: 'Kind', value: r => r.kind, cell: r => r.kind || '—', hidden: true },
    ];
    function drawNotes() {
      const d = s.data, out = [];
      if (s.error) out.push(el('p', `Could not read the worktrees (${s.error}).`, 'warn'));
      if (d) {
        for (const n of list(d.notes)) out.push(el('p', String(n)));
        if (d.dataset && d.dataset.enabled) {
          out.push(el('p', Number.isFinite(d.dataset.since)
            ? `forge has recorded every worktree it adds or removes since ${when(d.dataset.since)}; removed ones from before then are derived from run dirs and the dataset, without dates.`
            : 'forge records the worktrees it adds and removes from this release on; removed ones until then are derived from run dirs and the dataset, without dates.'));
        }
      }
      notes.replaceChildren(...out);
    }
    function drawTotals(rows) {
      const t = totals(rows), hidden = list(s.data && s.data.rows).length - list(rows).length;
      sums.replaceChildren(el('strong', `${t.onDisk} on disk`), el('span', ` · ${humanBytes(t.bytes)}${t.pending ? ` so far, ${t.pending} still being measured` : ''} · ${t.count - t.onDisk} removed or missing · ${t.count} listed${hidden > 0 ? ` · ${hidden} more hidden by the filters` : ''}`));
    }
    function drawGroups() {
      const items = list(s.data && s.data.runs).filter(g => g.command && (!s.filter.run || g.key === s.filter.run) && (!s.filter.repo || g.repo === s.filter.repo));
      if (!items.length) { groups.replaceChildren(); return; }
      const d = el('details', undefined, 'wt-groups'); d.dataset.keep = 'wt-groups';
      d.append(el('summary', `Whole runs (${items.length})`));
      d.append(el('p', 'forge runs rm removes a run\'s clean worktrees and its branches, and deletes the run directory as well, after archiving it into the dataset. This is a dry run: it changes nothing and says what a real one would do.', 'muted'));
      for (const g of items) {
        const row = el('div', undefined, 'wt-group');
        const a = el('a', g.run_id, 'mono'); a.href = ctx.runHref ? ctx.runHref(g.key) : '#/runs/' + encodeURIComponent(g.key);
        row.append(a, commandBox({ label: 'dry run of deleting the run directory and its worktrees', command: g.command }));
        d.append(row);
      }
      groups.replaceChildren(d);
    }
    function shownPaths() { return selectable(list(s.data && s.data.rows), s.filter); }
    function chosenPaths() { return shownPaths().filter(p => s.selected.has(p)); }
    function pruneSelection() {
      if (!s.data) return;
      const live = new Set(selectable(s.data.rows, { status: 'all', kind: 'all' }));
      for (const p of [...s.selected]) if (!live.has(p)) s.selected.delete(p);
    }
    function syncBar() {
      if (!removeSel) return;
      const n = chosenPaths().length;
      removeSel.textContent = 'Remove selected (' + n + ')';
      removeSel.disabled = n === 0 || s.busy;
      if (branchesBox) branchesBox.disabled = s.busy;
      for (const b of tableHost.querySelectorAll('tbody button')) if (b.textContent === 'Remove') b.disabled = s.busy;
    }
    function syncSelectAll() {
      const box = tableHost.querySelector('th[data-col="select"] input');
      if (!box) return;
      const paths = shownPaths(), n = paths.filter(p => s.selected.has(p)).length;
      box.indeterminate = n > 0 && n < paths.length;
      box.checked = paths.length > 0 && n === paths.length;
      box.disabled = paths.length === 0 || s.busy;
    }
    function mountSelectAll() {
      const text = tableHost.querySelector('th[data-col="select"] .th-text');
      if (!text) return;
      const box = el('input'); box.type = 'checkbox';
      box.setAttribute('aria-label', 'Select all removable worktrees');
      box.addEventListener('dragstart', e => e.preventDefault());
      box.addEventListener('change', () => {
        const paths = shownPaths();
        if (box.checked) for (const p of paths) s.selected.add(p);
        else for (const p of paths) s.selected.delete(p);
        for (const input of tableHost.querySelectorAll('tbody input')) {
          if (typeof input._path === 'string' && paths.includes(input._path)) input.checked = s.selected.has(input._path);
        }
        syncSelectAll(); syncBar();
      });
      text.replaceChildren(box);
      syncSelectAll();
    }
    // Sort, drag, Alt+arrow, the Columns menu and Reset all rebuild the header from `c.label` and swap the table root
    // with replaceWith. Nothing tells us, so the box mounted after render would vanish until the next full draw.
    function armRedraw(node) {
      if (!node || armedTables.has(node) || typeof node.replaceWith !== 'function') return;
      const orig = node.replaceWith;
      armedTables.add(node);
      node.replaceWith = function (...nodes) {
        const out = orig.apply(this, nodes);
        for (const next of nodes) armRedraw(next);
        if (s.controls && host.isConnected) mountSelectAll();
        return out;
      };
    }
    function drawNow() {
      pruneSelection();
      syncFilters(); drawNotes();
      const d = s.data;
      if (!d) { tableHost.replaceChildren(el('p', s.error ? '' : 'Reading every repository\'s worktrees… the first read runs git status in each, so it can take a few seconds.', 'muted')); sums.replaceChildren(); return; }
      const rows = filterRows(d.rows, s.filter);
      drawTotals(rows);
      UI.keepOpen(groups, drawGroups);
      if (!rows.length) {
        tableHost.replaceChildren(el('p', list(d.rows).length ? `No worktrees match these filters (${list(d.rows).length} hidden).` : 'forge has no worktrees on record.', 'muted'));
        syncBar(); return;
      }
      const id = 'worktrees';
      const cols = s.controls ? [selectColumn].concat(columns) : columns;
      tableHost.replaceChildren();
      const table = root.ForgeTable.render(tableHost, { id, label: 'Worktrees', columns: cols, rows, className: 'wt-grid', rowClass: r => 'wt-row-' + r.status,
        // a cell cut short by its width says the whole of itself on hover
        onRow: (_row, tr) => { for (const td of tr.children) if (!td.classList.contains('wt-command-col') && !td.title) td.title = td.textContent; },
        prefs: ctx.tablePrefs ? ctx.tablePrefs(id) : null, savePrefs: p => ctx.saveTablePrefs && ctx.saveTablePrefs(id, p) });
      if (s.controls) { armRedraw(table); mountSelectAll(); syncBar(); }
    }
    // A redraw never pulls a command out from under someone selecting or copying it (#115).
    const draw = () => { if (UI.guard) UI.guard(host, drawNow); else drawNow(); };

    async function load(refresh) {
      if (s.loading && !refresh) return;
      const mine = ++s.seq;
      s.loading = true; refreshKey.disabled = true;
      if (refresh) stamp.textContent = 'reading…';
      const res = await UI.get('/api/worktrees' + (refresh ? '?refresh=1' : ''));
      if (mine !== s.seq) return;
      s.loading = false; refreshKey.disabled = false;
      if (res.ok) { s.data = res.data; s.error = null; stamp.textContent = 'read ' + when(res.data.computed); }
      else { s.error = res.detail || res.error || 'no answer'; stamp.textContent = ''; }
      draw();
      schedule();
    }
    function schedule() {
      clearTimeout(s.timer);
      if (!host.isConnected) return;      // the screen was left: no more reads
      const pending = s.data && s.data.sizes_pending > 0;
      s.timer = setTimeout(() => { if (host.isConnected && document.visibilityState === 'visible') load(false); else schedule(); }, pending ? SIZE_POLL_MS : POLL_MS);
    }

    let branchesBox = null, removeSel = null, outcomeHost = null;
    function showFailure(res) {
      const text = res && res.status === 409 ? 'busy: another removal is running' : UI.outcomeText(res).text;
      const line = el('p', text, 'warn'); line.setAttribute('role', 'status');
      outcomeHost.replaceChildren(line);
    }
    function showLines(lines) {
      const ul = el('ul');
      for (const line of lines) {
        const li = el('li');
        li.append(el('span', line.path, 'mono'), el('span', ' ' + resultPhrase(line)));
        ul.append(li);
      }
      outcomeHost.replaceChildren(ul);
    }
    // Nothing is removed on the first click: dry run, the summary, confirm, then the real call.
    async function removePaths(paths) {
      const wanted = list(paths).filter(p => typeof p === 'string' && p);
      if (s.busy || !wanted.length || !outcomeHost) return;
      const branches = !!s.branches;
      const body = dryRun => ({ paths: wanted.slice(), branches, dry_run: dryRun });
      s.busy = true; syncBar(); syncSelectAll();
      try {
        const dry = await UI.post('/api/worktrees/remove', body(true)) || { ok: false };
        if (!host.isConnected) return;
        if (!dry.ok) { showFailure(dry); return; }
        const data = dry.data || {};
        const n = answerRows(data).filter(r => r.status === 'would-remove').length;
        outcomeHost.replaceChildren(el('p', summaryText(data)));
        // Nothing would be removed: the summary is the answer. Do not ask, and do not send the real call.
        if (n === 0) return;
        const yes = await UI.confirmInline(outcomeHost, confirmQuestion(n), { danger: true });
        if (!yes || !host.isConnected) return;
        const real = await UI.post('/api/worktrees/remove', body(false)) || { ok: false };
        if (!host.isConnected) return;
        if (!real.ok) { showFailure(real); return; }
        const lines = resultLines(real.data);
        showLines(lines);
        for (const line of lines) if (line.status === 'removed') s.selected.delete(line.path);
        s.busy = false;          // the refresh draws the checkboxes; they must not stay disabled
        await load(true);
      } finally {
        s.busy = false;
        if (host.isConnected) { syncBar(); syncSelectAll(); }
      }
    }
    function enableControls() {
      if (s.controls) return;
      const bar = el('div', undefined, 'wt-filters');
      bar.setAttribute('role', 'group'); bar.setAttribute('aria-label', 'Remove worktrees');
      branchesBox = el('input'); branchesBox.type = 'checkbox';
      const branchesLabel = el('label');
      branchesLabel.append(branchesBox, el('span', 'Delete merged branches too'));
      branchesBox.addEventListener('change', () => { s.branches = branchesBox.checked; });
      removeSel = el('button', 'Remove selected (0)'); removeSel.type = 'button'; removeSel.disabled = true;
      removeSel.addEventListener('click', () => removePaths(chosenPaths()));
      bar.append(branchesLabel, removeSel);
      outcomeHost = el('div', undefined, 'wt-notes'); outcomeHost.setAttribute('role', 'status');
      host.insertBefore(bar, tableHost);
      host.insertBefore(outcomeHost, tableHost);
      s.controls = true;
      lede.textContent = 'Every worktree forge created, in any repository it knows, with what state it is in and the command that removes it safely. A removal runs through forge worktrees rm after a dry run and a confirmation. Commands can still be copied.';
    }

    drawNow();
    load(false);
    UI.controlsEnabled().then(on => {
      if (!on || !host.isConnected) return;
      enableControls();
      draw();
    });
    return {
      setFilter(f) {
        const repo = (f && f.repo) || '', run = (f && f.run) || '';
        if (repo === s.filter.repo && run === s.filter.run) return;   // the page's own refresh: nothing changed here
        s.filter.repo = repo; s.filter.run = run; draw();
      },
      reload: () => load(true),
    };
  }

  return { render, filterRows, totals, humanBytes, repoName, shortPath, label, removable, blockReason, selectable, summaryText, confirmQuestion, resultLines, STATUSES, LABEL };
});
