/* forge-ui Projects screen (#126): one page per repository with its runs, every finding forge wrote about it and
   every memory forge wrote for it (window.ForgeProjects, and a CommonJS module under node:test).
   Routes: #/projects (the list) and #/projects/<id>[/runs|/findings|/memories]. The id is the run cloud's.
   Each tab reads its own endpoint when it opens, never on the page's poll; "Read again" asks the server to read
   everything again. A redraw keeps filters, scroll and a selection (#115). Finding and memory texts are untrusted
   and only ever enter the page as text. The pure helpers are tested in ui/tests/js/projectsview.test.js.
   ctx = { id?, tab?, runs() (the page's run list), runHref(key), tablePrefs?(id), saveTablePrefs?(id, prefs) }. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeProjects = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const A = root.ForgeFindingsActions || (typeof require === 'function' ? require('./findingsactions.js') : null);
  const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
  const STATUSES = ['open', 'to-file', 'filed', 'fixed', 'swept', 'dismissed', 'duplicate', 'shadow', 'unlabelled', 'superseded'];
  const LAMPS = { open: 'lamp-off', 'to-file': 'lamp-route', filed: 'fnd-amber', fixed: 'st-pass', swept: 'st-pass', dismissed: 'lamp-off', duplicate: 'lamp-off',
    shadow: 'st-outdated', unlabelled: 'st-unknown', superseded: 'st-outdated' };
  const OPEN = ['open', 'to-file'];   // the filter `open` shows both: a finding marked to file is still open work
  const STATES = { in_memory: 'in memory', waiting: 'waiting for a second run', stale: 'dropped: stale', capped: 'dropped: trimmed by the cap',
    muted: 'not injected (curated out)', dropped: 'dropped', gone: 'from a checkout that is gone' };
  const TABS = ['runs', 'findings', 'memories'];
  const ROLES_NOTE = 'planner and dwarf get Verify, Known traps and Recurring QA findings; QA gets the last two';

  /* ---------- pure ---------- */
  // filter = { severity: 'all' | P0..P3, status: 'all' | 'open' (default: open and to-file) | STATUSES, tag: '' | a tag, run: '' | run id,
  //            reviewer: '', path: '' (substring), since: 0 (days, 0 = any), shadow: false }
  function filterFindings(rows, filter, now) {
    const f = filter || {}, sev = f.severity || 'all', status = f.status || 'open', path = String(f.path || '').toLowerCase();
    const since = Number(f.since) > 0 ? (now || Date.now() / 1000) - Number(f.since) * 86400 : null;
    return list(rows).filter(r => (f.shadow || r.kind !== 'shadow' || status === 'shadow')
      && (sev === 'all' || r.severity === sev)
      && (status === 'all' || (status === 'open' ? OPEN.includes(r.status) : r.status === status))
      && (!f.tag || list(r.tags).includes(f.tag))
      && (!f.run || r.run_id === f.run) && (!f.reviewer || r.reviewer === f.reviewer)
      && (!path || String(r.location || '').toLowerCase().includes(path))
      && (since === null || (Number.isFinite(r.at) && r.at >= since)));
  }
  // A learning's state as the picked checkout's forge-memory.sh decides it: its own ledger's state, else the first
  // other checkout's (with that checkout named), else `gone` for one only the dataset still has.
  function stateFor(learning, source) {
    const states = learning && learning.states || {};
    if (source && states[source]) return { state: states[source], from: null };
    const other = Object.keys(states)[0];
    if (other) return { state: states[other], from: other };
    return { state: 'gone', from: 'dataset' };
  }
  function filterLearnings(rows, filter, source) {
    const f = filter || {}, cat = f.category || 'all', st = f.state || 'all', text = String(f.text || '').toLowerCase();
    return list(rows).filter(r => (cat === 'all' || r.category === cat) && (st === 'all' || stateFor(r, source).state === st)
      && (!text || String(r.text || '').toLowerCase().includes(text)));
  }
  // The forget wire key is the learning's own category and key, joined once. A row missing either is not selectable.
  function learningKey(row) {
    if (!row || row.category == null || row.key == null) return '';
    const category = String(row.category), key = String(row.key);
    return category && key ? category + ':' + key : '';
  }
  // Keys to forget, in learning order. `selected` is a Set or array of learning rows or of "category:key" strings.
  function forgetKeys(learnings, selected) {
    const want = selected instanceof Set ? selected : new Set(list(selected));
    const out = [], seen = new Set();
    for (const row of list(learnings)) {
      const key = learningKey(row);
      if (!key || seen.has(key) || (!want.has(row) && !want.has(key))) continue;
      seen.add(key);
      out.push(key);
    }
    return out;
  }
  function forgetData(res) {
    const body = res && res.data && res.data.checkouts ? res.data : res;
    return body && body.checkouts ? body : null;
  }
  // The confirm sentence: the chosen learnings, then the memory.md lines the dry run says go. All of it is text.
  function forgetConfirmText(learnings, keys, res) {
    const byKey = new Map();
    for (const row of list(learnings)) { const key = learningKey(row); if (key && !byKey.has(key)) byKey.set(key, row); }
    const lines = ['Forget these learnings?'];
    for (const key of keys) {
      const row = byKey.get(key), text = row && row.text != null ? String(row.text) : '';
      lines.push(text ? key + ' — ' + text : key);
    }
    const removed = [];
    for (const checkout of list(forgetData(res) && forgetData(res).checkouts)) {
      for (const line of list(checkout && checkout.result && checkout.result.memory_lines_removed)) {
        if (line != null && String(line) !== '') removed.push(String(line));
      }
    }
    lines.push(removed.length ? 'memory.md lines that go:' : 'No memory.md line goes.');
    return lines.concat(removed).join('\n');
  }
  // One line per key after the real call: how many ledger rows went, or that the key was not found.
  function forgetResultLines(res) {
    const lines = [];
    for (const checkout of list(forgetData(res) && forgetData(res).checkouts)) {
      const result = checkout && checkout.result;
      if (!result) {
        const err = checkout && checkout.error;
        const why = typeof err === 'string' ? err : err && (err.detail || err.error);
        lines.push(why || 'Could not forget in this checkout');
        continue;
      }
      for (const item of list(result.keys)) {
        if (!item || item.key == null) continue;
        if (!item.found) { lines.push(String(item.key) + ': not found'); continue; }
        const rows = typeof item.rows === 'number' && Number.isFinite(item.rows) ? item.rows : 0;
        lines.push(String(item.key) + ': ' + rows + (rows === 1 ? ' row' : ' rows') + ' removed');
      }
    }
    return lines;
  }
  // what the Memories tab says when there is nothing to show
  function memoryEmpty(data) {
    if (!data) return null;
    if (data.runs > 0 && data.memory_off_runs === data.runs) return 'Every run of this repository ran with memory turned off (--no-memory), so forge recorded no learnings.';
    if (!list(data.learnings).length && !list(data.sources).some(s => s.memory)) return 'No memories yet: forge has recorded no learning for this repository.';
    return null;
  }
  // A summary without what its own columns show: the bullet, the [P1][CONFIRMED] tags and a leading location.
  function cleanSummary(text, location) {
    let s = String(text || '').replace(/^\s*(?:[-*+•]|\d+[.)])\s*/, '').replace(/\*\*/g, '');
    s = s.replace(/^(?:\s*(?:\[[^\]\n]*\]|\((?:P[0-3]|CONFIRMED|PLAUSIBLE|UNSTATED)\)|P[0-3]:?|CONFIRMED|PLAUSIBLE))+\s*/, '');
    if (location) {
      const at = s.indexOf(location);
      if (at >= 0 && at < 4) s = s.slice(at + location.length).replace(/^[`'"*]*\s*(?:\([^)]*\))?\s*(?:[—–:-]+\s*)?/, '');
    }
    return s.trim() || String(text || '').trim();
  }
  const basename = p => typeof p === 'string' && p ? p.replace(/\/+$/, '').split('/').pop() : '—';
  const routeOf = (id, tab) => '#/projects/' + encodeURIComponent(id) + (tab && tab !== 'runs' ? '/' + tab : '');

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI, el = (tag, text, cls) => UI.el(tag, text, cls);
    const when = t => Number.isFinite(t) ? new Date(t * 1000).toLocaleString() : '—';
    const guard = (region, draw) => { if (UI.guard) UI.guard(region, draw); else draw(); };
    const s = { id: ctx.id || null, tab: TABS.includes(ctx.tab) ? ctx.tab : 'runs', list: null, detail: null, findings: null, memories: null,
      ffilter: { severity: 'all', status: 'open', tag: '', run: '', reviewer: '', path: '', since: 0, shadow: false },
      mfilter: { category: 'all', state: 'all', text: '' }, source: null, checkout: '', error: null, seq: 0, timer: null,
      writes: false, picked: new Set(), forgetNote: null, forgetting: false, forgetEpoch: 0, forgetHost: null,
      fsel: new Set(), acting: false, actEpoch: 0, actHost: null, actResult: null };
    const tablePrefs = id => ctx.tablePrefs ? ctx.tablePrefs(id) : null, savePrefs = id => p => ctx.saveTablePrefs && ctx.saveTablePrefs(id, p);

    function table(parent, id, label, columns, rows, extra) {
      parent.replaceChildren();
      root.ForgeTable.render(parent, Object.assign({ id, label, columns, rows, prefs: tablePrefs(id), savePrefs: savePrefs(id) }, extra || {}));
    }
    function readAgain(onClick) {
      const b = el('button', 'Read again'); b.type = 'button'; b.title = 'Read the run dirs, forge issues, the ledgers and the dataset again now';
      b.addEventListener('click', onClick); return b;
    }
    // confirmInline settles only when its own host answers. A redraw drops that host (another checkout,
    // Read again, leaving the tab), the await never finishes, and `forgetting` stays true, so every later
    // checkbox and the Forget button are drawn disabled. Cancel the strip now and retire the attempt.
    function press(node) {
      if (node && typeof node.click === 'function') node.click();
    }
    function dismissConfirm(host) {
      if (!host) return;
      for (const child of [...(host.children || [])]) {
        if (!child.classList || !child.classList.contains('ui-confirm')) continue;
        for (const button of child.children || []) {
          if (button.tagName === 'BUTTON' && button.textContent === 'Cancel') { press(button); return; }
        }
      }
    }
    // The same for a finding action (a confirm, or the drafts being made for File selected).
    function abandonAct() {
      if (!s.acting) return;
      const host = s.actHost;
      s.acting = false;
      s.actEpoch += 1;
      s.actHost = null;
      dismissConfirm(host);
    }
    function abandonForget() {
      abandonAct();
      if (!s.forgetting) return;
      const host = s.forgetHost;
      s.forgetting = false;
      s.forgetEpoch += 1;
      s.forgetHost = null;
      dismissConfirm(host);
    }
    const notes = n => el('div', undefined, 'prj-notes muted');
    function fill(node, items) { node.replaceChildren(...list(items).map(t => el('p', String(t)))); }

    /* the list */
    function drawList() {
      abandonForget();
      const head = el('div', undefined, 'prj-head'), body = el('div', undefined, 'prj-body');
      head.append(el('h1', 'Projects', 'module-title'));
      host.replaceChildren(head, el('p', 'Each repository forge has run in: its runs, every finding its reviews wrote and every memory forge keeps for it, whichever checkout a run started in.', 'muted prj-lede'), body);
      const draw = () => guard(body, () => {
        if (!s.list) { body.replaceChildren(el('p', s.error ? `Could not read the projects (${s.error}).` : 'Reading the projects…', s.error ? 'warn' : 'muted')); return; }
        const rows = list(s.list.projects);
        if (!rows.length) { body.replaceChildren(el('p', 'No projects yet: forge-ui lists no run.', 'muted')); return; }
        table(body, 'projects', 'Projects', [
          { id: 'name', label: 'Project', value: p => p.name, cell: p => {
            const box = el('span', undefined, 'prj-name'), a = el('a', p.name); a.href = routeOf(p.id); box.append(a);
            if (!p.exists) box.append(el('span', 'folder gone', 'chip st-unknown'));
            if (p.path_only) { const c = el('span', 'tied by path', 'chip st-pending'); c.title = `${p.path_only} run(s) were tied to this repository only by their path: it may not be a separate repository`; box.append(c); }
            return box; } },
          { id: 'root', label: 'Root', value: p => p.root, mono: true, cls: 'prj-cut', cell: p => { const n = el('span', p.root); n.title = p.root; return n; } },
          { id: 'runs', label: 'Runs', value: p => p.runs, cell: p => String(p.runs) },
          { id: 'live', label: 'Live now', value: p => p.live, cell: p => p.live ? String(p.live) : '—' },
          { id: 'last', label: 'Last activity', value: p => p.last, cell: p => when(p.last) },
          { id: 'open', label: 'Findings open', title: 'findings no later passing review superseded; dismissals and filed issues show on the Findings tab', value: p => p.open_findings, cell: p => p.open_findings == null ? '—' : String(p.open_findings) },
          { id: 'memory', label: 'Memory lines', value: p => p.memory_lines, cell: p => p.memory_lines == null ? '—' : String(p.memory_lines) },
          { id: 'checkouts', label: 'Checkouts', value: p => p.checkouts, cell: p => String(p.checkouts), hidden: true },
        ], rows);
      });
      return draw;
    }

    /* one project */
    function drawProject() {
      abandonForget();
      const head = el('div', undefined, 'prj-head'), tabs = el('nav', undefined, 'seg prj-tabs'), body = el('div', undefined, 'prj-body');
      tabs.setAttribute('aria-label', 'Project tabs');
      for (const t of TABS) { const a = el('a', t[0].toUpperCase() + t.slice(1)); a.href = routeOf(s.id, t); a.dataset.tab = t; if (t === s.tab) a.setAttribute('aria-current', 'page'); tabs.append(a); }
      const title = el('h1', '…', 'module-title'), where = el('p', '', 'muted mono prj-root');
      const links = el('div', undefined, 'prj-links');
      const back = el('a', 'All projects'); back.href = '#/projects';
      const cloud = el('a', 'In the run cloud'); cloud.href = '#/cloud/projects/' + encodeURIComponent(s.id);
      const wts = el('a', 'Worktrees'); wts.href = '#/worktrees';
      links.append(back, cloud, wts);
      head.append(title, links);
      const note = notes();
      host.replaceChildren(head, where, tabs, note, body);
      const syncHead = () => {
        const d = s.detail;
        title.textContent = d ? d.name : s.error ? 'Unknown project' : '…';
        where.textContent = d ? d.root + (d.exists ? '' : ' (this folder is gone)') : '';
        if (d) wts.href = '#/worktrees/repo/' + encodeURIComponent(d.root);
      };
      const draw = () => { syncHead(); guard(body, () => drawTab(body, note)); };
      return draw;
    }
    function drawTab(body, note) {
      if (!s.detail) { abandonForget(); body.replaceChildren(el('p', s.error ? `Could not read this project (${s.error}).` : 'Reading the project…', s.error ? 'warn' : 'muted')); return; }
      if (s.tab === 'runs') return drawRuns(body, note);
      if (s.tab === 'findings') return drawFindings(body, note);
      return drawMemories(body, note);
    }

    /* Runs */
    function drawRuns(body, note) {
      abandonForget();
      fill(note, []);
      const runs = list(s.detail.runs), checkouts = list(s.detail.checkouts);
      const bar = el('div', undefined, 'prj-filters');
      if (checkouts.length > 1) {
        const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = 'checkout';
        for (const [v, t] of [['', 'all']].concat(checkouts.map(c => [c.path, basename(c.path) + (c.branch ? ` (${c.branch})` : '') + (c.exists ? '' : ' · gone')]))) { const o = el('option', t); o.value = v; sel.append(o); }
        sel.value = s.checkout; sel.addEventListener('change', () => { s.checkout = sel.value; drawRuns(body, note); });
        l.append(el('span', 'Checkout', 'plate'), sel); bar.append(l);
      }
      const shown = runs.filter(r => !s.checkout || (r.repo_worktree || s.detail.root) === s.checkout);
      const tableHost = el('div', undefined, 'prj-table');
      body.replaceChildren(bar, tableHost);
      if (!shown.length) { tableHost.append(el('p', 'No runs.', 'muted')); return; }
      const RM = root.ForgeRunModel;
      table(tableHost, 'project-runs', 'Runs', [
        { id: 'state', label: 'State', value: r => RM ? RM.runState(r) : null, cell: r => { const b = el('span', undefined, 'fnd-status'); const st = RM ? RM.runState(r) : 'pending'; b.append(el('i', undefined, 'lamp st-' + ({ live: 'running', failed: 'fail', done: 'merged', pending: 'pending' })[st]), el('span', st)); return b; } },
        { id: 'goal', label: 'Goal', value: r => r.goal, cell: r => { const a = el('a', String(r.goal || 'Untitled run').split('\n')[0]); a.href = ctx.runHref(r.key); return a; } },
        { id: 'run', label: 'Run', value: r => r.run_id, mono: true, cell: r => r.run_id || '—' },
        { id: 'checkout', label: 'Checkout', value: r => r.repo_worktree || '', cell: r => r.repo_worktree ? basename(r.repo_worktree) + (r.repo_branch ? ` (${r.repo_branch})` : '') : 'root' },
        { id: 'tasks', label: 'Tasks', value: r => r.task_count, cell: r => Object.entries(r.counts || {}).map(([k, n]) => `${k} ${n}`).join(' · ') || '—' },
        { id: 'verification', label: 'Verification', value: r => r.verification_status, cell: r => r.verification_status || '—' },
        { id: 'review', label: 'Final review', value: r => r.final_review, cell: r => r.final_review ? r.final_review + (r.final_review_outdated ? ' (outdated)' : '') : '—' },
        { id: 'updated', label: 'Updated', value: r => r.updated, cell: r => when(r.updated) },
      ], shown);
    }

    /* Findings */
    function copyButton(command) {
      const b = el('button', 'Copy', 'fnd-copy'); b.type = 'button'; b.setAttribute('aria-label', 'Copy: ' + command);
      b.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); b.textContent = 'Copied'; } catch (_) { b.textContent = 'Copy failed: select it'; }
        setTimeout(() => { b.textContent = 'Copy'; }, 2500);
      });
      return b;
    }
    function drawFindings(body, note) {
      abandonForget();
      const d = s.findings;
      if (!d) { fill(note, []); body.replaceChildren(el('p', s.ferror ? `Could not read the findings (${s.ferror}).` : 'Reading every review of every run… the first read parses each reply, so it can take a few seconds.', s.ferror ? 'warn' : 'muted')); return; }
      const lines = list(d.notes).concat(d.dataset_runs ? [`${d.dataset_runs} run(s) whose directory is gone are read from the dataset: their findings link to no run page or diff.`] : []);
      if (d.issues_online && Number.isFinite(d.issues_read_at)) lines.push(`issue states read from GitHub at ${when(d.issues_read_at)}`);
      else if (!d.issues_online && !lines.some(n => /GitHub/.test(String(n)))) lines.push('issue states were not read from GitHub (offline): filed findings may show as open');
      fill(note, lines);
      const rows = list(d.rows);
      const have = new Set(rows.map(r => r.record_fp));
      for (const fp of [...s.fsel]) if (!have.has(fp)) s.fsel.delete(fp);
      const tags = A.tagsIn(rows);
      if (s.ffilter.tag && !tags.includes(s.ffilter.tag)) s.ffilter.tag = '';
      const bar = el('div', undefined, 'prj-filters');
      const select = (name, text, options) => {
        const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = name;
        for (const [v, t] of options) { const o = el('option', t); o.value = String(v); sel.append(o); }
        sel.value = String(s.ffilter[name]);
        sel.addEventListener('change', () => { s.ffilter[name] = name === 'since' ? Number(sel.value) : sel.value; drawTable(); });
        l.append(el('span', text, 'plate'), sel); bar.append(l);
      };
      const counts = {}; for (const r of rows) counts[r.status] = (counts[r.status] || 0) + 1;
      const countOf = x => x === 'open' ? OPEN.reduce((n, k) => n + (counts[k] || 0), 0) : counts[x] || 0;
      select('status', 'Status', [['all', 'all']].concat(STATUSES.map(x => [x, `${x} (${countOf(x)})`])));
      select('tag', 'Tag', [['', 'all']].concat(tags.map(t => [t, t])));
      select('severity', 'Severity', [['all', 'all']].concat(SEVERITIES.map(x => [x, x])));
      select('run', 'Run', [['', 'all']].concat(Array.from(new Set(rows.map(r => r.run_id).filter(Boolean))).sort().map(x => [x, x])));
      select('reviewer', 'Reviewer', [['', 'all']].concat(Array.from(new Set(rows.map(r => r.reviewer).filter(Boolean))).sort().map(x => [x, x])));
      select('since', 'Date', [[0, 'any time'], [1, 'last day'], [7, 'last week'], [30, 'last month']]);
      const pl = el('label', undefined, 'fnd-field'), path = el('input'); path.type = 'search'; path.placeholder = 'path contains…'; path.value = s.ffilter.path; path.dataset.filter = 'path';
      path.addEventListener('input', () => { s.ffilter.path = path.value; drawTable(); });
      pl.append(el('span', 'Path', 'plate'), path); bar.append(pl);
      const sh = el('label', undefined, 'fnd-toggle'), box = el('input'); box.type = 'checkbox'; box.checked = s.ffilter.shadow; box.dataset.filter = 'shadow';
      box.addEventListener('change', () => { s.ffilter.shadow = box.checked; drawTable(); });
      sh.append(box, el('span', 'Shadow reviews')); bar.append(sh);
      const again = readAgain(() => loadTab(true));
      bar.append(again);
      const count = el('p', '', 'muted prj-count'), tableHost = el('div', undefined, 'prj-table');
      const formHost = el('div', undefined, 'fnd-form'), actHost = el('div', undefined, 'fnd-confirm'), noteHost = el('div', undefined, 'fnd-result');
      s.actHost = actHost;
      if (s.actResult) UI.outcome(noteHost, s.actResult.res, s.actResult.text, { silent: true });
      const shownNow = () => filterFindings(rows, s.ffilter);
      const selectedNow = () => A.selectedRows(shownNow(), s.fsel);

      /* the write controls */
      let allBox = null, picked = null;
      const opButtons = {};
      function syncBar() {
        if (!picked) return;
        const sel = selectedNow(), ok = A.allowed(sel);
        picked.textContent = ok.why || `${ok.count} selected`;
        for (const [op, b] of Object.entries(opButtons)) b.disabled = s.acting || !ok[op];
        const shown = shownNow().filter(A.selectable);
        allBox.checked = shown.length > 0 && shown.every(r => s.fsel.has(r.record_fp));
        allBox.disabled = !shown.length;
      }
      const closeForm = () => formHost.replaceChildren();
      // Confirm, post, and show what the route said. Copies forgetSelected's guard: a redraw (Read again, another tab)
      // drops this strip, so its epoch is retired, the controls are usable again, and a request already sent keeps its result.
      async function perform(op, fps, request) {
        if (s.acting) { UI.outcome(noteHost, { ok: false, status: 409, error: 'busy', detail: 'another action is waiting for its confirm' }); return; }
        const epoch = s.actEpoch, host = actHost, current = () => epoch === s.actEpoch && host.isConnected;
        s.acting = true; s.actResult = null; s.actHost = host; syncBar();
        noteHost.replaceChildren();
        for (const child of [...host.children]) child.remove();
        try {
          const yes = await UI.confirmInline(host, A.confirmText(op, fps.length, request), {});
          if (!current() || !yes) return;
          const res = await UI.post(A.pathOf(s.id, op), request);
          s.actResult = { res, text: `${fps.length} finding${fps.length === 1 ? '' : 's'} ${A.PAST[op]}` };
          if (res && res.ok) for (const fp of fps) s.fsel.delete(fp);
          if (!current()) { if (s.tab === 'findings') await loadTab(true); return; }
          if (!res || !res.ok) { UI.outcome(noteHost, res); return; }
          closeForm();
          s.acting = false;
          await loadTab(true);
        } finally {
          if (epoch === s.actEpoch) { s.acting = false; if (host.isConnected) syncBar(); }
        }
      }
      const textInput = (ph, max) => { const i = el('input'); i.type = 'text'; i.placeholder = ph; if (max) i.maxLength = max; return i; };
      const field = (label, input) => { const l = el('label', undefined, 'fnd-field'); l.append(el('span', label, 'plate'), input); return l; };
      function openForm(op, row) {
        const fpsNow = () => row ? [row.record_fp] : A.fpsOf(selectedNow());
        if (op === 'reopen') { const fps = fpsNow(), built = A.bodyFor(op, fps, {}); if (built.error) UI.outcome(noteHost, { ok: false, error: built.error }); else perform(op, fps, built.body); return; }
        closeForm();
        let collect = () => ({}), first = null;
        const parts = [], heading = el('p', undefined, 'fnd-form-title');
        if (op === 'fix') {
          const c = textInput('7 to 40 hex characters'), r = textInput('why (optional with a commit)', 500); first = c;
          parts.push(field('Commit', c), field('Reason', r)); collect = () => ({ commit: c.value, reason: r.value });
          heading.textContent = 'Mark fixed: give a commit, a reason, or both';
        } else if (op === 'dismiss') {
          const r = textInput('why it needs no work', 500); first = r;
          parts.push(field('Reason (required)', r)); collect = () => ({ reason: r.value });
          heading.textContent = 'Dismiss: say why';
        } else if (op === 'to-file') {
          const t = textInput('issue title (optional)', 200); first = t;
          parts.push(field('Title', t)); collect = () => ({ title: t.value });
          heading.textContent = 'Mark to file: keep a draft issue for these findings';
        } else if (op === 'dup') {
          const sel = el('select'); first = sel;
          const mine = fpsNow()[0];
          for (const r of shownNow().filter(x => A.selectable(x) && x.record_fp !== mine)) {
            const o = el('option', `${r.severity || '—'} ${cleanSummary(r.summary, r.location).slice(0, 90)} · ${r.record_fp}`); o.value = r.record_fp; sel.append(o);
          }
          if (!sel.children.length) parts.push(el('p', 'No other finding is shown: widen the filters to pick the original.', 'muted'));
          else { parts.push(field('Duplicate of', sel)); collect = () => ({ of: sel.value }); }
          heading.textContent = 'Mark duplicate: pick the finding it repeats';
        } else if (op === 'tag') {
          const t = textInput('tags, comma separated'), m = el('select'); first = t;
          for (const [v, x] of [['add', 'Add'], ['remove', 'Remove']]) { const o = el('option', x); o.value = v; m.append(o); }
          parts.push(field('Tags', t), field('Action', m)); collect = () => ({ tags: t.value, remove: m.value === 'remove' });
          heading.textContent = 'Tags: lowercase, digits and dashes, at most 8';
        } else if (op === 'note') {
          const t = el('textarea'); t.rows = 3; t.maxLength = 2000; t.placeholder = 'a note (empty clears it)'; t.value = row && row.record && row.record.note || ''; first = t;
          parts.push(field('Note', t)); collect = () => ({ text: t.value });
          heading.textContent = 'Note on ' + (row ? cleanSummary(row.summary, row.location).slice(0, 80) : 'the finding');
        } else if (op === 'edit') {
          const sv = el('select'), t = el('textarea'); first = sv; t.rows = 4; t.maxLength = 4000;
          const was = row && row.record && row.record.edit && row.record.edit.text || (row && (row.text || row.summary)) || '';
          t.value = was;
          for (const v of [''].concat(SEVERITIES)) { const o = el('option', v || 'keep'); o.value = v; sv.append(o); }
          sv.value = '';
          parts.push(field('Severity', sv), field('Text', t));
          collect = () => ({ severity: sv.value, text: t.value === was ? undefined : t.value });
          heading.textContent = 'Edit ' + (row ? cleanSummary(row.summary, row.location).slice(0, 80) : 'the finding') + (row && row.severity ? ` (now ${row.severity})` : '');
        }
        const msg = el('p', undefined, 'warn fnd-form-msg');
        const apply = () => {
          const fps = fpsNow(), built = A.bodyFor(op, fps, collect());
          if (built.error) { msg.textContent = built.error; return; }
          msg.textContent = '';
          perform(op, fps, built.body);
        };
        const go = el('button', 'Apply', 'primary'); go.type = 'button'; go.addEventListener('click', apply);
        const cancel = el('button', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', closeForm);
        const wrap = el('div', undefined, 'fnd-form-body');
        wrap.append(...parts, go, cancel);
        wrap.addEventListener('keydown', e => {
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeForm(); }
          else if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') { e.preventDefault(); apply(); }
        });
        formHost.append(heading, wrap, msg);
        if (first && first.focus) first.focus();
      }
      // File selected: drafts for exactly the chosen findings, a dialog, and the POST only after its confirm.
      async function fileSelected() {
        const FF = root.ForgeFindings;
        if (s.acting || !FF) return;
        const sel = selectedNow(), fps = A.fileableFps(sel), key = A.fileRun(sel);
        if (!fps.length || !key) return;
        const epoch = s.actEpoch;
        s.acting = true; s.actResult = null; s.actHost = actHost; noteHost.replaceChildren(); syncBar();
        let res;
        try { res = await UI.get(FF.issuesUrl(key, { scope: 'repo', offline: false, plausible: true, only: fps })); }
        finally { if (epoch === s.actEpoch) { s.acting = false; if (actHost.isConnected) syncBar(); } }
        if (!res || !res.ok) {
          if (epoch === s.actEpoch && actHost.isConnected) UI.outcome(noteHost, res); else s.actResult = { res };
          return;
        }
        fileDialog(FF, key, fps, list(res.data && res.data.drafts));
      }
      function fileDialog(FF, key, fps, drafts) {
        const dlg = el('dialog', undefined, 'fnd-dialog'); dlg.setAttribute('aria-label', 'File issues on GitHub');
        document.body.append(dlg);
        let posting = false;
        const cleanup = () => { dlg.remove(); };
        const shut = () => { if (typeof dlg.close === 'function') dlg.close(); else cleanup(); };
        dlg.addEventListener('close', cleanup);
        dlg.addEventListener('click', e => { if (e.target === dlg && !posting) shut(); });
        dlg.addEventListener('cancel', e => { if (posting) e.preventDefault(); });
        dlg.append(el('h2', 'File issues on GitHub'));
        const warn = el('p', undefined, 'fnd-warn');
        warn.append(el('i', undefined, 'lamp st-running'), el('span', `This creates ${drafts.length} issue${drafts.length === 1 ? '' : 's'} on this repository's GitHub project, from your gh login, and cannot be undone from here.`));
        dlg.append(warn);
        if (!drafts.length) dlg.append(el('p', 'forge has no draft for this selection (its findings may be filed already).', 'muted'));
        for (const dr of drafts) {
          const art = el('article', undefined, 'fnd-draft'), h = el('h3', undefined, 'fnd-draft-title');
          h.append(el('span', dr.severity || '—', 'chip sev-chip sev-' + (SEVERITIES.includes(dr.severity) ? dr.severity : 'none')), el('span', dr.title || 'untitled'));
          art.append(h);
          if (dr.same_title) {
            const url = FF.safeUrl(dr.same_title), p = el('p', undefined, 'fnd-same');
            p.append(el('i', undefined, 'lamp fnd-amber'), el('span', 'An open issue with this title exists already: '));
            if (url) { const a = el('a', url); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; p.append(a); } else p.append(el('span', String(dr.same_title)));
            p.append(el('span', '. forge skips it.')); art.append(p);
          }
          art.append(el('pre', dr.body || '', 'fnd-body')); dlg.append(art);
        }
        const labels = el('input'); labels.type = 'text'; labels.placeholder = 'bug, forge (optional, comma separated)';
        const labelNote = el('p', undefined, 'warn fnd-label-note'); labelNote.hidden = true;
        const results = el('div', undefined, 'fnd-results');
        const go = el('button', 'File on GitHub', 'primary'), cancel = el('button', 'Cancel'); go.type = 'button'; cancel.type = 'button';
        go.disabled = !drafts.length;
        cancel.addEventListener('click', shut);
        go.addEventListener('click', async () => {
          const parsed = FF.parseLabels(labels.value);
          labelNote.hidden = !parsed.error; labelNote.textContent = parsed.error || '';
          if (parsed.error) return;
          go.disabled = true; cancel.disabled = true; labels.disabled = true; results.replaceChildren(); posting = true;
          const res = await UI.post(FF.issuesPath(key), FF.fileBody('repo', fps, true, parsed.labels));
          posting = false;
          if (!res || !res.ok) { go.disabled = false; cancel.disabled = false; labels.disabled = false; UI.outcome(results, res); return; }
          const made = FF.resultLines(res.data.created), ul = el('ul', undefined, 'fnd-created');
          for (const ln of made) {
            const li = el('li', undefined, 'fnd-created-' + ln.kind), url = FF.safeUrl(ln.url);
            li.append(el('i', undefined, 'lamp ' + (ln.kind === 'created' ? 'st-pass' : 'fnd-amber')));
            if (url) { const a = el('a', ln.text); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; li.append(a); } else li.append(el('span', ln.text));
            ul.append(li);
          }
          results.replaceChildren(el('p', `${made.filter(l => l.kind === 'created').length} created`, 'fnd-done'), ul);
          for (const n of FF.emptyNotes(res.data.created, res.data.notes)) results.append(el('p', n, 'muted fnd-result-note'));
          go.remove(); cancel.textContent = 'Close'; cancel.disabled = false;
          for (const r of list(s.findings && s.findings.rows)) if (fps.includes(r.fp)) s.fsel.delete(r.record_fp);
          if (s.tab === 'findings') loadTab(true);
        });
        const actions = el('div', undefined, 'fnd-dialog-actions'); actions.append(cancel, go);
        const labelField = el('label', undefined, 'fnd-field'); labelField.append(el('span', 'Labels', 'plate'), labels);
        dlg.append(labelField, labelNote, results, actions);
        if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
        (drafts.length ? go : cancel).focus();
      }

      if (s.writes) {
        const abar = el('div', undefined, 'fnd-actions prj-actions');
        const lab = el('label', undefined, 'fnd-toggle');
        allBox = el('input'); allBox.type = 'checkbox'; allBox.setAttribute('aria-label', 'Select all shown findings');
        lab.append(allBox, el('span', 'Select all'));
        picked = el('span', '0 selected', 'muted fnd-picked');
        allBox.addEventListener('change', () => {
          for (const r of shownNow()) { if (!A.selectable(r)) continue; if (allBox.checked) s.fsel.add(r.record_fp); else s.fsel.delete(r.record_fp); }
          drawTable();
        });
        abar.append(lab, picked);
        for (const [op, text] of [['fix', 'Mark fixed'], ['dismiss', 'Dismiss'], ['reopen', 'Reopen'], ['to-file', 'Mark to file'], ['dup', 'Duplicate of…'], ['tag', 'Tags…']]) {
          const b = el('button', text); b.type = 'button'; b.dataset.op = op; b.disabled = true;
          b.addEventListener('click', () => openForm(op)); opButtons[op] = b; abar.append(b);
        }
        const fileKey = el('button', 'File selected', 'primary'); fileKey.type = 'button'; fileKey.dataset.op = 'file'; fileKey.disabled = true;
        fileKey.title = 'creates GitHub issues for the selected findings that forge issues can file (at most ' + A.MAX_FILE + ')';
        fileKey.addEventListener('click', () => { fileSelected(); }); opButtons.file = fileKey; abar.append(fileKey);
        body.replaceChildren(bar, abar, formHost, actHost, noteHost, count, tableHost);
      } else body.replaceChildren(bar, count, tableHost);

      /* the table */
      const summaryCell = r => {
        const wrap = el('div', undefined, 'fnd-summary'), line = el('div', undefined, 'fnd-summary-line');
        const more = el('button', undefined, 'fnd-more'); more.type = 'button'; more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-label', 'Show the full text');
        more.append(el('span', '▸', 'fnd-caret'));
        const detail = el('div', undefined, 'fnd-detail'); detail.hidden = true;
        detail.append(el('pre', r.text || r.summary || '', 'fnd-text'));
        const rec = r.record || {};
        for (const [k, label] of [['reason', 'reason'], ['commit', 'fixed by'], ['duplicate_of', 'duplicate of'], ['issue', 'issue'], ['note', 'note']]) {
          if (rec[k] != null && String(rec[k]) !== '') detail.append(el('p', `${label}: ${rec[k]}`, 'fnd-rec'));
        }
        if (list(r.occurrences).length > 1) {
          const ul = el('ul', undefined, 'prj-occ');
          for (const o of r.occurrences) { const li = el('li'); const t = `${o.run_id || '—'} · ${o.task || 'final review'} · attempt ${o.attempt == null ? '—' : o.attempt} · ${o.reviewer || '—'} · ${when(o.at)}`; if (o.href) { const a = el('a', t); a.href = o.href; li.append(a); } else li.append(el('span', t)); ul.append(li); }
          detail.append(el('p', `Seen ${r.count} times, the newest first in the row:`, 'muted'), ul);
        }
        if (!s.writes) {
          const cmds = A.commandsFor(s.detail && s.detail.root, r), cbox = el('div', undefined, 'fnd-cmds');
          if (!cmds.length) cbox.append(el('p', 'This finding has no record key, so forge cannot change it.', 'muted'));
          else {
            cbox.append(el('p', 'This page is read-only. Change this finding from a terminal:', 'muted'));
            for (const c of cmds) { const rowEl = el('div', undefined, 'fnd-cmd'), code = el('code', c.command, 'mono'); code.tabIndex = 0; code.title = c.label; rowEl.append(code, copyButton(c.command)); cbox.append(rowEl); }
          }
          detail.append(cbox);
        }
        more.addEventListener('click', () => { const open = detail.hidden; detail.hidden = !open; more.setAttribute('aria-expanded', String(open)); more.firstChild.textContent = open ? '▾' : '▸'; });
        line.append(more, el('span', cleanSummary(r.summary, r.location) || '—', 'prj-summary'));
        if (r.count > 1) line.append(el('span', `×${r.count}`, 'chip prj-count-chip'));
        wrap.append(line);
        if (list(r.tags).length) {
          const chips = el('div', undefined, 'fnd-tags'), derived = A.derivedTags(r);
          for (const t of r.tags) {
            const c = el('span', t, 'fnd-tag' + (derived ? ' fnd-tag-derived' : '')); if (derived) c.title = 'derived from the path';
            chips.append(c);
          }
          wrap.append(chips);
        }
        wrap.append(detail); return wrap;
      };
      const statusCell = r => {
        const b = el('span', undefined, 'fnd-status fnd-' + r.status);
        b.append(el('i', undefined, 'lamp fnd-lamp-' + r.status + ' ' + (LAMPS[r.status] || 'lamp-off')), el('span', r.status));
        if (r.status === 'superseded') b.title = 'a later review of the same task passed; that does not say this finding was fixed';
        else if (r.status === 'to-file') b.title = 'marked to file as a GitHub issue: still open work';
        else if (r.status === 'unlabelled') b.title = 'the finding has no usable label, so forge cannot say what it is';
        return b;
      };
      const whereCell = r => {
        if (r.source === 'dataset') { const x = el('span', 'from the dataset', 'chip st-unknown'); x.title = 'its run directory is gone: no run page or diff to open'; return x; }
        const box = el('span', undefined, 'prj-where');
        if (r.href) { const a = el('a', 'diff'); a.href = r.href; a.title = 'open the diff at this finding'; box.append(a); }
        if (s.writes && A.selectable(r)) {
          for (const op of ['note', 'edit']) {
            const b = el('button', op === 'note' ? 'Note' : 'Edit', 'fnd-rowbtn'); b.type = 'button'; b.dataset.op = op; b.title = op === 'note' ? 'add or change a note' : 'change the severity or the text';
            b.addEventListener('click', () => openForm(op, r)); box.append(document.createTextNode(' '), b);
          }
        }
        return box;
      };
      const runCell = r => { if (!r.run) return r.run_id || '—'; const a = el('a', r.run_id || r.run, 'mono'); a.href = ctx.runHref(r.run); return a; };
      const columns = [
        { id: 'severity', label: 'Severity', value: r => SEVERITIES.indexOf(r.severity), cell: r => el('span', r.severity || '—', 'chip sev-chip sev-' + (SEVERITIES.includes(r.severity) ? r.severity : 'none')) },
        { id: 'confidence', label: 'Confidence', value: r => r.confidence, cell: r => r.confidence || '—' },
        { id: 'summary', label: 'Summary', value: r => r.summary, cls: 'prj-wide', cell: summaryCell },
        { id: 'location', label: 'Location', value: r => r.location, mono: true, cls: 'prj-cut', cell: r => { const n = el('span', r.location || '—'); if (r.location) n.title = r.location; return n; } },
        { id: 'run', label: 'Run', value: r => r.run_id, cell: runCell },
        { id: 'task', label: 'Task', value: r => r.task, mono: true, cell: r => r.task || (r.final_review ? 'final review' : '—') },
        { id: 'attempt', label: 'Attempt', value: r => r.attempt, cell: r => r.attempt == null ? '—' : String(r.attempt) },
        { id: 'reviewer', label: 'Reviewer', value: r => r.reviewer, cell: r => (r.reviewer || '—') + (r.kind === 'shadow' ? ' (shadow)' : '') },
        { id: 'date', label: 'Date', value: r => r.at, cls: 'prj-nowrap', cell: r => { const n = el('span', Number.isFinite(r.at) ? new Date(r.at * 1000).toLocaleDateString() : '—'); n.title = when(r.at); return n; } },
        { id: 'status', label: 'Status', value: r => STATUSES.indexOf(r.status), cell: statusCell },
        { id: 'open', label: 'Open', sortable: false, cell: whereCell },
      ];
      if (s.writes) columns.unshift({ id: 'pick', label: '', title: 'Select', sortable: false, hideable: false, cell: r => {
        if (!A.selectable(r)) { const n = el('span', '—', 'muted'); n.title = 'this finding has no record key: forge cannot change it'; return n; }
        const cb = el('input'); cb.type = 'checkbox'; cb.checked = s.fsel.has(r.record_fp); cb.setAttribute('aria-label', 'Select finding ' + r.record_fp);
        cb.addEventListener('change', () => { if (cb.checked) s.fsel.add(r.record_fp); else s.fsel.delete(r.record_fp); syncBar(); });
        return cb;
      } });
      function drawTable() {
        const shown = shownNow();
        count.textContent = `${shown.length} of ${rows.length} findings`;
        syncBar();
        if (!shown.length) { tableHost.replaceChildren(el('p', rows.length ? 'No findings match these filters.' : 'No review of this repository wrote a finding.', 'muted')); return; }
        table(tableHost, 'project-findings', 'Findings', columns, shown, { className: 'fnd-grid prj-findings' });
      }
      drawTable();
    }

    /* Memories */
    function drawMemories(body, note) {
      abandonForget();
      const d = s.memories;
      if (!d) { fill(note, []); body.replaceChildren(el('p', s.merror ? `Could not read the memories (${s.merror}).` : 'Reading each checkout\'s ledger…', s.merror ? 'warn' : 'muted')); return; }
      fill(note, d.notes);
      const sources = list(d.sources);
      if (!s.source || !sources.some(x => x.path === s.source)) s.source = (sources.find(x => x.root) || sources[0] || {}).path || null;
      const bar = el('div', undefined, 'prj-filters');
      if (sources.length > 1) {
        const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = 'source';
        for (const x of sources) { const o = el('option', basename(x.path) + (x.root ? ' (root)' : x.branch ? ` (${x.branch})` : '')); o.value = x.path; sel.append(o); }
        sel.value = s.source || ''; sel.addEventListener('change', () => { s.source = sel.value; s.picked = new Set(); s.forgetNote = null; drawMemories(body, note); });
        l.append(el('span', 'Checkout', 'plate'), sel); bar.append(l);
      }
      bar.append(readAgain(() => loadTab(true)));
      const parts = [bar];
      const empty = memoryEmpty(d);
      if (empty) { parts.push(el('p', empty, 'muted prj-empty')); body.replaceChildren(...parts); return; }
      if (d.differs) parts.push(el('p', 'The checkouts\' memory.md files differ: pick a checkout to see what its runs are told.', 'warn prj-differs'));
      const src = sources.find(x => x.path === s.source);
      const given = el('section', undefined, 'prj-given');
      given.append(el('h2', 'Given to models'), el('p', ROLES_NOTE + '. At most ' + ((src && src.max_lines) || 40) + ' lines.', 'muted'));
      if (src && src.rebuilt_differs) given.append(el('p', 'This memory.md is not what forge would rebuild now: an older forge wrote it, or a prune asked Jev to drop facts. The states below are what the next rebuild does.', 'muted'));
      if (!src || !src.memory || !list(src.memory).length) given.append(el('p', 'This checkout has no memory.md yet.', 'muted'));
      for (const sec of list(src && src.memory)) {
        const block = el('div', undefined, 'prj-section');
        const h = el('h3', sec.title); const roles = el('span', ' · ' + list(sec.roles).join(', '), 'muted'); h.append(roles);
        const ul = el('ul', undefined, 'prj-lines');
        for (const line of list(sec.lines)) ul.append(el('li', (line.count ? line.count + 'x: ' : '') + line.text));
        block.append(h, ul); given.append(block);
      }
      parts.push(given);
      const all = el('section', undefined, 'prj-learnings');
      all.append(el('h2', 'All learnings'));
      const lbar = el('div', undefined, 'prj-filters');
      const lsel = (name, text, options) => { const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = 'm-' + name; for (const [v, t] of options) { const o = el('option', t); o.value = v; sel.append(o); } sel.value = s.mfilter[name]; sel.addEventListener('change', () => { s.mfilter[name] = sel.value; drawLearnings(); }); l.append(el('span', text, 'plate'), sel); lbar.append(l); };
      lsel('category', 'Category', [['all', 'all'], ['verify', 'verify'], ['trap', 'trap'], ['finding', 'finding']]);
      lsel('state', 'State', [['all', 'all']].concat(Object.entries(STATES).map(([k, v]) => [k, v])));
      const tl = el('label', undefined, 'fnd-field'), tx = el('input'); tx.type = 'search'; tx.placeholder = 'text contains…'; tx.value = s.mfilter.text; tx.dataset.filter = 'm-text';
      tx.addEventListener('input', () => { s.mfilter.text = tx.value; drawLearnings(); }); tl.append(el('span', 'Text', 'plate'), tx); lbar.append(tl);
      let allBox = null, forget = null;
      const faction = el('div'), noteHost = el('div'); faction.append(noteHost);
      s.forgetHost = faction;
      if (s.writes) {
        const lab = el('label', undefined, 'fnd-toggle');
        allBox = el('input'); allBox.type = 'checkbox'; allBox.setAttribute('aria-label', 'Select all shown learnings');
        lab.append(allBox, el('span', 'Select all'));
        forget = el('button', 'Forget selected (0)', 'ui-danger'); forget.type = 'button';
        forget.addEventListener('click', () => { forgetSelected(); });
        allBox.addEventListener('change', () => {
          const shown = filterLearnings(learnings, s.mfilter, s.source);
          for (const row of shown) {
            const key = learningKey(row);
            if (!key) continue;
            if (allBox.checked) s.picked.add(key); else s.picked.delete(key);
          }
          drawLearnings();
        });
        lbar.append(lab, forget);
      }
      const lcount = el('p', '', 'muted prj-count'), lhost = el('div', undefined, 'prj-table');
      all.append(lbar, faction, lcount, lhost); parts.push(all);
      body.replaceChildren(...parts);
      const learnings = list(d.learnings);
      const shownNow = () => filterLearnings(learnings, s.mfilter, s.source);
      function syncForget() {
        if (!forget) return;
        const keys = forgetKeys(shownNow(), s.picked);
        forget.textContent = `Forget selected (${keys.length})`;
        forget.disabled = s.forgetting || keys.length === 0;
        if (allBox) {
          const selectable = shownNow().map(learningKey).filter(Boolean);
          allBox.checked = selectable.length > 0 && selectable.every(key => s.picked.has(key));
          allBox.disabled = s.forgetting || selectable.length === 0;
        }
      }
      function paintNote() {
        noteHost.replaceChildren(...list(s.forgetNote).map(line => el('p', line, 'muted')));
      }
      async function forgetSelected() {
        if (s.forgetting) return;
        const keys = forgetKeys(shownNow(), s.picked);
        if (!keys.length) return;
        const epoch = s.forgetEpoch;
        const host = faction;
        const current = () => epoch === s.forgetEpoch && host.isConnected;
        s.forgetting = true; s.forgetNote = null; syncForget(); paintNote();
        for (const child of [...host.children]) if (child !== noteHost) child.remove();
        const path = '/api/projects/' + encodeURIComponent(s.id) + '/memories/forget';
        const base = { keys }; if (s.source) base.path = s.source;
        try {
          // Dry run first. Nothing is forgotten until the confirm, and a cancel sends nothing more.
          const preview = await UI.post(path, { ...base, dry_run: true });
          if (!current()) return;
          if (!preview || !preview.ok || (preview.data && preview.data.ok === false)) {
            UI.outcome(host, preview && preview.ok ? { ok: false, status: preview.status || 500, error: 'failed', detail: 'Could not preview what would be forgotten' } : preview);
            return;
          }
          const yes = await UI.confirmInline(host, forgetConfirmText(learnings, keys, preview), { danger: true });
          if (!current() || !yes) return;
          const res = await UI.post(path, { ...base, dry_run: false });
          // Once sent, the forget happened whatever the screen did meanwhile (Read again, another checkout,
          // another tab): its result line is kept for the next draw rather than dropped with this strip.
          if (!current()) {
            s.forgetNote = res && res.ok ? forgetResultLines(res)
              : ['Could not forget: ' + ((res && (res.detail || res.error)) || 'no answer')];
            if (res && res.ok) for (const key of keys) s.picked.delete(key);
            if (s.tab === 'memories') await loadTab(true);
            return;
          }
          if (!res || !res.ok) { UI.outcome(host, res); return; }
          s.forgetNote = forgetResultLines(res);
          for (const key of keys) s.picked.delete(key);
          s.forgetting = false;
          await loadTab(true);
        } finally {
          if (epoch !== s.forgetEpoch) return;
          s.forgetting = false;
          if (host.isConnected) syncForget();
        }
      }
      function drawLearnings() {
        const shown = shownNow();
        lcount.textContent = `${shown.length} of ${learnings.length} learnings`;
        paintNote(); syncForget();
        if (!shown.length) { lhost.replaceChildren(el('p', 'No learnings match these filters.', 'muted')); return; }
        const columns = [
          { id: 'category', label: 'Category', value: r => r.category, cell: r => r.category },
          { id: 'text', label: 'Learning', value: r => r.text, cell: r => el('span', r.text, 'prj-learning') },
          { id: 'state', label: 'State', value: r => stateFor(r, s.source).state, cell: r => { const st = stateFor(r, s.source), b = el('span', STATES[st.state] || st.state); if (st.from && st.from !== 'dataset') b.title = 'from ' + st.from + ', not this checkout\'s ledger'; return b; } },
          { id: 'count', label: 'Runs', title: 'distinct runs that emitted it', value: r => r.count, cell: r => String(r.count || '—') },
          { id: 'run', label: 'Last run', value: r => (list(r.rows).slice(-1)[0] || {}).run_id, mono: true, cell: r => (list(r.rows).slice(-1)[0] || {}).run_id || '—' },
          { id: 'task', label: 'Task', value: r => (list(r.rows).slice(-1)[0] || {}).task, mono: true, cell: r => (list(r.rows).slice(-1)[0] || {}).task || '—' },
          { id: 'role', label: 'Role', value: r => (list(r.rows).slice(-1)[0] || {}).role, cell: r => (list(r.rows).slice(-1)[0] || {}).role || '—' },
          { id: 'model', label: 'Model', value: r => (list(r.rows).slice(-1)[0] || {}).model, cell: r => (list(r.rows).slice(-1)[0] || {}).model || '—' },
          { id: 'from', label: 'From', value: r => r.dataset_only ? 'dataset' : '', cell: r => r.dataset_only ? 'dataset' : Array.from(new Set(list(r.rows).flatMap(x => list(x.from)))).map(basename).join(', ') },
          { id: 'last', label: 'Last seen', value: r => r.last, cell: r => r.last || '—' },
        ];
        if (s.writes) columns.unshift({ id: 'pick', label: '', title: 'Select', sortable: false, hideable: false, cell: r => {
          const key = learningKey(r), box = el('input'); box.type = 'checkbox'; box.checked = !!(key && s.picked.has(key));
          box.disabled = s.forgetting || !key; box.setAttribute('aria-label', 'Select ' + (key || 'learning'));
          box.addEventListener('change', () => { if (!key) return; if (box.checked) s.picked.add(key); else s.picked.delete(key); syncForget(); });
          return box;
        } });
        table(lhost, 'project-learnings', 'Learnings', columns, shown);
      }
      drawLearnings();
    }

    /* loading */
    async function loadList() {
      const res = await UI.get('/api/projects');
      if (res.ok) { s.list = res.data; s.error = null; } else s.error = res.detail || res.error || 'no answer';
      redraw();
    }
    async function loadDetail() {
      const res = await UI.get('/api/projects/' + encodeURIComponent(s.id));
      if (res.ok) { s.detail = res.data; s.error = null; } else s.error = res.status === 404 ? 'no project has this id' : res.detail || res.error || 'no answer';
      redraw();
    }
    async function loadTab(refresh) {
      const tab = s.tab, mine = ++s.seq;
      if (tab === 'runs') { await loadDetail(); return; }
      const q = refresh ? '?refresh=1' : '';
      const res = await UI.get(`/api/projects/${encodeURIComponent(s.id)}/${tab}${q}`);
      if (mine !== s.seq) return;
      if (tab === 'findings') { if (res.ok) { s.findings = res.data; s.ferror = null; } else s.ferror = res.detail || res.error || 'no answer'; }
      else { if (res.ok) { s.memories = res.data; s.merror = null; } else s.merror = res.detail || res.error || 'no answer'; }
      redraw();
    }
    let redraw = () => {};
    function open() {
      clearTimeout(s.timer);
      if (s.id) { redraw = drawProject(); redraw(); loadDetail().then(() => { if (s.tab !== 'runs') loadTab(false); }); }
      else { redraw = drawList(); redraw(); loadList(); }
    }
    UI.controlsEnabled().then(on => {
      const next = !!on;
      if (s.writes === next) return;
      s.writes = next;
      if ((s.tab === 'findings' && s.findings) || (s.tab === 'memories' && s.memories)) redraw();
    });
    open();
    return {
      go(id, tab) {
        const t = TABS.includes(tab) ? tab : 'runs';
        if (id === s.id && t === s.tab) return;             // the page's own refresh: nothing changed here
        const same = id === s.id;
        s.id = id || null; s.tab = t;
        if (!same) { s.detail = s.findings = s.memories = null; s.source = null; s.checkout = ''; s.picked = new Set(); s.forgetNote = null; s.fsel = new Set(); s.actResult = null; }
        if (same && s.id) { redraw = drawProject(); redraw(); if (t === 'runs') loadDetail(); else if (!(t === 'findings' ? s.findings : s.memories)) loadTab(false); }
        else open();
      },
      // the list and the runs follow the page at most every 30 s; findings and memories wait for Read again
      poll() {
        const now = Date.now();
        if (s.polled && now - s.polled < 30000) return;
        s.polled = now;
        if (!s.id) loadList(); else if (s.tab === 'runs') loadDetail();
      },
    };
  }

  return { render, cleanSummary, filterFindings, filterLearnings, stateFor, memoryEmpty, routeOf, forgetKeys, STATUSES, STATES };
});
