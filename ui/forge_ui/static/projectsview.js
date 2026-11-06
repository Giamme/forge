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
  const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
  const STATUSES = ['open', 'filed', 'fixed', 'swept', 'dismissed', 'superseded'];
  const STATES = { in_memory: 'in memory', waiting: 'waiting for a second run', stale: 'dropped: stale', capped: 'dropped: trimmed by the cap',
    muted: 'not injected (curated out)', dropped: 'dropped', gone: 'from a checkout that is gone' };
  const TABS = ['runs', 'findings', 'memories'];
  const ROLES_NOTE = 'planner and dwarf get Verify, Known traps and Recurring QA findings; QA gets the last two';

  /* ---------- pure ---------- */
  // filter = { severity: 'all' | P0..P3, status: 'all' | 'open' (default) | STATUSES, run: '' | run id, reviewer: '', path: '' (substring),
  //            since: 0 (days, 0 = any), shadow: false }
  function filterFindings(rows, filter, now) {
    const f = filter || {}, sev = f.severity || 'all', status = f.status || 'open', path = String(f.path || '').toLowerCase();
    const since = Number(f.since) > 0 ? (now || Date.now() / 1000) - Number(f.since) * 86400 : null;
    return list(rows).filter(r => (f.shadow || r.kind !== 'shadow')
      && (sev === 'all' || r.severity === sev) && (status === 'all' || r.status === status)
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
      ffilter: { severity: 'all', status: 'open', run: '', reviewer: '', path: '', since: 0, shadow: false },
      mfilter: { category: 'all', state: 'all', text: '' }, source: null, checkout: '', error: null, seq: 0, timer: null };
    const tablePrefs = id => ctx.tablePrefs ? ctx.tablePrefs(id) : null, savePrefs = id => p => ctx.saveTablePrefs && ctx.saveTablePrefs(id, p);

    function table(parent, id, label, columns, rows, extra) {
      parent.replaceChildren();
      root.ForgeTable.render(parent, Object.assign({ id, label, columns, rows, prefs: tablePrefs(id), savePrefs: savePrefs(id) }, extra || {}));
    }
    function readAgain(onClick) {
      const b = el('button', 'Read again'); b.type = 'button'; b.title = 'Read the run dirs, forge issues, the ledgers and the dataset again now';
      b.addEventListener('click', onClick); return b;
    }
    const notes = n => el('div', undefined, 'prj-notes muted');
    function fill(node, items) { node.replaceChildren(...list(items).map(t => el('p', String(t)))); }

    /* the list */
    function drawList() {
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
      if (!s.detail) { body.replaceChildren(el('p', s.error ? `Could not read this project (${s.error}).` : 'Reading the project…', s.error ? 'warn' : 'muted')); return; }
      if (s.tab === 'runs') return drawRuns(body, note);
      if (s.tab === 'findings') return drawFindings(body, note);
      return drawMemories(body, note);
    }

    /* Runs */
    function drawRuns(body, note) {
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
    function drawFindings(body, note) {
      const d = s.findings;
      if (!d) { fill(note, []); body.replaceChildren(el('p', s.ferror ? `Could not read the findings (${s.ferror}).` : 'Reading every review of every run… the first read parses each reply, so it can take a few seconds.', s.ferror ? 'warn' : 'muted')); return; }
      fill(note, list(d.notes).concat(d.dataset_runs ? [`${d.dataset_runs} run(s) whose directory is gone are read from the dataset: their findings link to no run page or diff.`] : []));
      const rows = list(d.rows);
      const bar = el('div', undefined, 'prj-filters');
      const select = (name, text, options) => {
        const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = name;
        for (const [v, t] of options) { const o = el('option', t); o.value = String(v); sel.append(o); }
        sel.value = String(s.ffilter[name]);
        sel.addEventListener('change', () => { s.ffilter[name] = name === 'since' ? Number(sel.value) : sel.value; drawTable(); });
        l.append(el('span', text, 'plate'), sel); bar.append(l);
      };
      const counts = {}; for (const r of rows) counts[r.status] = (counts[r.status] || 0) + 1;
      select('status', 'Status', [['all', 'all']].concat(STATUSES.map(x => [x, `${x} (${counts[x] || 0})`])));
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
      body.replaceChildren(bar, count, tableHost);
      const summaryCell = r => {
        const wrap = el('div', undefined, 'fnd-summary'), line = el('div', undefined, 'fnd-summary-line');
        const more = el('button', undefined, 'fnd-more'); more.type = 'button'; more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-label', 'Show the full text');
        more.append(el('span', '▸', 'fnd-caret'));
        const detail = el('div', undefined, 'fnd-detail'); detail.hidden = true;
        detail.append(el('pre', r.text || r.summary || '', 'fnd-text'));
        if (list(r.occurrences).length > 1) {
          const ul = el('ul', undefined, 'prj-occ');
          for (const o of r.occurrences) { const li = el('li'); const t = `${o.run_id || '—'} · ${o.task || 'final review'} · attempt ${o.attempt == null ? '—' : o.attempt} · ${o.reviewer || '—'} · ${when(o.at)}`; if (o.href) { const a = el('a', t); a.href = o.href; li.append(a); } else li.append(el('span', t)); ul.append(li); }
          detail.append(el('p', `Seen ${r.count} times, the newest first in the row:`, 'muted'), ul);
        }
        more.addEventListener('click', () => { const open = detail.hidden; detail.hidden = !open; more.setAttribute('aria-expanded', String(open)); more.firstChild.textContent = open ? '▾' : '▸'; });
        line.append(more, el('span', cleanSummary(r.summary, r.location) || '—', 'prj-summary'));
        if (r.count > 1) line.append(el('span', `×${r.count}`, 'chip prj-count-chip'));
        wrap.append(line, detail); return wrap;
      };
      const statusCell = r => {
        const b = el('span', undefined, 'fnd-status fnd-' + r.status);
        b.append(el('i', undefined, 'lamp ' + (r.status === 'open' ? 'lamp-off' : r.status === 'filed' ? 'fnd-amber' : r.status === 'superseded' ? 'st-outdated' : r.status === 'dismissed' ? 'lamp-off' : 'st-pass')), el('span', r.status));
        if (r.status === 'superseded') b.title = 'a later review of the same task passed; that does not say this finding was fixed';
        return b;
      };
      const whereCell = r => {
        if (r.source === 'dataset') { const x = el('span', 'from the dataset', 'chip st-unknown'); x.title = 'its run directory is gone: no run page or diff to open'; return x; }
        const box = el('span', undefined, 'prj-where');
        if (r.href) { const a = el('a', 'diff'); a.href = r.href; a.title = 'open the diff at this finding'; box.append(a); }
        if (r.fileable && s.writes) { const f = el('a', 'file or dismiss'); f.href = '#/runs/' + encodeURIComponent(r.run) + '/findings'; f.title = 'on the run\'s Open findings page, through forge issues'; box.append(document.createTextNode(' · '), f); }
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
      function drawTable() {
        const shown = filterFindings(rows, s.ffilter);
        count.textContent = `${shown.length} of ${rows.length} findings`;
        if (!shown.length) { tableHost.replaceChildren(el('p', rows.length ? 'No findings match these filters.' : 'No review of this repository wrote a finding.', 'muted')); return; }
        table(tableHost, 'project-findings', 'Findings', columns, shown, { className: 'fnd-grid prj-findings' });
      }
      drawTable();
    }

    /* Memories */
    function drawMemories(body, note) {
      const d = s.memories;
      if (!d) { fill(note, []); body.replaceChildren(el('p', s.merror ? `Could not read the memories (${s.merror}).` : 'Reading each checkout\'s ledger…', s.merror ? 'warn' : 'muted')); return; }
      fill(note, d.notes);
      const sources = list(d.sources);
      if (!s.source || !sources.some(x => x.path === s.source)) s.source = (sources.find(x => x.root) || sources[0] || {}).path || null;
      const bar = el('div', undefined, 'prj-filters');
      if (sources.length > 1) {
        const l = el('label', undefined, 'fnd-field'), sel = el('select'); sel.dataset.filter = 'source';
        for (const x of sources) { const o = el('option', basename(x.path) + (x.root ? ' (root)' : x.branch ? ` (${x.branch})` : '')); o.value = x.path; sel.append(o); }
        sel.value = s.source || ''; sel.addEventListener('change', () => { s.source = sel.value; drawMemories(body, note); });
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
      const lcount = el('p', '', 'muted prj-count'), lhost = el('div', undefined, 'prj-table');
      all.append(lbar, lcount, lhost); parts.push(all);
      body.replaceChildren(...parts);
      const learnings = list(d.learnings);
      function drawLearnings() {
        const shown = filterLearnings(learnings, s.mfilter, s.source);
        lcount.textContent = `${shown.length} of ${learnings.length} learnings`;
        if (!shown.length) { lhost.replaceChildren(el('p', 'No learnings match these filters.', 'muted')); return; }
        table(lhost, 'project-learnings', 'Learnings', [
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
        ], shown);
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
    UI.controlsEnabled().then(on => { s.writes = !!on; if (s.tab === 'findings' && s.findings) redraw(); });
    open();
    return {
      go(id, tab) {
        const t = TABS.includes(tab) ? tab : 'runs';
        if (id === s.id && t === s.tab) return;             // the page's own refresh: nothing changed here
        const same = id === s.id;
        s.id = id || null; s.tab = t;
        if (!same) { s.detail = s.findings = s.memories = null; s.source = null; s.checkout = ''; }
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

  return { render, cleanSummary, filterFindings, filterLearnings, stateFor, memoryEmpty, routeOf, STATUSES, STATES };
});
