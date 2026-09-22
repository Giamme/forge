'use strict';
(() => {
  const initial = JSON.parse(document.getElementById('capture').textContent);
  const app = document.querySelector('.app');
  const tree = document.getElementById('tree');
  const body = document.getElementById('detail-body');
  const tabs = document.getElementById('tabs');
  const detail = document.getElementById('detail');
  const search = document.getElementById('search');
  const connection = document.getElementById('connection');
  const retry = document.getElementById('retry');
  const pause = document.getElementById('pause-updates');
  const rows = new Map();
  const collapsed = new Set();
  const historyOffsets = new Map();
  const artifactCache = new Map();
  const selectedFiles = new Map();
  const requests = new Map();
  let generation = 0;
  let snapshot = initial;
  let livePaused = false;
  let followLogs = false;
  let mobileNav = false;
  let currentViewKey = '';
  let currentViewAvailable = true;
  let locationState = parseLocation();
  if (!FORGE_LIVE) locationState.run = initial.config?.id || '';
  const initialRun = initial.config?.id || '';
  if (FORGE_LIVE && locationState.run !== initialRun && !(initial.runs && !locationState.run)) snapshot = null;

  function parseLocation() {
    const q = new URLSearchParams(location.search);
    return {run:q.has('run') ? q.get('run') : (initial.config?.id || ''), task:q.get('task') || '',
      node:q.get('node') || '', view:q.get('view') || ''};
  }
  function urlFor(s) {
    const q = new URLSearchParams();
    q.set('run', s.run || '');
    if (s.task) q.set('task', s.task);
    if (s.node) q.set('node', s.node);
    if (s.view) q.set('view', s.view);
    return '?' + q.toString();
  }
  function e(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function setText(item, value) {
    const next = String(value ?? '');
    if (item.textContent !== next) item.textContent = next;
  }
  function tone(value) {
    const word = String(value || 'unknown');
    if (/^(pass|passed|complete|completed|verified|accepted)$/i.test(word)) return 'good';
    if (/fail|error|stopped|rejected|invalid/i.test(word)) return 'bad';
    if (/active|running|progress/i.test(word)) return 'active';
    return 'warning';
  }
  function bound(tag, key, value, cls) {
    const item = e(tag, value, cls);
    item.dataset.bind = key;
    return item;
  }
  function card(label, key, value, sub) {
    const item = e('div', undefined, 'card');
    item.append(e('div', label, 'card-label'), bound('div', key, value, 'card-value'));
    if (['task-execution', 'task-acceptance', 'task-verification', 'task-qa', 'node-status'].includes(key))
      item.querySelector('.card-value').dataset.tone = tone(value);
    if (sub) item.append(e('div', sub, 'card-sub'));
    return item;
  }
  function section(title, ...items) {
    const wrap = e('section', undefined, 'section');
    wrap.append(e('h3', title, 'section-title'), ...items);
    return wrap;
  }
  function panel(title, ...items) {
    const wrap = e('div', undefined, 'panel');
    if (title) wrap.append(e('h3', title));
    wrap.append(...items);
    return wrap;
  }
  function field(label, value, key) {
    const wrap = e('div', undefined, 'field');
    wrap.append(e('dt', label), key ? bound('dd', key, value) : e('dd', value));
    return wrap;
  }
  function fields(pairs) {
    const list = e('dl', undefined, 'field-grid');
    for (const [label, value, key] of pairs) list.append(field(label, value, key));
    return list;
  }
  function empty(message) { return e('p', message, 'empty'); }
  function excerpt(value) { return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120); }
  function selectedTask() { return snapshot?.tasks?.find(t => t.id === locationState.task); }
  function selectedNode() {
    const summary = selectedTask()?.nodes?.find(n => n.id === locationState.node);
    if (!summary) return undefined;
    const detail = artifactCache.get(artifactKey('node'));
    return detail ? {...detail, ...summary, goal:detail.goal, last:detail.last,
      planning_last:detail.planning_last, decomposition:detail.decomposition,
      decomposition_history:detail.decomposition_history, error:summary.error} : summary;
  }
  function tabsFor() {
    if (!locationState.run) return [];
    if (locationState.node) return ['Overview', 'Decomposition', 'Logs', 'Changes'];
    if (locationState.task) return ['Summary', 'QA', 'History'];
    return ['Overview', 'Configuration', 'Commands'];
  }
  function activeView() {
    const available = tabsFor();
    return available.find(v => v.toLowerCase() === locationState.view.toLowerCase()) || available[0] || '';
  }
  function viewKey() { return [locationState.run, locationState.task, locationState.node, activeView()].join('/'); }
  function select(next, replace = false) {
    locationState = {...locationState, ...next};
    if (!locationState.task) locationState.node = '';
    if (next.task !== undefined || next.node !== undefined) locationState.view = '';
    if (next.run !== undefined) { locationState.task = ''; locationState.node = ''; locationState.view = ''; }
    if (FORGE_LIVE) history[replace ? 'replaceState' : 'pushState']({}, '', urlFor(locationState));
    generation++;
    currentViewKey = '';
    mobileNav = false;
    followLogs = false;
    if (next.run !== undefined) {
      snapshot = next.run === snapshot?.config?.id || (!next.run && snapshot?.runs) ? snapshot : null;
      if (FORGE_LIVE) loadProgress(true);
    }
    render();
  }
  function isCurrent(resource, started, run) { return generation === started && locationState.run === run; }
  async function fetchResource(resource, params, onData) {
    if (!FORGE_LIVE || requests.has(resource)) return;
    const started = generation;
    const run = locationState.run;
    const controller = new AbortController();
    requests.set(resource, controller);
    try {
      const response = await fetch('data?' + params.toString(), {signal:controller.signal, cache:'no-store'});
      if (!response.ok) throw Error('Dashboard request failed (' + response.status + ')');
      const data = await response.json();
      if (data.error) throw Error(data.error);
      if (isCurrent(resource, started, run)) {
        onData(data);
        connection.textContent = livePaused ? 'Live updates paused' : 'Live · updated ' + new Date().toLocaleTimeString();
        connection.classList.remove('stale');
        retry.hidden = true;
      }
    } catch (error) {
      if (error.name !== 'AbortError' && isCurrent(resource, started, run)) {
        connection.textContent = 'Stale · reconnecting';
        connection.classList.add('stale');
        retry.hidden = false;
      }
    } finally { requests.delete(resource); }
  }
  function loadProgress(force = false) {
    if (!FORGE_LIVE || (livePaused && !force)) return;
    const q = new URLSearchParams({scope:locationState.run ? 'progress' : 'runs', run:locationState.run});
    const key = 'progress:' + locationState.run;
    fetchResource(key, q, data => {
      const oldTask = snapshot?.tasks?.find(task => task.id === locationState.task);
      const newTask = data.tasks?.find(task => task.id === locationState.task);
      if (oldTask && newTask && oldTask.goal !== newTask.goal) artifactCache.delete(artifactKey('task'));
      const oldNode = oldTask?.nodes?.find(node => node.id === locationState.node);
      const newNode = newTask?.nodes?.find(node => node.id === locationState.node);
      if (oldNode && newNode && oldNode.goal !== newNode.goal) artifactCache.delete(artifactKey('node'));
      if (!snapshot) currentViewKey = '';
      snapshot = data;
      render();
      if (locationState.task && activeView() === 'Logs') loadArtifact('logs', true);
      if (locationState.task && activeView() === 'Changes') loadArtifact('changes', true);
      if (locationState.node && ['Overview', 'Decomposition'].includes(activeView())) loadArtifact('node', true);
      if (locationState.task && activeView() === 'QA') loadArtifact('qa', true);
      if (locationState.task && activeView() === 'History') {
        loadArtifact('history', true, historySelection().key);
      }
    });
  }
  function artifactKey(kind, extra = '') { return [locationState.run, locationState.task, locationState.node, kind, extra].join(':'); }
  function historySelection() {
    const table = document.getElementById('history-table')?.value || 'events';
    const offset = historyOffsets.get(locationState.task + ':' + table) || 0;
    return {table, offset, key:table + ':' + offset};
  }
  function loadArtifact(kind, refresh = false, extra = '') {
    const key = artifactKey(kind, extra);
    if (!FORGE_LIVE) return;
    if (artifactCache.has(key) && !refresh) { if (kind !== 'node') renderArtifact(kind, artifactCache.get(key)); return; }
    const requestedView = viewKey();
    const q = new URLSearchParams({scope:'artifact', run:locationState.run, task:locationState.task, artifact:kind});
    if (locationState.node) q.set('node', locationState.node);
    if (kind === 'history') {
      const [table, requestedOffset] = extra.split(':');
      q.set('table', table);
      q.set('offset', requestedOffset || '0');
    }
    fetchResource(key, q, data => {
      const previous = artifactCache.get(key);
      artifactCache.set(key, data);
      if (requestedView !== viewKey() || (kind === 'history' && extra !== historySelection().key)) return;
      if (JSON.stringify(previous) !== JSON.stringify(data)) renderArtifact(kind, data);
    });
  }
  function runOverview() {
    const tasks = snapshot?.tasks || [];
    const nodes = tasks.flatMap(t => t.nodes || []);
    const active = nodes.filter(n => /active|running/i.test(n.status || '')).length;
    const failed = nodes.filter(n => /fail|error|stopped/i.test(n.status || '')).length;
    const known = nodes.filter(n => typeof n.cost === 'number' && Number.isFinite(n.cost));
    const cost = known.length ? known.reduce((sum, n) => sum + n.cost, 0).toFixed(4) + ' USD observed' : 'Unknown';
    const cards = e('div', undefined, 'cards');
    cards.append(card('Fractal execution', 'execution', nodes.length + ' nodes · ' + active + ' active', failed + ' problems'),
      card('Forge acceptance', 'acceptance', tasks.filter(t => t.acceptance === 'PASS').length + ' / ' + tasks.length + ' passed'),
      card('Verification', 'verification', tasks.filter(t => /verified|pass/i.test(t.verification || '')).length + ' / ' + tasks.length + ' verified'),
      card('Observed cost', 'cost', cost, 'Unknown usage is excluded'));
    body.append(section('Progress and problems', cards));
    const problems = e('div'); problems.id = 'problem-list';
    const summary = e('div', undefined, 'cards'); summary.id = 'task-list';
    body.append(section('Problems', problems), section('Tasks', summary));
    syncOverviewLists();
  }
  function syncOverviewLists() {
    const tasks = snapshot?.tasks || [];
    const problems = tasks.flatMap(t => [
      ...((t.nodes || []).filter(n => /fail|error|stopped/i.test(n.status || '')).map(n => ({
        key:t.id + ':' + n.id, task:t.id, node:n.id, label:n.id + ' · ' + n.status + ' · ' + t.id,
        description:excerpt(n.error || n.goal)}))),
      ...(/fail|error/i.test(t.state?.qa_status || '') ? [{key:t.id + ':qa', task:t.id, node:'',
        label:'QA · ' + t.state.qa_status + ' · ' + t.id, description:excerpt(t.goal)}] : [])]);
    const entries = tasks.map(t => ({key:t.id, task:t.id, node:'', label:t.id,
      description:excerpt(t.goal) + ' · Fractal: ' + (t.state?.status || 'unknown') + ' · Forge: ' + (t.acceptance || 'pending')}));
    for (const [host, records, message] of [
      [document.getElementById('problem-list'), problems, 'No problems reported in the current snapshot.'],
      [document.getElementById('task-list'), entries, 'No task execution has started.']]) {
      if (!host) continue;
      host.querySelector(':scope > .empty')?.remove();
      const keys = new Set(records.map(record => record.key));
      for (const child of [...host.children]) if (!keys.has(child.dataset.key)) child.remove();
      records.forEach((record, index) => {
        let item = [...host.children].find(child => child.dataset.key === record.key);
        if (!item) {
          const button = e('button', undefined, 'button');
          item = panel('', button, e('p', undefined, 'small'));
          item.dataset.key = record.key;
          host.insertBefore(item, host.children[index] || null);
        }
        const button = item.querySelector('button');
        setText(button, record.label);
        button.onclick = () => select({task:record.task, node:record.node});
        setText(item.querySelector('p'), record.description);
        if (host.children[index] !== item) host.insertBefore(item, host.children[index] || null);
      });
      if (!records.length) host.append(empty(message));
    }
  }
  function configView() {
    const config = snapshot?.config || {};
    body.append(section('Effective run configuration', panel('', fields([
      ['Run ID', config.id || 'unknown'], ['Repository', config.repository || 'unknown'],
      ['Backend', 'Fractal ' + (config.version || 'unknown')], ['Mode', config.mode || 'unknown']]),
      e('pre', JSON.stringify(config, null, 2), 'artifact'))));
  }
  function commandsView() {
    const id = snapshot?.config?.id || locationState.run;
    body.append(e('p', 'These commands are for a terminal. The browser cannot pause or change execution.', 'notice'));
    for (const command of ['status', 'tree', 'logs', 'pause', 'resume', 'stop']) {
      const line = './forge fractal ' + command + ' ' + id;
      const wrap = panel(command, e('pre', line)); wrap.classList.add('command');
      const copy = e('button', 'Copy', 'button copy');
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(line); copy.textContent = 'Copied'; }
        catch {
          const selection = window.getSelection();
          selection.removeAllRanges();
          const range = document.createRange(); range.selectNodeContents(wrap.querySelector('pre'));
          selection.addRange(range); copy.textContent = 'Selected';
        }
      };
      wrap.append(copy); body.append(wrap);
    }
  }
  function taskSummary() {
    const t = selectedTask(); if (!t) return body.append(empty('This task is not present in the latest snapshot.'));
    const cards = e('div', undefined, 'cards');
    cards.append(card('Fractal execution', 'task-execution', t.state?.status || 'unknown'),
      card('Forge acceptance', 'task-acceptance', t.acceptance || 'pending'),
      card('Verification', 'task-verification', t.verification || 'unverified'),
      card('QA execution', 'task-qa', t.state?.qa_status || 'unknown'));
    body.append(section('Status', cards));
    const prompt = t.request?.prompt || t.goal || '';
    const goal = bound('pre', 'task-goal', prompt, 'artifact');
    body.append(section('Task goal', goal), section('Details', panel('', fields([
      ['Model', t.model || t.request?.model || 'unknown', 'task-model'],
      ['Elapsed', Math.round(t.state?.elapsed || 0) + 's', 'task-elapsed'],
      ['Nodes', String(t.nodes?.length || 0), 'task-nodes'],
      ['Runner result', t.pipeline_exit_code ?? 'pending', 'task-exit']]))));
    if (FORGE_LIVE) loadArtifact('task');
  }
  function taskQA() {
    const t = selectedTask(); if (!t) return body.append(empty('Task unavailable.'));
    body.append(section('Forge acceptance and QA', panel('', fields([
      ['Acceptance', t.acceptance || 'pending', 'qa-acceptance'],
      ['Verification', t.verification || 'unverified', 'qa-verification'],
      ['QA execution', t.state?.qa_status || 'unknown', 'qa-status'],
      ['Pipeline exit', t.pipeline_exit_code ?? 'pending', 'qa-exit']]))));
    const host = e('div'); host.id = 'artifact-host'; body.append(section('QA artifacts', host));
    if (FORGE_LIVE) loadArtifact('qa'); else renderArtifact('qa', t.qa || {});
  }
  function taskHistory() {
    const t = selectedTask(); if (!t) return body.append(empty('Task unavailable.'));
    const toolbar = e('div', undefined, 'toolbar');
    const label = e('label', 'History source', 'small'); label.htmlFor = 'history-table';
    const source = e('select'); source.id = 'history-table';
    for (const name of ['events', 'steps', 'messages']) {
      const option = e('option', name === 'events' ? 'Activity' : name[0].toUpperCase() + name.slice(1)); option.value = name; source.append(option);
    }
    source.value = source.dataset.value = selectedFiles.get('history:' + t.id) || 'events';
    source.onchange = () => { selectedFiles.set('history:' + t.id, source.value); showHistory(); };
    toolbar.append(label, source);
    const prev = e('button', 'Previous', 'button'); prev.id = 'history-prev'; prev.onclick = () => pageHistory(-1);
    const next = e('button', 'Next', 'button'); next.id = 'history-next'; next.onclick = () => pageHistory(1);
    toolbar.append(prev, next, e('span', '', 'small')); toolbar.lastChild.id = 'history-page';
    body.append(toolbar);
    const host = e('div'); host.id = 'artifact-host'; body.append(host);
    showHistory();
  }
  function pageHistory(direction) {
    const table = document.getElementById('history-table').value;
    const key = locationState.task + ':' + table;
    historyOffsets.set(key, Math.max(0, (historyOffsets.get(key) || 0) + direction * 100));
    showHistory();
  }
  function showHistory() {
    const {table, offset, key} = historySelection();
    const page = document.getElementById('history-page');
    if (page) page.textContent = 'Rows ' + (offset + 1) + '–' + (offset + 100);
    document.getElementById('history-prev').disabled = offset === 0;
    const host = document.getElementById('artifact-host'); host.replaceChildren(empty('Loading history…'));
    if (FORGE_LIVE) loadArtifact('history', false, key);
    else {
      const t = selectedTask();
      const data = ({events:t.activity, steps:t.steps, messages:t.messages})[table] || {};
      renderArtifact('history', {rows:(data.rows || []).slice(offset, offset + 100), complete:data.complete});
    }
  }
  function nodeOverview() {
    const n = selectedNode(); if (!n) return body.append(empty('This node is not present in the latest snapshot.'));
    if (FORGE_LIVE) loadArtifact('node');
    const cards = e('div', undefined, 'cards');
    cards.append(card('Fractal execution', 'node-status', n.status || 'unknown'), card('Iteration', 'node-iteration', n.iteration ?? 0),
      card('Observed cost', 'node-cost', n.cost == null ? 'Unknown' : n.cost + ' USD', 'Unpriced steps: ' + (n.unknown_cost_steps ?? 'unknown')));
    body.append(section('Execution', cards), section('Goal', bound('pre', 'node-goal', n.goal || '', 'artifact')),
      section('Scope', panel('', fields([['Model', n.model || 'unknown'], ['Parent', n.parent || 'Top level'],
        ['Difficulty', n.difficulty || 'unknown'], ['Owned paths', (n.paths || []).join(', ') || 'none'],
        ['Dependencies', (n.deps || []).join(', ') || 'none']]))));
    if (n.error) body.append(section('Problem', panel('', bound('pre', 'node-error', n.error))));
    if (n.last) body.append(section('Last response', panel('', bound('pre', 'node-last', n.last))));
  }
  function decisionPanel(value, title) {
    const item = panel(title, fields([
      ['Decision', value.decision || value.phase || 'unknown'], ['Reason', value.reason || value.error || 'No reason recorded'],
      ['Phase', value.phase || 'unknown'], ['Planner', value.planner || selectedNode()?.resolved_planner || 'unknown'],
      ['Attempts', value.attempts ?? 'unknown'], ['Bounds', value.bounds ? JSON.stringify(value.bounds) : 'unknown']]));
    if (value.admission?.length) {
      item.append(e('h3', 'Admitted children'));
      for (const child of value.admission) item.append(e('p', (child.name || child.id) + ' · ' + excerpt(child.goal || child.paths?.join(', '))));
    }
    return item;
  }
  function nodeDecomposition() {
    const n = selectedNode(); if (!n) return body.append(empty('Node unavailable.'));
    if (FORGE_LIVE) loadArtifact('node');
    if (FORGE_LIVE && !artifactCache.has(artifactKey('node'))) return body.append(empty('Loading decomposition decision…'));
    if (!n.decomposition && !n.decomposition_history?.length) return body.append(empty('No decomposition decision has been recorded for this node.'));
    if (n.decomposition) body.append(section('Current decision', decisionPanel(n.decomposition, 'Decision and reason')));
    if (n.decomposition_history?.length) body.append(section('Earlier decisions', ...n.decomposition_history.map((d, i) => decisionPanel(d, 'Decision ' + (i + 1)))));
    if (n.planning_last) body.append(section('Planner response', e('pre', n.planning_last, 'artifact')));
  }
  function nodeLogs() {
    const toolbar = e('div', undefined, 'toolbar');
    const label = e('label', 'Step and file', 'small'); label.htmlFor = 'log-file';
    const selectFile = e('select'); selectFile.id = 'log-file';
    selectFile.onchange = () => { selectedFiles.set(artifactKey('log-file'), selectFile.value); renderArtifact('logs', FORGE_LIVE ? artifactCache.get(artifactKey('logs')) || {} : selectedNode()?.logs || {}); };
    const follow = e('button', 'Follow: off', 'button'); follow.id = 'follow-logs'; follow.setAttribute('aria-pressed', 'false');
    follow.onclick = () => { followLogs = !followLogs; follow.setAttribute('aria-pressed', String(followLogs)); follow.textContent = 'Follow: ' + (followLogs ? 'on' : 'off'); if (followLogs) jumpLatest(); };
    const jump = e('button', 'Jump to latest', 'button'); jump.onclick = () => { followLogs = true; follow.setAttribute('aria-pressed', 'true'); follow.textContent = 'Follow: on'; jumpLatest(); };
    toolbar.append(label, selectFile, follow, jump);
    const host = e('div'); host.id = 'artifact-host';
    body.append(toolbar, host);
    if (FORGE_LIVE) loadArtifact('logs'); else renderArtifact('logs', selectedNode()?.logs || {});
  }
  function jumpLatest() { const pre = document.getElementById('log-output'); if (pre) pre.scrollTop = pre.scrollHeight; }
  function nodeChanges() {
    const host = e('div'); host.id = 'artifact-host'; body.append(host);
    if (FORGE_LIVE) loadArtifact('changes'); else renderArtifact('changes', selectedNode()?.candidate_changes || {});
  }
  function rawDownload(name, text) {
    const button = e('button', 'Download raw', 'button');
    button.onclick = () => {
      const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
      const link = e('a'); link.href = url; link.download = name.replace(/[^A-Za-z0-9._-]/g, '_'); link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    return button;
  }
  function renderArtifact(kind, data) {
    if (kind === 'node') {
      const scroll = detail.scrollTop;
      currentViewKey = '';
      renderDetails();
      detail.scrollTop = scroll;
      return;
    }
    if (kind === 'task') {
      const goal = document.querySelector('[data-bind=task-goal]');
      const task = selectedTask();
      if (task && data.request) task.request = data.request;
      if (goal && data.request?.prompt) setText(goal, data.request.prompt);
      return;
    }
    if (kind !== activeView().toLowerCase()) return;
    const host = document.getElementById('artifact-host');
    if (!host) return;
    const paneScroll = detail.scrollTop;
    if (kind === 'history') renderHistoryArtifact(host, data);
    if (kind === 'qa') renderQaArtifact(host, data);
    if (kind === 'logs') renderLogArtifact(host, data);
    if (kind === 'changes') renderChangesArtifact(host, data);
    detail.scrollTop = paneScroll;
  }
  function renderHistoryArtifact(host, data) {
    const rows = data.rows || [];
    const list = e('div', undefined, 'history-list');
    if (data.missing) list.append(empty('History unavailable: ' + data.missing));
    else if (!rows.length) list.append(empty('No history entries in this page.'));
    else for (const row of rows) {
      const entry = e('div', undefined, 'history-row');
      entry.append(e('div', 'Entry ' + (row.id ?? '—'), 'small'), e('pre', JSON.stringify(row, null, 2)));
      list.append(entry);
    }
    host.replaceChildren(list);
    document.getElementById('history-next').disabled = rows.length < 100;
  }
  function renderQaArtifact(host, data) {
    const wrap = e('div');
    for (const [name, artifact] of Object.entries(data || {})) {
      const text = artifact?.text;
      wrap.append(section(name, artifact?.missing ? empty('Artifact unavailable: ' + artifact.missing) :
        panel('', rawDownload(name, text || ''), e('pre', text || 'Empty artifact', 'artifact'))));
    }
    if (!Object.keys(data || {}).length) wrap.append(empty('No QA artifacts have been captured.'));
    host.replaceChildren(wrap);
  }
  function renderLogArtifact(host, data) {
    const files = Object.keys(data || {});
    const picker = document.getElementById('log-file');
    const chosen = selectedFiles.get(artifactKey('log-file')) || files[0] || '';
    if (picker) {
      const old = Array.from(picker.options).map(o => o.value).join('|');
      if (old !== files.join('|')) {
        picker.replaceChildren(...files.map(name => { const option = e('option', name); option.value = name; return option; }));
      }
      picker.value = files.includes(chosen) ? chosen : files[0] || '';
    }
    if (!files.length) { host.replaceChildren(empty('No step logs have been captured.')); return; }
    const artifact = data?.[picker?.value];
    const oldPre = document.getElementById('log-output');
    const previous = oldPre?.dataset.file === picker.value ? oldPre.scrollTop : 0;
    const atEnd = oldPre ? oldPre.scrollHeight - oldPre.clientHeight - oldPre.scrollTop < 25 : false;
    const pre = e('pre', artifact?.text || (artifact?.missing ? 'Artifact unavailable: ' + artifact.missing : 'Empty log'), 'artifact');
    pre.id = 'log-output'; pre.dataset.file = picker.value;
    pre.addEventListener('scroll', () => {
      if (followLogs && pre.scrollHeight - pre.clientHeight - pre.scrollTop > 25) {
        followLogs = false;
        const button = document.getElementById('follow-logs');
        button.setAttribute('aria-pressed', 'false'); button.textContent = 'Follow: off';
      }
    });
    if (oldPre && oldPre.textContent === pre.textContent && oldPre.dataset.file === pre.dataset.file) return;
    host.replaceChildren(panel('', rawDownload(picker.value, artifact?.text || ''), pre));
    pre.scrollTop = followLogs && (atEnd || !oldPre) ? pre.scrollHeight : previous;
  }
  function renderChangesArtifact(host, data) {
    if (data?.missing) { host.replaceChildren(empty('Candidate diff unavailable: ' + data.missing)); return; }
    const text = data?.text || '';
    if (!text) { host.replaceChildren(empty('No candidate changes have been captured.')); return; }
    const lines = e('pre', undefined, 'artifact');
    for (const line of text.split('\n')) {
      const span = e('span', line + '\n', 'diff-line');
      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') || line.startsWith('diff ')) span.classList.add('header');
      else if (line.startsWith('+')) span.classList.add('add');
      else if (line.startsWith('-')) span.classList.add('remove');
      lines.append(span);
    }
    host.replaceChildren(panel('', rawDownload('candidate.diff', text), lines));
  }
  function updateBoundValues() {
    const t = selectedTask(), n = selectedNode();
    const tasks = snapshot?.tasks || [], nodes = tasks.flatMap(item => item.nodes || []);
    const values = {
      execution:nodes.length + ' nodes · ' + nodes.filter(item => /active|running/i.test(item.status || '')).length + ' active',
      acceptance:tasks.filter(item => item.acceptance === 'PASS').length + ' / ' + tasks.length + ' passed',
      verification:tasks.filter(item => /verified|pass/i.test(item.verification || '')).length + ' / ' + tasks.length + ' verified',
      cost:nodes.some(item => typeof item.cost === 'number') ? nodes.reduce((sum, item) => sum + (typeof item.cost === 'number' ? item.cost : 0), 0).toFixed(4) + ' USD observed' : 'Unknown',
      'task-execution':t?.state?.status || 'unknown', 'task-acceptance':t?.acceptance || 'pending',
      'task-verification':t?.verification || 'unverified', 'task-qa':t?.state?.qa_status || 'unknown',
      'task-model':t?.model || t?.request?.model || 'unknown',
      'task-elapsed':Math.round(t?.state?.elapsed || 0) + 's', 'task-nodes':String(t?.nodes?.length || 0),
      'task-exit':t?.pipeline_exit_code ?? 'pending',
      'qa-acceptance':t?.acceptance || 'pending', 'qa-verification':t?.verification || 'unverified',
      'qa-status':t?.state?.qa_status || 'unknown', 'qa-exit':t?.pipeline_exit_code ?? 'pending',
      'task-goal':t?.request?.prompt || t?.goal || '', 'node-status':n?.status || 'unknown',
      'node-iteration':n?.iteration ?? 0, 'node-cost':n?.cost == null ? 'Unknown' : n.cost + ' USD',
      'node-goal':n?.goal || '', 'node-error':n?.error || '', 'node-last':n?.last || ''};
    for (const item of body.querySelectorAll('[data-bind]')) if (item.dataset.bind in values) {
      setText(item, values[item.dataset.bind]);
      if (item.dataset.tone) item.dataset.tone = tone(values[item.dataset.bind]);
    }
    if (!locationState.task && locationState.run && activeView() === 'Overview') syncOverviewLists();
  }
  function renderDetails() {
    const key = viewKey();
    const c = snapshot?.config;
    const t = selectedTask(), n = selectedNode();
    const available = !locationState.task || Boolean(t && (!locationState.node || n));
    document.getElementById('eyebrow').textContent = !locationState.run ? 'Workspace' : n ? 'Fractal node' : t ? 'Forge task' : 'Run overview';
    document.getElementById('detail-title').textContent = !locationState.run ? 'Managed runs' : n?.id || t?.id || c?.id || locationState.run;
    document.getElementById('detail-subtitle').textContent = n ? excerpt(n.goal) : t ? excerpt(t.goal || t.request?.prompt) : c?.repository || '';
    if (currentViewKey === key && currentViewAvailable === available) { updateBoundValues(); return; }
    currentViewKey = key;
    currentViewAvailable = available;
    tabs.replaceChildren();
    for (const name of tabsFor()) {
      const button = e('button', name); button.type = 'button';
      if (name === activeView()) button.setAttribute('aria-current', 'page');
      button.onclick = () => { locationState.view = name.toLowerCase(); if (FORGE_LIVE) history.pushState({}, '', urlFor(locationState)); currentViewKey = ''; renderDetails(); };
      tabs.append(button);
    }
    body.replaceChildren();
    if (!snapshot) { body.append(empty('Loading run…')); return; }
    if (!locationState.run || snapshot.runs) {
      tabs.replaceChildren();
      const list = e('div', undefined, 'run-list');
      for (const run of snapshot.runs || []) {
        const link = e(FORGE_LIVE ? 'a' : 'div', undefined, 'panel run-link');
        link.append(e('strong', run.id), e('span', run.repository || 'Repository unknown'));
        if (FORGE_LIVE) {
          link.href = urlFor({run:run.id});
          link.onclick = event => { event.preventDefault(); select({run:run.id}); };
        }
        list.append(link);
      }
      body.append(list.childElementCount ? list : empty('No managed runs yet. Start a Forge run with --fractal.'));
      if (snapshot.diagnostics?.length) body.append(section('Discovery diagnostics', e('pre', JSON.stringify(snapshot.diagnostics, null, 2), 'artifact')));
      return;
    }
    const view = activeView();
    if (n) ({Overview:nodeOverview, Decomposition:nodeDecomposition, Logs:nodeLogs, Changes:nodeChanges})[view]();
    else if (t) ({Summary:taskSummary, QA:taskQA, History:taskHistory})[view]();
    else ({Overview:runOverview, Configuration:configView, Commands:commandsView})[view]();
    updateBoundValues();
  }
  function makeRow(key, taskId, node, depth, hasChildren) {
    const row = e('div', undefined, 'tree-row ' + (node ? 'node' : 'task'));
    row.dataset.key = key;
    const indent = e('span', undefined, 'indent'); indent.style.setProperty('--depth', String(depth));
    const toggle = e('button', '▾', 'tree-toggle'); toggle.type = 'button'; toggle.setAttribute('aria-label', 'Collapse ' + (node?.id || taskId));
    toggle.onclick = () => { if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key); renderTree(); };
    const selectRow = e('button', undefined, 'tree-select'); selectRow.type = 'button';
    selectRow.onclick = () => select({task:taskId, node:node?.id || ''});
    const top = e('span', undefined, 'row-top');
    top.append(e('span', '', 'row-label'), e('span', '', 'small row-status'), e('span', '', 'small row-model'));
    selectRow.append(top, e('span', '', 'row-goal'));
    row.append(indent, toggle, selectRow); rows.set(key, row);
    updateRow(row, taskId, node, hasChildren);
    return row;
  }
  function updateRow(row, taskId, node, hasChildren) {
    const key = row.dataset.key;
    row.classList.toggle('selected', locationState.task === taskId && locationState.node === (node?.id || ''));
    const toggle = row.querySelector('.tree-toggle'); toggle.classList.toggle('empty', !hasChildren);
    toggle.setAttribute('aria-expanded', String(!collapsed.has(key)));
    toggle.textContent = collapsed.has(key) ? '▸' : '▾';
    const selectRow = row.querySelector('.tree-select');
    selectRow.setAttribute('aria-current', row.classList.contains('selected') ? 'page' : 'false');
    setText(row.querySelector('.row-label'), node?.id || taskId);
    const statusText = node?.status || snapshot.tasks.find(t => t.id === taskId)?.state?.status || 'unknown';
    const model = node?.model || snapshot.tasks.find(t => t.id === taskId)?.model || 'unknown model';
    setText(row.querySelector('.row-status'), statusText);
    row.querySelector('.row-status').dataset.tone = tone(statusText);
    setText(row.querySelector('.row-model'), model);
    setText(row.querySelector('.row-goal'), excerpt(node?.goal || snapshot.tasks.find(t => t.id === taskId)?.goal));
    selectRow.setAttribute('aria-label', (node ? 'Node ' + node.id : 'Task ' + taskId) + ', ' + statusText + ', model ' + model + ', ' + excerpt(node?.goal || snapshot.tasks.find(t => t.id === taskId)?.goal));
  }
  function rowsForTask(task, query) {
    const nodes = task.nodes || [];
    const nodeIds = new Set(nodes.map(node => node.id));
    const children = new Map();
    for (const node of nodes) {
      const parent = nodeIds.has(node.parent) ? node.parent : null;
      const siblings = children.get(parent) || [];
      siblings.push(node);
      children.set(parent, siblings);
    }
    const matches = item => [item.id, item.goal, item.model, item.status, item.state?.status]
      .some(value => String(value || '').toLowerCase().includes(query));
    const taskMatch = matches(task);
    const visible = new Set();
    function findMatches(node, ancestors) {
      if (ancestors.has(node.id)) return false;
      const next = new Set([...ancestors, node.id]);
      let found = taskMatch || matches(node);
      for (const child of children.get(node.id) || []) if (findMatches(child, next)) found = true;
      if (found) visible.add(node.id);
      return found;
    }
    for (const root of children.get(null) || []) findMatches(root, new Set());
    if (query && !taskMatch && !visible.size) return [];
    const taskKey = 'task:' + task.id;
    const result = [[taskKey, task.id, null, 0, nodes.length > 0]];
    function append(parent, depth, ancestors) {
      const parentKey = parent === null ? taskKey : 'node:' + task.id + ':' + parent;
      if (collapsed.has(parentKey)) return;
      for (const node of children.get(parent) || []) {
        if (ancestors.has(node.id) || (query && !visible.has(node.id))) continue;
        const key = 'node:' + task.id + ':' + node.id;
        result.push([key, task.id, node, depth, (children.get(node.id) || []).length > 0]);
        append(node.id, depth + 1, new Set([...ancestors, node.id]));
      }
    }
    append(null, 1, new Set());
    return result;
  }
  function renderTree() {
    const query = search.value.trim().toLowerCase();
    const desired = (snapshot?.tasks || []).flatMap(task => rowsForTask(task, query));
    const keys = new Set(desired.map(item => item[0]));
    for (const [key, row] of rows) if (!keys.has(key)) { row.remove(); rows.delete(key); }
    tree.querySelector(':scope > .empty')?.remove();
    desired.forEach(([key, taskId, node, depth, hasChildren], index) => {
      const row = rows.get(key) || makeRow(key, taskId, node, depth, hasChildren);
      updateRow(row, taskId, node, hasChildren);
      if (tree.children[index] !== row) tree.insertBefore(row, tree.children[index] || null);
    });
    document.getElementById('nav-count').textContent = desired.length + ' items';
    if (!desired.length) tree.append(empty(query ? 'No matching tasks or nodes.' : 'No tasks yet.'));
  }
  function render() {
    document.getElementById('run-heading').textContent = locationState.run || 'Runs';
    document.getElementById('run-meta').textContent = snapshot?.config ? snapshot.config.repository || '' : '';
    document.getElementById('all-runs').hidden = !FORGE_LIVE;
    pause.hidden = !FORGE_LIVE;
    search.hidden = !locationState.run;
    document.querySelector('.search-label').hidden = !locationState.run;
    document.getElementById('navigator').hidden = !locationState.run;
    app.classList.toggle('show-detail', Boolean(locationState.run && (locationState.task || locationState.node) && !mobileNav));
    if (!FORGE_LIVE) connection.textContent = 'Offline capture · ' + new Date((initial.captured_at || 0) * 1000).toLocaleString();
    else if (!connection.textContent) connection.textContent = 'Connecting…';
    renderTree(); renderDetails();
  }
  document.getElementById('all-runs').onclick = event => { event.preventDefault(); select({run:''}); };
  document.getElementById('mobile-back').onclick = () => { mobileNav = true; app.classList.remove('show-detail'); search.focus(); };
  search.oninput = () => renderTree();
  retry.onclick = () => {
    loadProgress(true);
    const view = activeView();
    if (view === 'Summary') loadArtifact('task', true);
    if (view === 'Logs' || view === 'Changes') loadArtifact(view.toLowerCase(), true);
    if (view === 'QA') loadArtifact('qa', true);
    if (view === 'History') {
      loadArtifact('history', true, historySelection().key);
    }
  };
  pause.onclick = () => { livePaused = !livePaused; pause.setAttribute('aria-pressed', String(livePaused)); pause.textContent = livePaused ? 'Resume live updates' : 'Pause live updates';
    connection.textContent = livePaused ? 'Live updates paused' : 'Reconnecting…';
    if (livePaused) for (const controller of requests.values()) controller.abort();
    else loadProgress(true);
  };
  window.addEventListener('popstate', () => { locationState = parseLocation(); generation++; currentViewKey = ''; mobileNav = false;
    if (snapshot?.config?.id !== locationState.run && !(snapshot?.runs && !locationState.run)) { snapshot = null; loadProgress(true); }
    render(); });
  render();
  if (FORGE_LIVE) { if (!snapshot) loadProgress(true); setInterval(() => loadProgress(), 2000); }
})();
