/* forge-ui Disk screen (#139): what forge takes on disk in every repository it has runs for (run dirs, worktrees,
   its data home) and the command that takes each part back (window.ForgeStorage, and a CommonJS module under
   node:test). Route #/disk. The same report as `forge du --all`, from the same rules (scripts/forge_storage.py).
   The commands come from the server. Under --read-only they are only ever copied. When controls are on, a
   reclaimable run (and every reclaimable run at once) can be compacted from here, after a dry run and a
   confirmation. Measuring visits every file, so it runs once in the background when the screen opens and
   again on "Measure again"; the page polls only while a measurement runs.
   The pure helpers are tested in ui/tests/js/storageview.test.js.
   ctx = { runHref(key) -> href (a run row's key is null when the run list does not show it), tablePrefs?(id), saveTablePrefs?(id, prefs) }. */
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
  // A copied command is a compact only when it is `forge runs compact`. `--all` is the whole disk; anything
  // else names one run. `forge du --all` and `forge runs rm` are not compacts.
  function compactKind(command) {
    if (typeof command !== 'string' || !command.includes('forge runs compact')) return null;
    return /(?:^|\s)--all(?:\s|$)/.test(command) ? 'all' : 'run';
  }
  // The compact POST body, without dry_run (the caller adds it). `all` compacts every reclaimable run;
  // a string or {run}|{key} compacts that run.
  function compactBody(target) {
    if (target === 'all' || (target && typeof target === 'object' && target.all === true)) return { all: true };
    if (typeof target === 'string') return { run: target };
    const raw = target && typeof target === 'object' ? (target.run != null ? target.run : target.key) : null;
    return { run: raw == null ? '' : String(raw) };
  }
  function compactPayload(answer) {
    const data = answer && answer.data;
    if (data && typeof data === 'object' && !Array.isArray(data)
      && ('bytes_freed' in data || 'runs' in data || 'dry_run' in data)) return data;
    return answer && typeof answer === 'object' ? answer : {};
  }
  // What the confirm and the result say: the bytes `forge runs compact --json` would free, and how many runs.
  function compactSummary(answer) {
    const body = compactPayload(answer);
    const bytes = typeof body.bytes_freed === 'number' && Number.isFinite(body.bytes_freed) ? body.bytes_freed : 0;
    const n = Array.isArray(body.runs) ? body.runs.length : 0;
    const runs = n === 1 ? '1 run' : n + ' runs';
    return (body.dry_run ? 'Would free ' : 'Freed ') + humanBytes(bytes) + ' across ' + runs;
  }
  const ACTIVE_RUN = 'this run is active: compact it after it ends';
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
  // The chart's parts, in a fixed order that fixes their colours (--disk-1 … --disk-6): a part keeps its colour
  // whatever else is on screen.
  const PARTS = [
    { key: 'snapshots', label: 'review snapshots', note: 'can go: nothing reads them', color: 'var(--disk-1)' },
    { key: 'logs', label: 'logs', note: 'gzipping the log copies takes most of it back', color: 'var(--disk-2)' },
    { key: 'other', label: 'other run files', note: 'prompts, diffs, verdicts: what forge-ui shows', color: 'var(--disk-3)' },
    { key: 'strays', label: "not forge's", note: 'found inside run dirs; forge never removes them', color: 'var(--disk-4)' },
    { key: 'worktrees', label: 'worktrees', note: 'see the Worktrees screen', color: 'var(--disk-5)' },
    { key: 'home', label: 'data home', note: 'the dataset (kept), archives, exports', color: 'var(--disk-6)' },
  ];
  // [{key, label, note, color, bytes, share}] of everything forge takes, in PARTS order; empty parts stay (0 bytes).
  function breakdown(data) {
    const t = (data && data.totals) || {}, runs = list(data && data.runs);
    const sum = f => runs.reduce((n, r) => n + (Number(f(r)) || 0), 0);
    const bytes = { snapshots: sum(r => r.snapshots), logs: sum(r => (r.logs || 0) + (r.gz || 0)), other: sum(r => r.other || 0),
      strays: sum(r => r.strays), worktrees: t.worktrees || 0, home: (data && data.data_home && data.data_home.bytes) || 0 };
    const total = Object.values(bytes).reduce((a, b) => a + b, 0);
    return PARTS.map(p => ({ ...p, bytes: bytes[p.key], share: total ? bytes[p.key] / total : 0 }));
  }
  // [{repo, name, total, parts: {key: bytes}}] largest first: each repository's run dirs and worktree folders.
  function byRepo(data) {
    const out = new Map();
    const at = repo => {
      const k = repo || '';
      if (!out.has(k)) out.set(k, { repo: repo || null, name: repo ? repoName(repo) : 'unknown repository', total: 0,
        parts: { snapshots: 0, logs: 0, other: 0, strays: 0, worktrees: 0 } });
      return out.get(k);
    };
    const add = (g, key, n) => { n = Number(n) || 0; g.parts[key] += n; g.total += n; };
    for (const r of list(data && data.runs)) {
      const g = at(r.repo);
      add(g, 'snapshots', r.snapshots); add(g, 'logs', (r.logs || 0) + (r.gz || 0)); add(g, 'other', r.other); add(g, 'strays', r.strays);
    }
    for (const w of list(data && data.worktrees)) add(at(w.repo), 'worktrees', w.bytes);
    return Array.from(out.values()).filter(g => g.total > 0).sort((a, b) => b.total - a.total);
  }
  const pct = x => x >= 0.995 ? '100%' : x >= 0.1 ? Math.round(x * 100) + '%' : x > 0 ? (x * 100 < 1 ? '<1%' : (x * 100).toFixed(0) + '%') : '0%';

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI;
    const el = (tag, text, cls) => UI.el(tag, text, cls);
    const s = { answer: null, error: null, timer: null, seq: 0, writes: false, compacting: false };

    const head = el('div', undefined, 'wt-head');
    const again = el('button', 'Measure again'); again.type = 'button';
    again.title = 'Visit every file again now; it takes a while on large run dirs';
    again.addEventListener('click', () => load(true));
    const stamp = el('span', '', 'muted wt-stamp');
    head.append(el('h1', 'Disk', 'module-title'), again, stamp);
    const LEDE_OFF = 'What forge takes on disk in every repository it has runs for, and the command that takes each part back. Review snapshots and log copies can go without losing a run: every run stays viewable here and in the dataset. Commands are only copied: this page runs nothing.';
    const LEDE_ON = 'What forge takes on disk in every repository it has runs for, and the command that takes each part back. Review snapshots and log copies can go without losing a run: every run stays viewable here and in the dataset. Copy a command, or compact from here after a dry run and a confirmation.';
    const lede = el('p', LEDE_OFF, 'muted wt-lede');
    const status = el('p', undefined, 'wt-totals'); status.setAttribute('role', 'status');
    const cmds = el('div', undefined, 'disk-commands');
    const action = el('div');
    const chart = el('section', undefined, 'disk-chart');
    chart.setAttribute('aria-label', 'Where the space goes');
    const tip = el('div', undefined, 'disk-tip'); tip.hidden = true; tip.setAttribute('role', 'tooltip');
    const tableHost = el('div', undefined, 'wt-table');
    const extra = el('section', undefined, 'disk-extra');
    host.replaceChildren(head, lede, status, chart, cmds, action, tableHost, extra, tip);

    // one tooltip for every segment: what it is, its size and its share, beside the pointer or the focused segment
    function showTip(seg, x, y) {
      const p = seg._part;
      tip.replaceChildren(el('strong', p.label), el('span', ` ${humanBytes(p.bytes)} · ${pct(p.share)}${seg._of ? ' of ' + seg._of : ''}`), el('span', p.note, 'muted'));
      tip.hidden = false;
      const box = host.getBoundingClientRect(), w = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(x - box.left + 12, box.width - w)) + 'px';
      tip.style.top = (y - box.top + 16) + 'px';
    }
    function segment(part, width, of) {
      const seg = el('span', undefined, 'disk-seg');
      seg._part = part; seg._of = of;
      seg.style.flexGrow = String(width); seg.style.background = part.color;
      seg.tabIndex = 0;
      seg.setAttribute('aria-label', `${part.label}: ${humanBytes(part.bytes)}, ${pct(part.share)}${of ? ' of ' + of : ''}`);
      seg.addEventListener('pointermove', e => showTip(seg, e.clientX, e.clientY));
      seg.addEventListener('pointerleave', () => { tip.hidden = true; });
      seg.addEventListener('focus', () => { const r = seg.getBoundingClientRect(); showTip(seg, r.left + r.width / 2, r.bottom - 8); });
      seg.addEventListener('blur', () => { tip.hidden = true; });
      return seg;
    }
    function drawChart(d) {
      const parts = breakdown(d), total = parts.reduce((n, p) => n + p.bytes, 0);
      if (!total) { chart.replaceChildren(); return; }
      const whole = el('div', undefined, 'disk-bar disk-bar-whole');
      for (const p of parts) if (p.bytes > 0) whole.append(segment(p, p.bytes));
      const legend = el('ul', undefined, 'disk-legend');
      for (const p of parts) {
        if (!p.bytes) continue;
        const li = el('li'); const sw = el('i', undefined, 'disk-swatch'); sw.style.background = p.color;
        li.append(sw, el('span', p.label), el('strong', humanBytes(p.bytes)), el('span', pct(p.share), 'muted'));
        li.title = p.note; legend.append(li);
      }
      const repos = byRepo(d).slice(0, 10), top = repos.length ? repos[0].total : 0;
      const rows = el('div', undefined, 'disk-repos');
      for (const g of repos) {
        const row = el('div', undefined, 'disk-repo');
        const name = el('span', g.name, 'disk-repo-name'); if (g.repo) name.title = g.repo;
        const track = el('div', undefined, 'disk-track');
        const bar = el('div', undefined, 'disk-bar'); bar.style.width = (100 * g.total / top) + '%';
        for (const p of parts) {
          const n = g.parts[p.key] || 0;
          if (n > 0) bar.append(segment({ ...p, bytes: n, share: n / g.total }, n, g.name));
        }
        track.append(bar);
        row.append(name, track, el('span', humanBytes(g.total), 'disk-repo-size'));
        rows.append(row);
      }
      const parts2 = [el('h2', 'Where the space goes', 'plate'), whole, legend];
      if (repos.length) parts2.push(el('h2', 'By repository' + (byRepo(d).length > repos.length ? ' (the 10 largest)' : ''), 'plate disk-sub'), rows);
      chart.replaceChildren(...parts2);
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
    function commandBox(c, key) {
      const box = el('div', undefined, 'wt-cmd');
      const code = el('code', c.command, 'mono'); code.tabIndex = 0; code.title = c.label;
      box.append(code, copyButton(c.command));
      const kind = compactKind(c && c.command);
      if (s.writes && kind === 'all') box.append(compactButton('Compact all', { all: true }));
      else if (s.writes && kind === 'run' && key) box.append(compactButton('Compact', { run: key }));
      return box;
    }
    function compactButton(text, target) {
      const b = el('button', text, 'wt-copy ui-danger'); b.type = 'button';
      b.addEventListener('click', () => { runCompact(target); });
      return b;
    }
    function refusedActive(res) { return !!(res && res.status === 409 && res.error !== 'busy'); }
    async function runCompact(target) {
      if (s.compacting) return;
      s.compacting = true;
      for (const child of [...action.children]) child.remove();
      const path = '/api/storage/compact';
      const base = compactBody(target);
      try {
        const preview = await UI.post(path, { ...base, dry_run: true });
        if (!action.isConnected) return;
        if (refusedActive(preview)) {
          UI.outcome(action, { ok: false, status: 409, error: preview.error || 'refused', detail: ACTIVE_RUN });
          return;
        }
        if (!preview || !preview.ok || (preview.data && preview.data.ok === false)) {
          UI.outcome(action, preview && preview.ok ? { ok: false, status: preview.status || 500, error: 'failed', detail: 'Could not preview what compact would free' } : preview);
          return;
        }
        const yes = await UI.confirmInline(action, compactSummary(preview), { danger: true });
        if (!action.isConnected || !yes) return;
        const res = await UI.post(path, { ...base, dry_run: false });
        if (!action.isConnected) return;
        if (refusedActive(res)) {
          UI.outcome(action, { ok: false, status: 409, error: res.error || 'refused', detail: ACTIVE_RUN });
          return;
        }
        if (!res || !res.ok) { UI.outcome(action, res); return; }
        UI.outcome(action, res, compactSummary(res));
        await load(true);
      } finally {
        s.compacting = false;
      }
    }
    const when = t => Number.isFinite(t) ? new Date(t * 1000).toLocaleString() : '—';
    const bytesCell = n => Number.isFinite(n) && n > 0 ? humanBytes(n) : '—';
    const columns = [
      { id: 'run', label: 'Run', value: r => r.run_id, cell: r => {
        const href = r.key && ctx.runHref ? ctx.runHref(r.key) : null;
        const n = el(href ? 'a' : 'span', r.run_id, 'mono'); if (href) n.href = href; n.title = r.path; return n; } },
      { id: 'repo', label: 'Repository', value: r => repoName(r.repo), cell: r => { const n = el('span', repoName(r.repo)); if (r.repo) n.title = r.repo; return n; } },
      { id: 'total', label: 'Total', value: r => r.total, cell: r => humanBytes(r.total) },
      { id: 'snapshots', label: 'Snapshots', title: 'review snapshots: nothing reads one once its review ended', value: r => r.snapshots, cell: r => bytesCell(r.snapshots) },
      { id: 'logs', label: 'Logs', value: r => r.logs + r.gz, cell: r => bytesCell(r.logs + r.gz) },
      { id: 'strays', label: "Not forge's", title: 'a dependency install or a .git found inside the run dir: forge never removes these', value: r => r.strays, cell: r => bytesCell(r.strays) },
      { id: 'reclaimable', label: 'Can go', title: 'what forge runs compact --logs would take back (logs estimated)', value: r => r.reclaimable, cell: r => bytesCell(r.reclaimable) },
      { id: 'command', label: 'Command', sortable: false, cls: 'wt-command-col', cell: r => { const box = el('div', undefined, 'wt-cmds'); for (const c of list(r.commands)) box.append(commandBox(c, r.key)); return box; } },
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
      lede.textContent = s.writes ? LEDE_ON : LEDE_OFF;
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
      if (!d) { chart.replaceChildren(); cmds.replaceChildren(); tableHost.replaceChildren(); extra.replaceChildren(); return; }
      drawChart(d);
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
    UI.controlsEnabled().then(on => {
      const next = !!on;
      if (s.writes === next) return;
      s.writes = next;
      draw();
    });

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

  return { render, humanBytes, summary, homeParts, repoName, HOME_LABEL, PARTS, breakdown, byRepo, pct, compactBody, compactSummary };
});
