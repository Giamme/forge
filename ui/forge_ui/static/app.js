/* forge-ui: read-only API viewer. All API values enter the DOM as text. */
window.ForgeUI = (() => {
  'use strict';
  const state = { runs: [], listText: null, cache: new Map(), key: null, task: null,
    filter: '', paused: false, errors: new Map(), pending: new Map() };
  const $ = id => document.getElementById(id);
  const display = value => value === null || value === undefined || value === '' ? '—' :
    typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  const list = value => Array.isArray(value) ? value : [];
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = display(text);
    if (className) node.className = className;
    return node;
  }
  function route(key, task) {
    return '#/runs/' + encodeURIComponent(key) + (task == null ? '' : '/tasks/' + encodeURIComponent(task));
  }
  function link(text, key, task) {
    const node = el('a', text);
    node.href = route(key, task);
    return node;
  }
  const statuses = new Set(['merged', 'pass', 'running', 'pending', 'fail', 'error', 'infra',
    'timeout', 'unknown', 'invalidated', 'blocked', 'interrupted', 'nochanges', 'conflict']);
  function chip(value) {
    const status = String(value || 'unknown').toLowerCase();
    return el('span', value || '—', 'chip st-' + (statuses.has(status) ? status : 'unknown'));
  }
  function time(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
  }
  function relative(value) {
    if (value == null || !Number.isFinite(Number(value))) return 'updated —';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(value)));
    if (seconds < 60) return 'updated just now';
    if (seconds < 3600) return `updated ${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `updated ${Math.floor(seconds / 3600)}h ago`;
    return `updated ${Math.floor(seconds / 86400)}d ago`;
  }
  function basename(path) { return path ? String(path).split(/[\\/]/).filter(Boolean).pop() || path : '—'; }
  function section(parent, title) {
    const node = el('section');
    node.append(el('h2', title));
    parent.append(node);
    return node;
  }
  function fields(parent, entries) {
    const dl = el('dl', undefined, 'fields');
    for (const [label, value, mono] of entries) {
      dl.append(el('dt', label));
      const dd = el('dd', undefined, mono ? 'mono' : '');
      dd.append(value instanceof Node ? value : document.createTextNode(display(value)));
      dl.append(dd);
    }
    parent.append(dl);
  }
  function details(parent, title, value, key) {
    const node = el('details');
    node.dataset.keep = key;
    const pre = el('pre', value);
    pre.dataset.scroll = key + '-text';
    node.append(el('summary', title), pre);
    parent.append(node);
    return node;
  }
  function table(parent, headers, rows, key) {
    const wrap = el('div', undefined, 'table-scroll');
    wrap.dataset.scroll = key;
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', key);
    const node = el('table');
    const head = el('tr');
    for (const label of headers) {
      const th = el('th', label);
      th.scope = 'col';
      head.append(th);
    }
    const thead = el('thead');
    thead.append(head);
    const body = el('tbody');
    for (const cells of rows) {
      const tr = el('tr');
      for (const [index, value] of cells.entries()) {
        const mono = ['ID', 'Dwarf', 'QA', 'Branch', 'Default', 'Chosen', 'Spec', 'Primary', 'Decision', 'From → to'].includes(headers[index]);
        const td = el('td', undefined, mono ? 'mono' : '');
        td.append(value instanceof Node ? value : document.createTextNode(display(value)));
        tr.append(td);
      }
      body.append(tr);
    }
    node.append(thead, body);
    wrap.append(node);
    Array.from(wrap.querySelectorAll('pre')).forEach((pre, i) => {
      pre.dataset.scroll = `${key}-text-${i}`;
    });
    parent.append(wrap);
    return body;
  }
  // Restore disclosures, internal scroll and keyboard focus after changed data arrives.
  function preserve(parent, build, { restore = true, restorePage = true } = {}) {
    const open = new Map(Array.from(parent.querySelectorAll('details[data-keep]'), n => [n.dataset.keep, n.open]));
    const scroll = new Map(Array.from(parent.querySelectorAll('[data-scroll]'), n => [n.dataset.scroll, [n.scrollLeft, n.scrollTop]]));
    const active = parent.contains(document.activeElement) ? document.activeElement : null;
    const focusHref = active?.getAttribute('href');
    const page = [window.scrollX, window.scrollY];
    parent.replaceChildren();
    build();
    if (!restore) return;
    for (const node of parent.querySelectorAll('details[data-keep]')) {
      if (open.has(node.dataset.keep)) node.open = open.get(node.dataset.keep);
    }
    for (const node of parent.querySelectorAll('[data-scroll]')) {
      const pos = scroll.get(node.dataset.scroll);
      if (pos) { node.scrollLeft = pos[0]; node.scrollTop = pos[1]; }
    }
    if (focusHref) {
      const target = Array.from(parent.querySelectorAll('a')).find(n => n.getAttribute('href') === focusHref);
      target?.focus({ preventScroll: true });
    }
    if (restorePage) window.scrollTo(page[0], page[1]);
  }
  function currentResource(path) {
    return path === '/api/runs' || (state.key != null && path === '/api/runs/' + encodeURIComponent(state.key));
  }
  function renderErrors() {
    for (const path of state.errors.keys()) {
      if (!currentResource(path)) state.errors.delete(path);
    }
    const banner = $('errors');
    banner.textContent = Array.from(state.errors.values()).join(' · ');
    banner.hidden = !state.errors.size;
  }
  function renderList() {
    const query = state.filter.toLowerCase();
    const runs = state.runs.filter(run => [run.goal, run.repo, run.run_id, run.path]
      .some(value => String(value || '').toLowerCase().includes(query)))
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
    $('run-count').textContent = `${runs.length} / ${state.runs.length}`;
    preserve($('run-list'), () => {
      if (!runs.length) {
        const empty = el('div', undefined, 'empty');
        empty.append(el('p', state.runs.length ? 'No runs match this filter.' : 'No runs found.'));
        if (!state.runs.length) {
          empty.append(el('p', 'Sources: explicit --plan directories, --root directories (up to two levels deep), and the forge run registry at $XDG_CONFIG_HOME/forge/runs/ (default ~/.config/forge/runs/).'));
          empty.append(el('p', 'Point the server at your runs:'), el('pre', 'python3 -m forge_ui --root <dir>'));
        }
        $('run-list').append(empty);
      }
      for (const run of runs) {
        const item = link(undefined, run.key);
        item.className = 'run-item';
        item.dataset.run = run.key;
        const goal = el('strong', String(run.goal || 'Untitled run').split('\n')[0], 'run-goal');
        goal.title = run.goal || 'Untitled run';
        const solo = run.kind === 'solo';
        item.append(goal, el('span', basename(run.repo), 'repo'), el('span', run.run_id, 'mono'));
        if (solo) item.append(el('span', 'solo', 'tag'));
        const updated = el('span', relative(run.updated), 'muted updated');
        if (run.updated != null) updated.dataset.epoch = run.updated;
        updated.title = time(run.updated);
        item.append(updated);
        const counts = el('div', undefined, 'chips');
        for (const [status, count] of Object.entries(run.counts || {})) {
          const badge = chip(status);
          badge.append(document.createTextNode(' ' + display(count)));
          counts.append(badge);
        }
        item.append(counts);
        if (!solo) {
          const verification = el('span', undefined, 'verification');
          verification.append(document.createTextNode('Verification '), chip(run.verification_status));
          item.append(verification);
        }
        $('run-list').append(item);
      }
    });
    markSelection();
  }
  function markSelection() {
    for (const node of $('run-list').querySelectorAll('[data-run]')) {
      const selected = node.dataset.run === state.key;
      node.classList.toggle('selected', selected);
      if (selected) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
    }
  }
  function depLinks(deps, key) {
    const node = el('span', undefined, 'mono');
    list(deps).forEach((id, index) => {
      if (index) node.append(document.createTextNode(', '));
      node.append(link(id, key, id));
    });
    if (!node.hasChildNodes()) node.textContent = '—';
    return node;
  }
  function renderRun(run) {
    const main = $('main');
    const sameRun = main.dataset.run === run.key;
    const taskChanged = main.dataset.task !== (state.task ?? '');
    const solo = run.kind === 'solo';
    preserve(main, () => {
      const header = section(main, run.goal || 'Untitled run');
      header.className = 'run-header';
      if (solo) {
        // A solo run is one task: header and the task panel inline, no table, graph or waves.
        const only = list(run.tasks)[0];
        fields(header, [['Repo', run.repo, true], ['Status', chip(only?.status), false], ['Run ID', run.run_id, true],
          ['Path', run.path, true], ['Updated', time(run.updated)]]);
        details(header, 'Request', run.request, 'run-request');
        if (list(run.warnings).length) details(header, `Reader warnings (${run.warnings.length})`, run.warnings.join('\n'), 'warnings');
        if (list(run.truncated).length) details(header, 'Truncated fields', run.truncated.join('\n'), 'truncated');
        if (only) renderTask(main, only, run);
        else main.append(el('p', 'This run has no task data yet.', 'empty'));
        scopeSaved(run);
        return;
      }
      fields(header, [ ['Repo', run.repo, true], ['Run ID', run.run_id, true], ['Plan path', run.path, true],
        ['Source', run.source], ['Decompose', run.decompose_level], ['Updated', time(run.updated)],
        ['Task count', run.task_count], ['Counts', run.counts], ['Explore', run.settings?.explore],
        ['Shadow rate', run.settings?.shadow_rate], ['Shadow reviewers', list(run.settings?.shadow_qa).join(', '), true],
        ['Timeout (seconds)', run.settings?.timeout], ['Verification', chip(run.verification?.status || run.verification_status)],
        ['Command', run.verification?.command, true] ]);
      details(header, 'Request', run.request, 'run-request');
      if (run.verification?.log_tail != null) details(header, 'Verification output', run.verification.log_tail, 'verification-output');
      if (list(run.warnings).length) details(header, `Reader warnings (${run.warnings.length})`, run.warnings.join('\n'), 'warnings');
      if (list(run.truncated).length) details(header, 'Truncated fields', run.truncated.join('\n'), 'truncated');
      details(header, 'Waves', list(run.waves).map((ids, i) => `${i + 1}: ${list(ids).join(', ')}`).join('\n') || '—', 'waves');
      const graphSection = section(main, 'Dependencies');
      const graph = el('div', undefined, 'graph');
      graph.dataset.scroll = 'graph';
      graphSection.append(graph);
      if (typeof window.ForgeGraph?.render === 'function') {
        try { window.ForgeGraph.render(graph, run, { selected: state.task, onSelect: id => { location.hash = route(state.key, id); } }); }
        catch (_) { graph.replaceChildren(el('p', 'Graph unavailable. Select tasks in the table below.', 'muted')); }
      } else graph.append(el('p', 'Graph unavailable. Select tasks in the table below.', 'muted'));
      const taskSection = section(main, `Tasks (${list(run.tasks).length})`);
      if (!list(run.tasks).length) taskSection.append(el('p', 'This run has no tasks yet.', 'empty'));
      else renderTasks(taskSection, run);
      if (state.task != null) {
        const task = list(run.tasks).find(t => t.id === state.task);
        if (task) renderTask(main, task, run);
        else main.append(el('p', `Task ${state.task} was not found in this run.`, 'empty'));
      }
      scopeSaved(run);
    }, { restore: sameRun, restorePage: sameRun && !taskChanged });
    main.dataset.run = run.key;
    main.dataset.task = state.task ?? '';
    if (!sameRun) window.scrollTo(0, 0);
    if (!solo && (!sameRun || taskChanged) && $('task-panel')) {
      const panel = $('task-panel');
      panel.tabIndex = -1;
      panel.focus({ preventScroll: true });
      panel.scrollIntoView({ block: 'start' });
    }
  }
  // Scope saved state to its run and, inside the panel, its task.
  function scopeSaved(run) {
    for (const node of $('main').querySelectorAll('[data-keep], [data-scroll]')) {
      const scope = JSON.stringify([run.key, node.closest('#task-panel') ? state.task : null]);
      if (node.dataset.keep) node.dataset.keep = scope + ':' + node.dataset.keep;
      if (node.dataset.scroll) node.dataset.scroll = scope + ':' + node.dataset.scroll;
    }
  }
  function renderTasks(parent, run) {
    const rows = list(run.tasks).map(task => {
      const picks = list(task.route).filter(r => r.explored);
      const exploration = task.explored ? '* ' + (picks.length ? picks.map(r => `${display(r.chosen)} p=${display(r.propensity)}`).join('; ') : 'p=—') : '—';
      return [link(task.id, run.key, task.id), task.title, task.difficulty, task.wave, chip(task.status),
        task.dwarf, task.qa, task.attempt, chip(task.verdict), exploration, depLinks(task.deps, run.key), task.branch];
    });
    const body = table(parent, ['ID', 'Title', 'Difficulty', 'Wave', 'Status', 'Dwarf', 'QA', 'Attempt', 'Verdict', '* Explored / propensity', 'Deps', 'Branch'], rows, 'Tasks');
    Array.from(body.children).forEach((row, i) => {
      const id = run.tasks[i].id;
      row.classList.toggle('selected', id === state.task);
      row.addEventListener('click', event => {
        if (!event.target.closest('a') && !window.getSelection()?.toString()) location.hash = route(run.key, id);
      });
    });
  }
  function renderTask(parent, task, run) {
    const solo = run.kind === 'solo';
    const panel = section(parent, solo ? 'Solo task' : `${task.id} · ${task.title || 'Untitled task'}`);
    panel.className = 'task-panel';
    panel.id = 'task-panel';
    if (!solo) {
      const close = link('Close task', run.key);
      close.className = 'close-task';
      panel.prepend(close);
    }
    if (solo) fields(panel, [['Status', chip(task.status)], ['Verdict', chip(task.verdict)],
      ['Dwarf', task.dwarf, true], ['QA', task.qa, true], ['Attempt', task.attempt]]);
    else fields(panel, [['ID', task.id, true], ['Title', task.title], ['Difficulty', task.difficulty],
      ['Status', chip(task.status)], ['Verdict', chip(task.verdict)], ['Accepted by', task.accepted_by],
      ['Branch', task.branch, true], ['Wave', task.wave], ['Dependencies', depLinks(task.deps, run.key)],
      ['Files', list(task.files).join('\n'), true], ['Dwarf', task.dwarf, true], ['QA', task.qa, true],
      ['Attempt', task.attempt], ['Explored', task.explored]]);
    const routing = solo ? null : section(panel, 'Routing');
    if (solo) { /* no routing for a solo run */ } else if (!list(task.route).length) routing.append(el('p', 'No routing data: routed before forge recorded decisions.', 'muted'));
    else table(routing, ['Tier', 'Candidates', 'Default', 'Chosen', 'Explored', 'Propensity', 'Epsilon', 'Time', 'Task', 'Decision', 'Inputs / extra fields'],
      task.route.map(entry => {
        const candidates = el('div', undefined, 'candidates mono');
        list(entry.candidates).forEach(spec => {
          const candidate = el('span', `${spec}${spec === entry.default ? ' (default)' : ''}`, spec === entry.chosen ? 'chosen' : '');
          candidates.append(candidate);
        });
        const known = ['tier', 'candidates', 'default', 'chosen', 'explored', 'propensity', 'epsilon', 'at', 'task', 'decision'];
        const extra = Object.fromEntries(Object.entries(entry).filter(([key]) => !known.includes(key)));
        return [entry.tier, candidates, entry.default, entry.chosen, entry.explored, entry.propensity, entry.epsilon,
          time(entry.at), entry.task, entry.decision, el('pre', extra)];
      }), 'Routing');
    const attempts = section(panel, 'Attempts');
    if (!list(task.attempts).length) attempts.append(el('p', 'No attempt history recorded.', 'muted'));
    else table(attempts, ['Attempt', 'Status', 'Infra class', 'Time'], task.attempts.map(a =>
      [a.attempt, chip(a.status), a.infra, time(a.at)]), 'Attempts');
    const escalations = solo ? null : section(panel, 'Escalations');
    if (solo) { /* no escalations for a solo run */ } else if (!list(task.escalations).length) escalations.append(el('p', 'No escalations recorded.', 'muted'));
    else table(escalations, ['Attempt', 'From → to', 'Time'], task.escalations.map(e =>
      [e.attempt, `${display(e.from)} → ${display(e.to)}`, time(e.at)]), 'Escalations');
    const reviews = section(panel, 'Reviews');
    fields(reviews, [['Primary reviewer', task.qa, true], ['Primary verdict', chip(task.verdict)]]);
    const qa = el('pre', task.qa_tail);
    qa.dataset.scroll = `qa-${task.id}`;
    reviews.append(qa);
    if (solo) { /* no shadow reviews for a solo run */ } else if (!list(task.shadow).length) reviews.append(el('p', 'No shadow reviews.', 'muted'));
    else table(reviews, ['Attempt', 'Index', 'Spec', 'Primary', 'Status', 'Verdict', 'Agreement', 'Started', 'Other fields'],
      task.shadow.map(s => {
        // The API has only the current primary verdict, not a verdict per attempt.
        const comparable = s.attempt != null && s.attempt === task.attempt;
        const agreement = comparable && ['PASS', 'FAIL'].includes(s.verdict) && ['PASS', 'FAIL'].includes(task.verdict) ?
          s.verdict === task.verdict ? 'agree' : 'disagree' : 'unknown';
        const extra = Object.fromEntries(Object.entries(s).filter(([key]) => !['attempt', 'k', 'spec', 'primary', 'status', 'verdict', 'started'].includes(key)));
        return [s.attempt, s.k, s.spec, s.primary, chip(s.status), chip(s.verdict), agreement, time(s.started), el('pre', extra)];
      }), 'Shadow reviews');
    details(panel, 'Dwarf output', task.dwarf_tail, `task-${task.id}-dwarf_tail`);
    const clef = solo ? null : section(panel, 'Clef predictions');
    const predictions = [];
    if (!solo && task.clef?.first_pass) predictions.push(['First pass', task.clef.first_pass]);
    for (const [attempt, prediction] of solo ? [] : Object.entries(task.clef?.retry_pass || {})) {
      if (prediction) predictions.push([`Retry · attempt ${attempt}`, prediction]);
    }
    if (solo) { /* no Clef predictions for a solo run */ } else if (!predictions.length) clef.append(el('p', 'No Clef data.', 'muted'));
    else table(clef, ['Pass', 'p', 'Assignment', 'Model', 'Time', 'Attempt', 'Plan table / extra fields'],
      predictions.map(([label, p]) => [label, p.p, el('pre', p.assignment), p.model, time(p.at), p.attempt,
        el('pre', Object.fromEntries(Object.entries(p).filter(([key]) => !['p', 'assignment', 'model', 'at', 'attempt'].includes(key))))]), 'Clef predictions');
    for (const [name, key] of solo ? [['Prompt', 'prompt']] :
      [['Prompt', 'prompt'], ['Approach', 'approach'], ['Retry findings', 'retry_findings']]) {
      details(panel, name, task[key], `task-${task.id}-${key}`);
    }
  }
  async function fetchJSON(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const text = await response.text();
    return { text, data: JSON.parse(text) };
  }
  // Coalesce polling and navigation requests for the same resource.
  function request(path, consume, label, onError) {
    if (state.pending.has(path)) return state.pending.get(path);
    const pending = fetchJSON(path).then(result => {
      consume(result);
      state.errors.delete(path);
    }).catch(error => {
      if (currentResource(path)) {
        state.errors.set(path, `${label}: ${error.message}. Any last good data is kept. Refresh retries while enabled and this tab is visible.`);
        onError?.(error);
      }
    }).finally(() => { state.pending.delete(path); renderErrors(); });
    state.pending.set(path, pending);
    return pending;
  }
  function loadList() {
    return request('/api/runs', result => {
      if (result.text === state.listText) return;
      state.runs = list(result.data.runs);
      state.listText = result.text;
      renderList();
    }, 'Could not refresh runs');
  }
  function loadRun(key) {
    return request('/api/runs/' + encodeURIComponent(key), result => {
      const changed = state.cache.get(key)?.text !== result.text;
      state.cache.set(key, result);
      if (state.key === key && changed) renderRun(result.data);
    }, 'Could not refresh run', error => {
      if (state.key !== key || state.cache.has(key)) return;
      // A freshly launched run can be opened before it is registered: wait, do not alarm.
      if (error.status === 404) state.errors.delete('/api/runs/' + encodeURIComponent(key));
      $('main').replaceChildren(el('p', error.status === 404 ?
        'Waiting for this run to appear… checking every 5 seconds while this tab is visible. If its directory was removed, pick another run.' :
        'Could not load this run. Select another run, or leave refresh enabled to retry.', 'empty'));
    });
  }
  function navigate() {
    const match = location.hash.match(/^#\/runs\/([^/]+)(?:\/tasks\/([^/]+))?\/?$/);
    let key = null, task = null;
    try { if (match) { key = decodeURIComponent(match[1]); task = match[2] == null ? null : decodeURIComponent(match[2]); } }
    catch (_) { /* Invalid escaped hashes fall back to the run picker. */ }
    state.key = key;
    state.task = task;
    renderErrors();
    markSelection();
    if ($('main').dataset.run !== key) {
      delete $('main').dataset.run;
      delete $('main').dataset.task;
    }
    if (!key) { $('main').replaceChildren(el('p', 'Select a run to inspect its tasks.', 'empty')); return; }
    const cached = state.cache.get(key);
    if (cached) renderRun(cached.data);
    else $('main').replaceChildren(el('p', 'Loading run…', 'empty'));
    loadRun(key);
  }
  function refresh() {
    if (state.paused || document.visibilityState !== 'visible') return;
    loadList();
    if (state.key) loadRun(state.key);
    for (const node of document.querySelectorAll('[data-epoch]')) node.textContent = relative(node.dataset.epoch);
  }
  function init() {
    $('filter').addEventListener('input', event => { state.filter = event.target.value; renderList(); });
    $('refresh').addEventListener('change', event => { state.paused = !event.target.checked; refresh(); });
    window.addEventListener('hashchange', navigate);
    document.addEventListener('visibilitychange', refresh);
    loadList();
    navigate();
    window.setInterval(refresh, 5000);
  }
  init();
  return { refresh };
})();
