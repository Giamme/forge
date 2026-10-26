/* forge-ui open findings: the findings a run (or every run of its repo) left open, to dismiss or to file as GitHub issues
   (window.ForgeFindings, and a CommonJS module under node:test). Route #/runs/<key>/findings.
   The pure helpers (status, filters, selection rules, request URLs and bodies, merging the online `filed` data) are
   tested in ui/tests/js/findingsview.test.js; render(host, ctx) is thin DOM over them. Finding and issue texts are
   untrusted: they only ever enter the page as text.
   ctx = { key, run? (the cached run, for the title), back (href of the run), tablePrefs?(id), saveTablePrefs?(id, prefs),
           onCount?(openRows, scope) }. The write calls are ForgeUI's; without controls the page shows no write control. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeFindings = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const MAX_FPS = 25, MAX_LABELS = 5, MAX_LABEL = 50, MAX_REASON = 500;
  const list = v => Array.isArray(v) ? v : [];
  const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
  const ORIGINS = ['tolerated', 'accepted', 'review', 'plausible'];
  const STATUSES = ['open', 'filed', 'fixed', 'dismissed'];

  /* ---------- pure ---------- */
  // open | filed (its issue is OPEN, or its state is not known yet) | fixed (CLOSED) | dismissed. A dismissal wins, so
  // that an Undismiss reaches every dismissed row.
  function statusOf(f) {
    if (f && f.dismissed) return 'dismissed';
    if (f && f.filed) return String(f.filed.state || '').toUpperCase() === 'CLOSED' ? 'fixed' : 'filed';
    return 'open';
  }
  // filter = { status: 'open' (default) | 'all' | one of STATUSES, severity: 'all' | P0..P3, origin: 'all' | ORIGINS }
  function filterRows(rows, filter) {
    const f = filter || {}, status = f.status || 'open', severity = f.severity || 'all', origin = f.origin || 'all';
    // `dismissed` (optional): false hides dismissed rows whatever the status; true adds them to the default 'open' view.
    return list(rows).filter(r => {
      const st = statusOf(r);
      if (f.dismissed === false && st === 'dismissed') return false;
      const byStatus = status === 'all' || st === status || (f.dismissed === true && status === 'open' && st === 'dismissed');
      return byStatus && (severity === 'all' || r.severity === severity) && (origin === 'all' || r.origin === origin);
    });
  }
  // The online listing knows which findings have an issue: its `filed` goes into the offline rows by fingerprint.
  // A row only the online listing has is added; a row only the offline listing has stays as it was.
  function mergeFiled(offline, online) {
    const by = new Map(list(online).map(r => [r.fp, r]));
    const seen = new Set();
    const rows = list(offline).map(r => {
      seen.add(r.fp);
      const o = by.get(r.fp);
      return o ? { ...r, filed: o.filed || null } : r;
    });
    for (const r of list(online)) if (!seen.has(r.fp)) rows.push(r);
    return rows;
  }
  // Which rows an action takes: filing and dismissing take open rows, Undismiss takes dismissed rows.
  function selectable(action, row) {
    const status = statusOf(row);
    return action === 'undismiss' ? status === 'dismissed' : (action === 'file' || action === 'dismiss') && status === 'open';
  }
  // What `selected` (a Set of fingerprints) means for one action: the fingerprints it takes, and whether the button
  // is enabled (1 to 25) with the note to show when it is not.
  function selection(action, rows, selected) {
    const fps = list(rows).filter(r => selected.has(r.fp) && selectable(action, r)).map(r => r.fp);
    const n = fps.length;
    const note = n > MAX_FPS ? `${n} selected: at most ${MAX_FPS} per action` : '';
    return { fps, count: n, enabled: n >= 1 && n <= MAX_FPS, note };
  }
  const bit = v => v ? '1' : '0';
  // The GET path: `o` = { scope, offline, plausible, dismissed, only (fingerprints) }.
  function issuesUrl(key, o) {
    const q = [`scope=${o.scope === 'repo' ? 'repo' : 'run'}`, `offline=${bit(o.offline)}`, `plausible=${bit(o.plausible)}`];
    if (o.dismissed) q.push('dismissed=1');
    if (list(o.only).length) q.push('only=' + o.only.join(','));
    return `/api/runs/${encodeURIComponent(key)}/issues?${q.join('&')}`;
  }
  const issuesPath = key => `/api/runs/${encodeURIComponent(key)}/issues`;
  const dismissPath = key => issuesPath(key) + '/dismiss';
  // "a, b" → {labels, error}: at most 5, each 1 to 50 characters, no control character, not starting with "-".
  function parseLabels(text) {
    const labels = String(text || '').split(',').map(s => s.trim()).filter(Boolean);
    if (labels.length > MAX_LABELS) return { labels: [], error: `at most ${MAX_LABELS} labels` };
    for (const l of labels) {
      if (l.length > MAX_LABEL) return { labels: [], error: `a label is at most ${MAX_LABEL} characters` };
      if (l.startsWith('-')) return { labels: [], error: 'a label cannot start with "-"' };
      if (/[\u0000-\u001f\u007f]/.test(l)) return { labels: [], error: 'a label cannot hold control characters' };
    }
    return { labels, error: null };
  }
  function fileBody(scope, fps, includePlausible, labels) {
    return { scope: scope === 'repo' ? 'repo' : 'run', fingerprints: list(fps).slice(), include_plausible: !!includePlausible, labels: list(labels).slice() };
  }
  // `reason` goes with a dismissal only; Undismiss has none.
  function dismissBody(fps, reason, undo) {
    const body = { fingerprints: list(fps).slice() };
    if (undo) { body.undo = true; return body; }
    const r = String(reason || '').trim();
    if (r) body.reason = r.slice(0, MAX_REASON);
    return body;
  }
  // The POST's `created` as lines: {kind: 'created' | 'skipped' | 'note', text, url?}
  function resultLines(created) {
    return list(created).map(c => {
      if (c && c.url) return { kind: 'created', text: c.title || c.url, url: c.url };
      if (c && c.skipped) return { kind: 'skipped', text: `${c.title || 'finding'}: skipped, ${c.skipped}`, url: c.issue || null };
      return { kind: 'note', text: c && c.note ? c.note : 'no detail' };
    });
  }
  // The CLI's `notes` as lines for the dialog, only when the POST created nothing: a bare "0 created" says no reason, the
  // notes ("--only fp: no such open finding") do. When something was created the notes are not about the dialog.
  function emptyNotes(created, notes) {
    return list(created).some(c => c && c.url) ? [] : list(notes).map(n => String(n)).filter(Boolean);
  }
  // An untrusted URL becomes a link only when it is a plain https address.
  const safeUrl = u => typeof u === 'string' && /^https:\/\/[^\s"'<>]+$/.test(u) ? u : null;
  // The marker for a finding whose whole-run review is outdated, or null. The lamp is the hollow unlit
  // `.st-outdated`. `title` is the same sentence the issue body adds, ids cut after 5, so a row can show it
  // without building HTML. A current review, and any finding that is not from a whole-run review, has none.
  function reviewMarker(row) {
    const review = row && row.review;
    if (!review || review.outdated !== true) return null;
    const ids = list(review.merged_since).map(id => String(id));
    const shown = ids.slice(0, 5), extra = ids.length - shown.length;
    const names = shown.join(', ') + (extra > 0 ? ', +' + extra + ' more' : '');
    const n = ids.length;
    const sentence = `The review is outdated: ${n} ${n === 1 ? 'task' : 'tasks'} merged into the integration branch after it`
      + (names ? ` (${names})` : '') + ', so check whether they fixed it first.';
    return { lamp: 'st-outdated', label: 'outdated review', title: sentence, sentence };
  }

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI;
    const el = (tag, text, cls) => UI.el(tag, text, cls);
    const s = { scope: 'run', plausible: false, dismissed: false, filter: { status: 'open', severity: 'all', origin: 'all' },
      rows: [], selected: new Set(), seq: 0, writes: false, busy: false };
    const key = ctx.key;

    const head = el('div', undefined, 'fnd-head');
    const title = el('h1', 'Open findings', 'module-title');
    const back = el('a', 'Back to the run', 'fnd-back'); back.href = ctx.back || '#/';
    head.append(title, back);
    const lede = el('p', undefined, 'muted fnd-lede');
    const scopeBar = el('div', undefined, 'seg fnd-scope'); scopeBar.setAttribute('role', 'group'); scopeBar.setAttribute('aria-label', 'Scope');
    const scopeKeys = {};
    for (const [v, label] of [['run', 'This run'], ['repo', 'All runs of this repo']]) {
      const b = el('button', label); b.type = 'button';
      b.addEventListener('click', () => { if (s.scope === v) return; s.scope = v; s.selected.clear(); load(); });
      scopeKeys[v] = b; scopeBar.append(b);
    }
    const lamp = el('p', undefined, 'fnd-lamp'); lamp.setAttribute('role', 'status');
    const notes = el('div', undefined, 'fnd-notes muted');

    // filters
    const filters = el('div', undefined, 'fnd-filters');
    const select = (label, options, get, set) => {
      const l = el('label', undefined, 'fnd-field'), sel = el('select');
      for (const [v, t] of options) { const o = el('option', t); o.value = v; sel.append(o); }
      sel.value = get();
      sel.addEventListener('change', () => { set(sel.value); drawTable(); });
      l.append(el('span', label, 'plate'), sel);
      return l;
    };
    const toggle = (label, get, set) => {
      const l = el('label', undefined, 'fnd-toggle'), c = el('input');
      c.type = 'checkbox'; c.checked = get();
      c.addEventListener('change', () => { set(c.checked); load(); });
      l.append(c, el('span', label));
      return l;
    };
    filters.append(
      select('Status', [['open', 'open'], ['all', 'all'], ...STATUSES.slice(1).map(x => [x, x])], () => s.filter.status, v => { s.filter.status = v; }),
      select('Severity', [['all', 'all'], ...SEVERITIES.map(x => [x, x])], () => s.filter.severity, v => { s.filter.severity = v; }),
      select('Origin', [['all', 'all'], ...ORIGINS.map(x => [x, x])], () => s.filter.origin, v => { s.filter.origin = v; }),
      toggle('Include plausible', () => s.plausible, v => { s.plausible = v; }),
      toggle('Show dismissed', () => s.dismissed, v => { s.dismissed = v; }));

    // actions (only with controls)
    const actions = el('div', undefined, 'fnd-actions'); actions.hidden = true;
    actions.setAttribute('role', 'group'); actions.setAttribute('aria-label', 'Actions on the selected findings');
    const key_ = (text, cls, fn) => { const b = el('button', text, cls); b.type = 'button'; b.addEventListener('click', fn); return b; };
    const fileKey = key_('File issues', 'primary', () => openFile());
    const dismissKey = key_('Dismiss', undefined, () => openDismiss());
    const undismissKey = key_('Undismiss', undefined, () => doDismiss(true, ''));
    const allKey = key_('Select shown open', undefined, () => {
      for (const r of shown()) if (selectable('file', r) && s.selected.size < MAX_FPS) s.selected.add(r.fp);
      drawTable(); syncActions();
    });
    const noneKey = key_('Clear selection', undefined, () => { s.selected.clear(); drawTable(); syncActions(); });
    const actionNote = el('span', undefined, 'fnd-note muted'); actionNote.setAttribute('aria-live', 'polite');
    actions.append(allKey, noneKey, fileKey, dismissKey, undismissKey, actionNote);
    const reasonHost = el('div', undefined, 'fnd-reason');
    const outcomeHost = el('div', undefined, 'fnd-outcome');
    const tableHost = el('div', undefined, 'fnd-table');
    const readOnly = el('p', 'read-only: findings can be read here, not filed or dismissed.', 'muted fnd-readonly'); readOnly.hidden = true;

    host.replaceChildren(head, lede, scopeBar, lamp, notes, filters, readOnly, actions, reasonHost, outcomeHost, tableHost);
    let posting = false;   // a filing POST is running: the dialog stays open until its results are shown
    const dlg = el('dialog', undefined, 'fnd-dialog'); dlg.setAttribute('aria-label', 'File issues on GitHub');
    host.append(dlg);

    const shown = () => filterRows(s.rows, { ...s.filter, dismissed: s.dismissed });
    // What an action takes: the selected rows the filters leave visible. Rows hidden by a filter stay in s.selected, so
    // they come back with the filter, but no button counts them and no action reaches them.
    const picked = action => selection(action, shown(), s.selected);
    function syncScope() {
      for (const [v, b] of Object.entries(scopeKeys)) b.setAttribute('aria-pressed', String(s.scope === v));
      lede.textContent = s.scope === 'run' ? `Findings this run left unfixed${ctx.run && ctx.run.goal ? ': ' + String(ctx.run.goal).split('\n')[0] : ''}.`
        : 'Findings every run of this repository left unfixed.';
    }
    function syncActions() {
      actions.hidden = !s.writes;
      if (!s.writes) return;
      const f = picked('file'), d = picked('dismiss'), u = picked('undismiss');
      fileKey.textContent = f.count ? `File ${f.count} issue${f.count === 1 ? '' : 's'}` : 'File issues';
      fileKey.disabled = !f.enabled || s.busy; dismissKey.disabled = !d.enabled || s.busy;
      undismissKey.textContent = u.count ? `Undismiss ${u.count}` : 'Undismiss'; undismissKey.disabled = !u.enabled || s.busy;
      actionNote.textContent = f.note || d.note || u.note || (f.count + u.count ? `${f.count} open, ${u.count} dismissed selected` : '');
    }
    function setLamp(cls, text) {
      lamp.replaceChildren(el('i', undefined, 'lamp ' + cls), el('span', text));
    }

    // cells
    const originCell = r => {
      const marker = reviewMarker(r);
      if (!marker) return r.origin || '—';
      const box = el('span', undefined, 'fnd-status');
      const lamp = el('i', undefined, 'lamp ' + marker.lamp);
      lamp.title = marker.title;
      box.title = marker.title;
      box.append(lamp, el('span', r.origin || '—'), el('span', marker.label));
      return box;
    };
    const statusCell = r => {
      const st = statusOf(r), box = el('span', undefined, 'fnd-status fnd-' + st);
      box.append(el('i', undefined, 'lamp ' + (st === 'filed' ? 'fnd-amber' : st === 'fixed' ? 'st-pass' : 'lamp-off')));
      if (st === 'dismissed') { box.title = r.dismissed.reason ? String(r.dismissed.reason) : 'dismissed'; box.append(el('span', 'dismissed')); return box; }
      if (st === 'open') { box.append(el('span', 'open')); return box; }
      const url = safeUrl(r.filed && r.filed.url), word = st === 'filed' ? 'filed' : 'fixed';
      if (url) { const a = el('a', word); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.title = 'the GitHub issue (' + String(r.filed.state || 'state unknown').toLowerCase() + ')'; box.append(a); }
      else box.append(el('span', word));
      return box;
    };
    const selectCell = r => {
      const c = el('input'); c.type = 'checkbox';
      c.setAttribute('aria-label', 'Select finding: ' + String(r.summary || r.fp).slice(0, 80));
      c.checked = s.selected.has(r.fp);
      c.disabled = !selectable('file', r) && !selectable('undismiss', r);
      c.addEventListener('change', () => { if (c.checked) s.selected.add(r.fp); else s.selected.delete(r.fp); syncActions(); });
      return c;
    };
    const summaryCell = r => {
      const box = el('div', undefined, 'fnd-summary'), line = el('div', undefined, 'fnd-summary-line');
      const more = el('button', undefined, 'fnd-more'); more.type = 'button';
      more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-label', 'Show the full text');
      more.append(el('span', '▸', 'fnd-caret'));
      const detail = el('div', undefined, 'fnd-detail'); detail.hidden = true;
      detail.append(el('pre', r.text || r.summary || '', 'fnd-text'));
      const marker = reviewMarker(r);
      if (marker) detail.append(el('p', marker.sentence, 'muted'));
      const meta = el('dl', undefined, 'fnd-meta');
      for (const [k, v] of [['Reviewer', r.reviewer], ['Commit', r.commit], ['Fingerprint', r.fp], ['Plan', r.plan_dir || r.plan],
        ['Dismissed', r.dismissed ? `${r.dismissed.at || ''} ${r.dismissed.reason || ''}`.trim() : null]]) {
        if (v) meta.append(el('dt', k), el('dd', v));
      }
      detail.append(meta);
      more.addEventListener('click', () => {
        const open = detail.hidden; detail.hidden = !open;
        more.setAttribute('aria-expanded', String(open)); more.setAttribute('aria-label', open ? 'Hide the full text' : 'Show the full text');
        more.firstChild.textContent = open ? '▾' : '▸';
      });
      line.append(more, el('span', r.summary || r.text || '—'));
      box.append(line, detail);
      return box;
    };
    const sevCell = r => el('span', r.severity || '—', 'chip sev-chip sev-' + (SEVERITIES.includes(r.severity) ? r.severity : 'none'));
    const columns = () => {
      const cols = [];
      if (s.writes) cols.push({ id: 'select', label: 'Select', sortable: false, hideable: false, cell: selectCell });
      cols.push(
        { id: 'severity', label: 'Severity', value: r => SEVERITIES.indexOf(r.severity), cell: sevCell },
        { id: 'confidence', label: 'Confidence', value: r => r.confidence, cell: r => r.confidence || '—' },
        { id: 'origin', label: 'Origin', value: r => r.origin, cell: originCell },
        { id: 'task', label: 'Task', value: r => r.task, mono: true, cell: r => r.task || 'whole run' });
      if (s.scope === 'repo') cols.push({ id: 'plan', label: 'Plan', value: r => r.plan, mono: true, cell: r => r.plan || '—' });
      cols.push(
        { id: 'file', label: 'File', value: r => r.file, mono: true, cell: r => r.file || '—' },
        { id: 'summary', label: 'Summary', value: r => r.summary, cell: summaryCell },
        { id: 'status', label: 'Status', value: r => STATUSES.indexOf(statusOf(r)), cell: statusCell });
      return cols;
    };
    function drawTable() {
      const rows = shown();
      if (!rows.length) {
        const total = s.rows.length;
        tableHost.replaceChildren(el('p', total ? `No findings match these filters (${total} hidden).` : 'No findings. Nothing here needs fixing.', 'muted fnd-empty'));
        syncActions();
        return;
      }
      const id = 'findings-' + (s.scope === 'repo' ? 'repo' : 'run') + (s.writes ? '-w' : '');
      tableHost.replaceChildren();
      root.ForgeTable.render(tableHost, { id, label: 'Open findings', columns: columns(), rows, className: 'fnd-grid',
        prefs: ctx.tablePrefs ? ctx.tablePrefs(id) : null, savePrefs: p => ctx.saveTablePrefs && ctx.saveTablePrefs(id, p) });
      syncActions();
    }

    // loading: offline first (fast), then online for `filed`
    const detailOf = res => res.detail || res.error || 'no answer';
    async function load() {
      const mine = ++s.seq;
      syncScope();
      setLamp('lamp-route', 'reading the findings…');
      const q = { scope: s.scope, plausible: s.plausible, dismissed: s.dismissed };
      const off = await UI.get(issuesUrl(key, { ...q, offline: true }));
      if (mine !== s.seq) return;
      if (!off.ok) {
        s.rows = []; drawTable();
        setLamp('st-fail', 'could not read the findings'); outcomeHost.replaceChildren(); UI.outcome(outcomeHost, off);
        return;
      }
      s.rows = list(off.data.findings);
      notes.replaceChildren(...list(off.data.notes).map(n => el('p', String(n))));
      prune(); drawTable(); report();
      setLamp('st-running', 'checking GitHub…');
      const on = await UI.get(issuesUrl(key, { ...q, offline: false }));
      if (mine !== s.seq) return;
      if (!on.ok) { setLamp('st-fail', 'GitHub status unknown: ' + detailOf(on)); return; }
      s.rows = mergeFiled(s.rows, list(on.data.findings));
      notes.replaceChildren(...list(on.data.notes).map(n => el('p', String(n))));
      prune(); drawTable(); report();
      setLamp('st-pass', 'GitHub status checked');
    }
    function prune() {
      const fps = new Set(s.rows.map(r => r.fp));
      for (const fp of Array.from(s.selected)) if (!fps.has(fp)) s.selected.delete(fp);
    }
    function report() { if (ctx.onCount) ctx.onCount(s.rows.filter(r => statusOf(r) === 'open').length, s.scope); }

    // dismiss: an optional reason, then the call
    async function openDismiss() {
      reasonHost.replaceChildren();
      const sel = picked('dismiss');
      if (!sel.enabled) return;
      const l = el('label', undefined, 'fnd-field fnd-reason-field'), input = el('input');
      input.type = 'text'; input.maxLength = MAX_REASON; input.placeholder = 'why (optional)';
      l.append(el('span', `Reason for dismissing ${sel.count}`, 'plate'), input);
      const go = key_('Dismiss', 'ui-danger', () => { const reason = input.value; reasonHost.replaceChildren(); doDismiss(false, reason); });
      const cancel = key_('Cancel', undefined, () => { reasonHost.replaceChildren(); dismissKey.focus(); });
      input.addEventListener('keydown', e => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel.click(); }
        else if (e.key === 'Enter') { e.preventDefault(); go.click(); }
      });
      reasonHost.append(l, go, cancel);
      input.focus();
    }
    async function doDismiss(undo, reason) {
      const sel = picked(undo ? 'undismiss' : 'dismiss');
      if (!sel.enabled || s.busy) return;
      s.busy = true; syncActions(); outcomeHost.replaceChildren();
      const res = await UI.post(dismissPath(key), dismissBody(sel.fps, reason, undo));
      s.busy = false;
      UI.outcome(outcomeHost, res, undo ? `restored ${sel.count}` : `dismissed ${sel.count}`);
      if (res.ok) { for (const fp of sel.fps) s.selected.delete(fp); await load(); } else syncActions();
    }

    // filing: drafts for exactly the selection, a preview, then the POST
    async function openFile() {
      const sel = picked('file');
      if (!sel.enabled || s.busy) return;
      s.busy = true; syncActions(); outcomeHost.replaceChildren();
      setLamp('st-running', 'preparing the issue drafts…');
      // The scope and the toggle stay usable while the drafts are made: the preview files what these drafts were made for.
      const scope = s.scope, plausible = s.plausible;
      const res = await UI.get(issuesUrl(key, { scope, offline: false, plausible, only: sel.fps }));
      s.busy = false; syncActions();
      if (!res.ok) { setLamp('st-fail', 'could not prepare the drafts'); UI.outcome(outcomeHost, res); return; }
      setLamp('st-pass', 'drafts ready');
      preview(sel.fps, list(res.data.drafts), { scope, plausible });
    }
    function preview(fps, drafts, made) {
      dlg.replaceChildren();
      dlg.append(el('h2', 'File issues on GitHub'));
      const warn = el('p', undefined, 'fnd-warn');
      warn.append(el('i', undefined, 'lamp st-running'),
        el('span', `This creates ${drafts.length} issue${drafts.length === 1 ? '' : 's'} on this run's GitHub repository, from your gh login, and cannot be undone from here.`));
      dlg.append(warn);
      if (!drafts.length) dlg.append(el('p', 'forge has no draft for this selection (its findings may be filed already).', 'muted'));
      for (const d of drafts) {
        const art = el('article', undefined, 'fnd-draft');
        const h = el('h3', undefined, 'fnd-draft-title');
        h.append(el('span', d.severity || '—', 'chip sev-chip sev-' + (SEVERITIES.includes(d.severity) ? d.severity : 'none')), el('span', d.title || 'untitled'));
        art.append(h);
        if (d.same_title) {
          const sameUrl = safeUrl(d.same_title);
          const p = el('p', undefined, 'fnd-same'); p.append(el('i', undefined, 'lamp fnd-amber'), el('span', 'An open issue with this title exists already: '));
          if (sameUrl) { const a = el('a', sameUrl); a.href = sameUrl; a.target = '_blank'; a.rel = 'noopener noreferrer'; p.append(a); } else p.append(el('span', String(d.same_title)));
          p.append(el('span', '. forge skips it.'));
          art.append(p);
        }
        art.append(el('pre', d.body || '', 'fnd-body'));
        dlg.append(art);
      }
      const lab = el('label', undefined, 'fnd-field'), labels = el('input');
      labels.type = 'text'; labels.placeholder = 'bug, forge (optional, comma separated)';
      lab.append(el('span', 'Labels', 'plate'), labels);
      const labelNote = el('p', undefined, 'warn fnd-label-note'); labelNote.hidden = true;
      const results = el('div', undefined, 'fnd-results');
      const go = key_('File on GitHub', 'primary', async () => {
        const parsed = parseLabels(labels.value);
        labelNote.hidden = !parsed.error; labelNote.textContent = parsed.error || '';
        if (parsed.error) return;
        go.disabled = true; cancel.disabled = true; labels.disabled = true; results.replaceChildren(); posting = true;
        const res = await UI.post(issuesPath(key), fileBody(made.scope, fps, made.plausible, parsed.labels));
        posting = false;
        if (!res.ok) { go.disabled = false; cancel.disabled = false; labels.disabled = false; UI.outcome(results, res); return; }
        const lines = resultLines(res.data.created), ul = el('ul', undefined, 'fnd-created');
        for (const ln of lines) {
          const li = el('li', undefined, 'fnd-created-' + ln.kind), u = safeUrl(ln.url);
          li.append(el('i', undefined, 'lamp ' + (ln.kind === 'created' ? 'st-pass' : 'fnd-amber')));
          if (u) { const a = el('a', ln.text); a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer'; li.append(a); } else li.append(el('span', ln.text));
          ul.append(li);
        }
        results.replaceChildren(el('p', `${lines.filter(l => l.kind === 'created').length} created`, 'fnd-done'), ul);
        for (const n of emptyNotes(res.data.created, res.data.notes)) results.append(el('p', n, 'muted fnd-result-note'));
        go.remove(); cancel.textContent = 'Close'; cancel.disabled = false;
        for (const fp of fps) s.selected.delete(fp);
        load();
      });
      const cancel = key_('Cancel', undefined, () => dlg.close());
      go.disabled = !drafts.length;
      const row = el('div', undefined, 'fnd-dialog-actions'); row.append(cancel, go);
      dlg.append(lab, labelNote, results, row);
      dlg.showModal(); (drafts.length ? go : cancel).focus();
    }
    dlg.addEventListener('click', e => { if (e.target === dlg && !posting) dlg.close(); });
    dlg.addEventListener('cancel', e => { if (posting) e.preventDefault(); });
    dlg.addEventListener('close', () => { fileKey.focus(); });

    syncScope();
    UI.controlsEnabled().then(on => {
      s.writes = !!on; readOnly.hidden = on; syncActions();
      if (s.rows.length) drawTable();
    });
    load();
    return { reload: load };
  }

  return { render, statusOf, filterRows, mergeFiled, selectable, selection, issuesUrl, issuesPath, dismissPath, fileBody, dismissBody,
    parseLabels, resultLines, emptyNotes, safeUrl, reviewMarker, ORIGINS, MAX_FPS };
});
