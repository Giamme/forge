/* forge-ui Disk screen (#139): what forge takes on disk in every repository it has runs for (run dirs, worktrees,
   its data home) and the command that takes each part back (window.ForgeStorage, and a CommonJS module under
   node:test). Route #/disk. The same report as `forge du --all`, from the same rules (scripts/forge_storage.py).
   The commands come from the server and are only ever copied: the page runs nothing, so it works with
   --read-only too. Measuring visits every file, so it runs once in the background when the screen opens and
   again on "Measure again"; the page polls only while a measurement runs.
   The pure helpers are tested in ui/tests/js/storageview.test.js.
   ctx = { runHref(path) -> href | null, tablePrefs?(id), saveTablePrefs?(id, prefs) }. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeStorage = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const POLL_MS = 2000;

  /* ---------- pure ---------- */
  function humanBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i === 0 ? String(v) : v.toFixed(v < 10 ? 1 : 0)) + ' ' + units[i];
  }
  // One line on the whole: what forge takes and what can go without losing a run.
  function summary(data) {
    const t = (data && data.totals) || {};
    const parts = [`${humanBytes(t.runs || 0)} in ${list(data && data.runs).length} run dirs`,
      `${humanBytes(t.worktrees || 0)} in ${list(data && data.worktrees).length} worktree folders`];
    if (data && data.data_home) parts.push(`${humanBytes(t.data_home || 0)} in the data home`);
    return { total: (t.runs || 0) + (t.worktrees || 0) + (t.data_home || 0), text: parts.join(' · '), reclaimable: t.reclaimable || 0 };
  }
  // The data home's parts, largest first, with what each is for.
  const HOME_LABEL = { dataset: 'dataset (kept: what forge learns from)', blobs: 'dataset blobs (kept)', archive: 'archives of deleted runs (forge runs rm)',
    exports: 'exports (forge dataset export writes them again)', legacy: 'legacy files from older releases', storage: 'storage records', other: 'other' };
  function homeParts(home) {
    const p = (home && home.parts) || {};
    return Object.keys(HOME_LABEL).filter(k => p[k] > 0).map(k => ({ key: k, label: HOME_LABEL[k], bytes: p[k] })).sort((a, b) => b.bytes - a.bytes);
  }
  const repoName = p => typeof p === 'string' && p ? p.replace(/\/+$/, '').split('/').pop() : '—';

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI;
    const el = (tag, text, cls) => UI.el(tag, text, cls);
    const s = { answer: null, error: null, timer: null, seq: 0 };

    const head = el('div', undefined, 'wt-head');
    const again = el('button', 'Measure again'); again.type = 'button';
    again.title = 'Visit every file again now; it takes a while on large run dirs';
    again.addEventListener('click', () => load(true));
    const stamp = el('span', '', 'muted wt-stamp');
    head.append(el('h1', 'Disk', 'module-title'), again, stamp);
    const lede = el('p', 'What forge takes on disk in every repository it has runs for, and the command that takes each part back. Review snapshots and log copies can go without losing a run: every run stays viewable here and in the dataset. Commands are only copied: this page runs nothing.', 'muted wt-lede');
    const status = el('p', undefined, 'wt-totals'); status.setAttribute('role', 'status');
    const cmds = el('div', undefined, 'disk-commands');
    const tableHost = el('div', undefined, 'wt-table');
    const extra = el('section', undefined, 'disk-extra');
    host.replaceChildren(head, lede, status, cmds, tableHost, extra);

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
    const bytesCell = n => Number.isFinite(n) && n > 0 ? humanBytes(n) : '—';
    const columns = [
      { id: 'run', label: 'Run', value: r => r.run_id, cell: r => {
        const href = ctx.runHref ? ctx.runHref(r.path) : null;
        const n = el(href ? 'a' : 'span', r.run_id, 'mono'); if (href) n.href = href; n.title = r.path; return n; } },
      { id: 'repo', label: 'Repository', value: r => repoName(r.repo), cell: r => { const n = el('span', repoName(r.repo)); if (r.repo) n.title = r.repo; return n; } },
      { id: 'total', label: 'Total', value: r => r.total, cell: r => humanBytes(r.total) },
      { id: 'snapshots', label: 'Snapshots', title: 'review snapshots: nothing reads one once its review ended', value: r => r.snapshots, cell: r => bytesCell(r.snapshots) },
      { id: 'logs', label: 'Logs', value: r => r.logs + r.gz, cell: r => bytesCell(r.logs + r.gz) },
      { id: 'strays', label: "Not forge's", title: 'a dependency install or a .git found inside the run dir: forge never removes these', value: r => r.strays, cell: r => bytesCell(r.strays) },
      { id: 'reclaimable', label: 'Can go', title: 'what forge runs compact --logs would take back (logs estimated)', value: r => r.reclaimable, cell: r => bytesCell(r.reclaimable) },
      { id: 'command', label: 'Command', sortable: false, cls: 'wt-command-col', cell: r => { const box = el('div', undefined, 'wt-cmds'); for (const c of list(r.commands)) box.append(commandBox(c)); return box; } },
      { id: 'updated', label: 'Changed', value: r => r.updated, cell: r => when(r.updated) },
      { id: 'path', label: 'Path', value: r => r.path, cell: r => el('span', r.path, 'mono'), hidden: true },
    ];
    function drawExtra(d) {
      const out = [];
      if (list(d.strays).length) {
        const box = el('div', undefined, 'disk-block');
        box.append(el('h2', "Not forge's, found inside run dirs", 'plate'), el('p', 'forge never removes these. Delete them by hand if nothing needs them.', 'muted'));
        const ul = el('ul', undefined, 'disk-list');
        for (const x of list(d.strays)) { const li = el('li'); li.append(el('strong', humanBytes(x.bytes)), el('span', ' '), el('span', x.path, 'mono')); ul.append(li); }
        box.append(ul); out.push(box);
      }
      const wt = el('div', undefined, 'disk-block');
      wt.append(el('h2', 'Worktrees', 'plate'));
      const line = el('p');
      line.append(el('span', `${humanBytes((d.totals || {}).worktrees || 0)} in ${list(d.worktrees).length} worktree folders. `));
      const a = el('a', 'Open the Worktrees screen'); a.href = '#/worktrees';
      line.append(a, el('span', ' for the state of each one and the command that removes it safely.'));
      wt.append(line);
      if (list(d.worktrees).length) {
        const ul = el('ul', undefined, 'disk-list');
        for (const w of list(d.worktrees).slice(0, 8)) { const li = el('li'); li.append(el('strong', humanBytes(w.bytes)), el('span', ' '), el('span', w.path, 'mono')); ul.append(li); }
        wt.append(ul);
      }
      out.push(wt);
      if (d.data_home) {
        const box = el('div', undefined, 'disk-block');
        box.append(el('h2', 'Data home', 'plate'));
        const p = el('p'); p.append(el('span', humanBytes(d.data_home.bytes) + ' in '), el('span', d.data_home.path, 'mono')); box.append(p);
        const ul = el('ul', undefined, 'disk-list');
        for (const x of homeParts(d.data_home)) { const li = el('li'); li.append(el('strong', humanBytes(x.bytes)), el('span', ' ' + x.label)); ul.append(li); }
        box.append(ul); out.push(box);
      }
      extra.replaceChildren(...out);
    }
    function drawNow() {
      const a = s.answer, d = a && a.data;
      if (s.error) status.replaceChildren(el('span', `Could not read the disk report (${s.error}).`, 'warn'));
      else if (!a) status.replaceChildren(el('span', 'Asking…', 'muted'));
      else if (a.state === 'unavailable' || (a.state === 'error' && !d)) status.replaceChildren(el('span', `Could not measure: ${a.error || 'unknown error'}.`, 'warn'));
      else if (a.state === 'measuring' && !d) {
        const p = a.progress;
        status.replaceChildren(el('span', p && p.of ? `Measuring run dir ${p.done + 1} of ${p.of}… every file is visited, so a large one takes a while.` : 'Measuring… every file is visited, so this takes a while.', 'muted'));
      } else if (d) {
        const sum = summary(d);
        const parts = [el('strong', humanBytes(sum.total)), el('span', ' · ' + sum.text)];
        if (sum.reclaimable) parts.push(el('span', ` · about ${humanBytes(sum.reclaimable)} can go without losing a run`));
        if (a.state === 'measuring') parts.push(el('span', ' · measuring again…', 'muted'));
        if (!d.complete) parts.push(el('span', ' · measuring stopped at its time limit: some sizes are lower bounds', 'muted'));
        status.replaceChildren(...parts);
      }
      if (!d) { cmds.replaceChildren(); tableHost.replaceChildren(); extra.replaceChildren(); return; }
      stamp.textContent = 'measured ' + when(d.computed);
      cmds.replaceChildren(...list(d.commands).map(commandBox));
      if (!list(d.runs).length) tableHost.replaceChildren(el('p', 'forge has no registered runs.', 'muted'));
      else {
        tableHost.replaceChildren();
        root.ForgeTable.render(tableHost, { id: 'disk', label: 'Run dirs', columns, rows: list(d.runs), className: 'wt-grid',
          onRow: (_row, tr) => { for (const td of tr.children) if (!td.classList.contains('wt-command-col') && !td.title) td.title = td.textContent; },
          prefs: ctx.tablePrefs ? ctx.tablePrefs('disk') : null, savePrefs: p => ctx.saveTablePrefs && ctx.saveTablePrefs('disk', p) });
      }
      drawExtra(d);
    }
    // A redraw never pulls a command out from under someone selecting or copying it (#115).
    const draw = () => { if (UI.guard) UI.guard(host, drawNow); else drawNow(); };

    async function load(refresh) {
      const mine = ++s.seq;
      again.disabled = true;
      const res = await UI.get('/api/storage' + (refresh ? '?refresh=1' : ''));
      if (mine !== s.seq) return;
      if (res.ok) { s.answer = res.data; s.error = null; } else s.error = res.detail || res.error || 'no answer';
      again.disabled = !!(s.answer && s.answer.state === 'measuring');
      draw();
      clearTimeout(s.timer);
      if (host.isConnected && s.answer && s.answer.state === 'measuring')
        s.timer = setTimeout(() => { if (host.isConnected) load(false); }, POLL_MS);
    }
    drawNow();
    load(false);
    return { reload: () => load(true) };
  }

  return { render, humanBytes, summary, homeParts, repoName, HOME_LABEL };
});
