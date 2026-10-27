/* forge-ui verification view: what the verification runs now, its lanes, and which tests failed
   (window.ForgeVerify, and a CommonJS module under node:test). panel.js's verification inspector delegates here.
   The pure helpers build a view model from RunDetail.verification (ui/docs/api.md) and are tested in
   ui/tests/js/verifyview.test.js; render(host, ctx) is thin DOM over them. Test names, messages, excerpts and log
   lines are untrusted: they only ever enter the page as text.
   ctx = { run, now (epoch seconds), fetchJSON(path), rerender(), hash(sel), onSelect(sel) } */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeVerify = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const IDLE_WARN_S = 300;                 // silence past this is flagged
  const CONTEXT_LINES = 10;                // lines shown above an opened log line
  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const STEP = /^::forge step name=(\S+) state=(start|end)(?: exit=(-?\d+))?$/;
  const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
  const LANE_CHIP = { waiting: 'pending', running: 'running', pass: 'pass', fail: 'fail', flaky: 'flaky', known: 'known',
    skipped: 'none', interrupted: 'interrupted' };

  /* ---------- pure ---------- */
  function dur(s) {
    s = num(s); if (s === null) return '—';
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
    return Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm';
  }
  const since = (start, now) => num(start) !== null && num(now) !== null ? Math.max(0, now - start) : null;
  // "no output for N min" past five minutes of silence, else null
  function idleWarning(idle) {
    idle = num(idle);
    return idle !== null && idle > IDLE_WARN_S ? `no output for ${Math.floor(idle / 60)} min` : null;
  }
  // The last step a log started and no later line ended (steps nest), or null.
  function openStep(text) {
    const open = [];
    for (const raw of String(text || '').split('\n')) {
      const m = STEP.exec(raw.replace(ANSI, '').trim());
      if (!m) continue;
      if (m[2] === 'start') open.push(m[1]);
      else { const i = open.lastIndexOf(m[1]); if (i >= 0) open.splice(i, 1); }
    }
    return open.length ? open[open.length - 1] : null;
  }
  function countsText(tests) {
    if (!tests || typeof tests !== 'object') return null;
    const n = k => num(tests[k]) || 0;
    const parts = [`${n('pass')} passed`, `${n('fail') + n('error')} failed`];
    if (n('skip')) parts.push(`${n('skip')} skipped`);
    return parts.join(' · ');
  }
  // "rerun 1 of 2" once a lane ran again (of its own budget, when the lane set one)
  function runsText(lane) {
    const reruns = list(lane && lane.runs).filter(r => r && r.kind === 'rerun').length;
    if (!reruns) return '';
    return `rerun ${reruns}${num(lane.retries) !== null ? ' of ' + lane.retries : ''}`;
  }
  function laneRow(lane, now) {
    const running = lane.state === 'running';
    const took = running && num(lane.started) !== null && num(now) !== null ? since(lane.started, now) : num(lane.duration_s);
    return {
      name: String(lane.name || ''), state: lane.state || 'unknown', chip: LANE_CHIP[lane.state] || 'unknown',
      time: took === null ? '' : dur(took), running,
      runs: runsText(lane), counts: countsText(lane.tests), hasTests: !!(lane.tests && typeof lane.tests === 'object'),
      step: running && lane.step && lane.step.name ? lane.step.name : null,
      last_line: running && lane.last_line ? String(lane.last_line) : null,
      idle: running ? idleWarning(lane.idle_s) : null,
      labels: [lane.first ? 'first' : null, lane.group ? 'group ' + lane.group : null].filter(Boolean),
    };
  }
  // failing tests in lane order: [{lane, entries: [{name, badge: new|known, message, excerpt, log_line}], fresh, known}]
  function groupFailing(failing) {
    const groups = new Map();
    for (const f of list(failing)) {
      if (!f || typeof f !== 'object') continue;
      const lane = String(f.lane || '');
      if (!groups.has(lane)) groups.set(lane, { lane, entries: [], fresh: 0, known: 0 });
      const g = groups.get(lane), known = f.known === true;
      g.entries.push({ name: String(f.name || '?'), badge: known ? 'known' : 'new', message: f.message || '', excerpt: f.excerpt || '',
        log_line: num(f.log_line) !== null && f.log_line > 0 ? f.log_line : null, suite: f.suite || null, file: f.file || null });
      if (known) g.known++; else g.fresh++;
    }
    return Array.from(groups.values());
  }
  // The lane whose log the live tail follows: the chosen one if it still exists, else the running lane that printed
  // last, else the first running, failed or listed lane. null in single mode.
  function tailLane(v, chosen) {
    const lanes = list(v && v.lanes);
    if (!lanes.length) return null;
    if (chosen && lanes.some(l => l.name === chosen)) return chosen;
    const running = lanes.filter(l => l.state === 'running');
    const printed = running.filter(l => num(l.last_output_at) !== null).sort((a, b) => b.last_output_at - a.last_output_at);
    const pick = printed[0] || running[0] || lanes.find(l => l.state === 'fail') || lanes[0];
    return pick.name;
  }
  function model(v, now) {
    if (!v) return null;
    const running = v.state === 'running', status = String(v.status || '').toUpperCase();
    const lanes = list(v.lanes).filter(l => l && typeof l === 'object').map(l => laneRow(l, now));
    const failed = !running && (status === 'FAIL' || lanes.some(l => l.state === 'fail'));
    const live = running && v.live ? {
      elapsed: dur(since(v.live.started, now)),
      step: v.live.step && v.live.step.name ? v.live.step.name : null,
      stepElapsed: v.live.step && num(v.live.step.started) !== null ? dur(since(v.live.step.started, now)) : null,
      last_line: v.live.last_line || null,
      idle: idleWarning(v.live.idle_s),
    } : null;
    return {
      running, failed, passed: !running && !failed && status === 'PASS', mode: v.mode === 'lanes' ? 'lanes' : 'single',
      live, lanes, groups: groupFailing(v.failing), mutated: v.mutated === true,
      reportless: failed ? lanes.filter(l => l.state === 'fail' && !l.hasTests).map(l => l.name) : [],
      flaky: lanes.filter(l => l.state === 'flaky').map(l => l.name),
      known: lanes.filter(l => l.state === 'known').map(l => l.name),
      // a single command that ended: the step it was in, from its output's step markers
      endStep: !running && v.mode !== 'lanes' && v.state ? openStep(v.log_tail) : null,
    };
  }
  const runsBase = key => `/api/runs/${encodeURIComponent(key)}/verification`;
  // the log endpoint of the single command (lane null), a lane, or one run k of a lane
  function logPath(key, lane, k) {
    if (!lane) return runsBase(key) + '/log';
    return `${runsBase(key)}/lanes/${encodeURIComponent(lane)}${num(k) !== null ? '/runs/' + k : ''}/log`;
  }
  // the range that shows `line` with a few lines above it
  const linesPath = (key, lane, line) => `${logPath(key, lane)}/lines/${Math.max(1, line - CONTEXT_LINES)}`;
  // lines of a tail as [{n, text}], numbered from tail_first_line when the server knew it
  function tailLines(body) {
    const text = String(body && body.tail || ''), first = num(body && body.tail_first_line);
    const lines = text.endsWith('\n') ? text.slice(0, -1).split('\n') : text.split('\n');
    return (text ? lines : []).map((t, i) => ({ n: first === null ? null : first + i, text: t }));
  }

  /* ---------- DOM ---------- */
  const views = new Map();                 // run key → { lane (followed by the tail), open: {lane, line} | null }
  const cache = new Map();                 // path → { data, error, pending, fresh }
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  const chip = (text, cls) => el('span', text || '—', 'chip st-' + cls);
  function section(parent, title, cls) {
    const s = el('section', undefined, 'insp-section' + (cls ? ' ' + cls : ''));
    s.append(el('h3', title)); parent.append(s); return s;
  }
  function button(text, onClick, cls) {
    const b = el('button', text, cls || 'vfy-link'); b.type = 'button';
    b.addEventListener('click', onClick); return b;
  }
  // a fetch that lands once per `fresh` token and re-renders the view when it does
  function load(ctx, path, fresh) {
    const c = cache.get(path);
    if (c && (c.fresh === fresh || c.pending)) return c;
    const entry = { pending: true, fresh, data: c ? c.data : null, error: null };
    cache.set(path, entry);
    ctx.fetchJSON(path).then(data => { entry.data = data; entry.error = null; }, err => { entry.error = err; })
      .finally(() => { entry.pending = false; ctx.rerender(); });
    return entry;
  }
  function logBlock(parent, rows, mark, scrollKey) {
    const pre = el('pre', undefined, 'vfy-log'); pre.dataset.scroll = scrollKey;
    for (const r of rows) {
      const line = el('span', undefined, 'vfy-line' + (mark !== null && r.n === mark ? ' vfy-hl' : ''));
      if (r.n !== null) line.append(el('span', r.n, 'vfy-ln'));
      line.append(el('span', r.text, 'vfy-lt'));
      pre.append(line);
    }
    if (!rows.length) pre.append(el('span', '(empty)', 'vfy-lt'));
    parent.append(pre);
    return pre;
  }
  function tail(parent, ctx, lane, fresh, scrollKey) {
    const entry = load(ctx, logPath(ctx.run.key, lane), fresh);
    if (!entry.data) {
      const why = !entry.error ? 'Reading the log…' : entry.error.snapshot ? 'This export holds no logs.'
        : entry.error.status === 404 ? 'No log to show yet.' : 'Could not read the log.';
      parent.append(el('p', why, 'muted'));
      return;
    }
    if (entry.data.truncated) parent.append(el('p', `the last ${Math.round(String(entry.data.tail || '').length / 1024)} KB`, 'muted vfy-note'));
    logBlock(parent, tailLines(entry.data), null, scrollKey);
  }
  function openedLog(parent, ctx, open) {
    const s = section(parent, `Log of ${open.lane} at line ${open.line}`, 'vfy-opened');
    const head = el('div', undefined, 'vfy-section-head');
    head.append(s.firstChild, button('Close the log', () => { views.get(ctx.run.key).open = null; ctx.rerender(); }, 'vfy-close'));
    s.prepend(head);
    const entry = load(ctx, linesPath(ctx.run.key, open.lane, open.line), 'once');
    if (entry.error) {
      // an export holds no line ranges: the tail, with the line marked when it is in it
      const back = load(ctx, logPath(ctx.run.key, open.lane), 'once');
      if (!back.data) { s.append(el('p', back.error ? 'This log is not available here.' : 'Reading the log…', 'muted')); return; }
      const rows = tailLines(back.data);
      if (!rows.some(r => r.n === open.line)) s.append(el('p', `Line ${open.line} is not in this export; this is the end of the log.`, 'muted vfy-note'));
      reveal(open, logBlock(s, rows, open.line, 'vfy-opened-log'));
      return;
    }
    if (!entry.data) { s.append(el('p', 'Reading the log…', 'muted')); return; }
    const first = num(entry.data.first_line) || 1;
    const pre = logBlock(s, list(entry.data.lines).map((t, i) => ({ n: first + i, text: t })), open.line, 'vfy-opened-log');
    if (entry.data.truncated) s.append(el('p', 'The log goes on past these lines.', 'muted vfy-note'));
    reveal(open, pre);
  }
  // Bring the marked line into view once, after the inspector put its own scroll back.
  function reveal(open, pre) {
    if (open.revealed || !pre.querySelector('.vfy-hl')) return;
    open.revealed = true;
    setTimeout(() => { if (pre.isConnected) pre.querySelector('.vfy-hl')?.scrollIntoView({ block: 'center' }); }, 0);
  }
  function liveBlock(parent, m) {
    const box = el('div', undefined, 'vfy-live');
    const row = el('p', undefined, 'vfy-now');
    row.append(el('span', 'running for ', 'muted'), el('span', m.live.elapsed, 'vfy-num'));
    if (m.live.step) {
      row.append(el('span', ' · step ', 'muted'), el('span', m.live.step, 'mono vfy-step'));
      if (m.live.stepElapsed) row.append(el('span', ' for ', 'muted'), el('span', m.live.stepElapsed, 'vfy-num'));
    }
    box.append(row);
    if (m.live.last_line) box.append(el('p', m.live.last_line, 'mono vfy-last'));
    if (m.live.idle) box.append(el('p', m.live.idle, 'warn vfy-idle'));
    parent.append(box);
  }
  function failingBlock(parent, ctx, m, view) {
    const total = m.groups.reduce((n, g) => n + g.entries.length, 0);
    const s = section(parent, `Failing tests (${total})`, 'vfy-failing');
    for (const g of m.groups) {
      const box = el('div', undefined, 'vfy-group');
      const h = el('h4', undefined, 'vfy-group-head');
      h.append(el('span', g.lane || 'the command', 'mono'), el('span', [g.fresh ? `${g.fresh} new` : '', g.known ? `${g.known} known` : ''].filter(Boolean).join(' · '), 'muted vfy-num'));
      box.append(h);
      const ul = el('ul', undefined, 'plain vfy-tests');
      for (const e of g.entries) {
        const li = el('li', undefined, 'vfy-test vfy-' + e.badge);
        const top = el('div', undefined, 'vfy-test-head');
        top.append(el('span', e.badge, 'vfy-badge vfy-badge-' + e.badge), el('span', e.name, 'mono vfy-test-name'));
        if (e.log_line !== null && g.lane) {
          top.append(button(`open in log (line ${e.log_line})`, () => { view.open = { lane: g.lane, line: e.log_line }; ctx.rerender(); }));
        }
        li.append(top);
        if (e.message) li.append(el('p', e.message, 'vfy-message'));
        if (e.excerpt) {
          const d = el('details', undefined, 'vfy-excerpt'); d.dataset.keep = `vfy-${g.lane}-${e.name}`;
          d.append(el('summary', 'excerpt'), el('pre', e.excerpt));
          li.append(d);
        }
        ul.append(li);
      }
      box.append(ul);
      s.append(box);
    }
    return s;
  }
  function lanesTable(parent, ctx, m, view, followed) {
    const s = section(parent, `Lanes (${m.lanes.length})`, 'vfy-lanes');
    const wrap = el('div', undefined, 'vfy-table-wrap'); wrap.dataset.scroll = 'vfy-lanes';
    const table = el('table', undefined, 'vfy-table');
    const head = el('tr');
    for (const h of ['Lane', 'State', 'Time', 'Runs', 'Tests']) head.append(el('th', h));
    const thead = el('thead'); thead.append(head);
    const body = el('tbody');
    for (const l of m.lanes) {
      const tr = el('tr', undefined, l.name === followed ? 'vfy-followed' : '');
      const name = el('td', undefined, 'vfy-lane');
      name.append(button(l.name, () => { view.lane = l.name; ctx.rerender(); }, 'vfy-link mono'));
      for (const label of l.labels) name.append(el('span', label, 'plate vfy-label'));
      if (l.step || l.last_line) {
        const now = el('div', undefined, 'vfy-lane-now');
        if (l.step) now.append(el('span', 'step ' + l.step, 'mono vfy-step'));
        if (l.last_line) now.append(el('span', l.last_line, 'mono vfy-last'));
        name.append(now);
      }
      if (l.idle) name.append(el('div', l.idle, 'warn vfy-idle'));
      const state = el('td'); state.append(chip(l.state, l.chip));
      tr.append(name, state, el('td', l.time, 'vfy-num vfy-nowrap'), el('td', l.runs, 'vfy-num vfy-nowrap'), el('td', l.counts || '', 'vfy-num'));
      body.append(tr);
    }
    table.append(thead, body); wrap.append(table); s.append(wrap);
  }
  function render(host, ctx) {
    const run = ctx.run, v = run.verification, m = model(v, num(ctx.now) !== null ? ctx.now : Date.now() / 1000);
    if (!m) return;
    if (!views.has(run.key)) views.set(run.key, { lane: null, open: null });
    const view = views.get(run.key);
    // logs are read again when the run's files moved: every poll while the verification prints
    const fresh = `${run.updated}|${v.live ? v.live.last_output_at : ''}`;
    if (m.running && m.live) liveBlock(host, m);
    if (m.failed && m.groups.length) failingBlock(host, ctx, m, view);
    if (view.open && list(v.lanes).some(l => l.name === view.open.lane)) openedLog(host, ctx, view.open);
    if (m.mutated) host.append(el('p', 'A lane changed the checkout, so no lane was rerun after it.', 'warn vfy-mutated'));
    for (const name of m.reportless) {
      const s = section(host, `${name}: no test report; see the log`, 'vfy-reportless');
      tail(s, ctx, name, fresh, 'vfy-tail-' + name);
    }
    if (m.endStep) host.append(el('p', `It ended in step ${m.endStep}.`, 'vfy-endstep'));
    if (m.passed && m.lanes.length) {
      const p = el('p', undefined, 'vfy-summary');
      p.append(el('span', `${m.lanes.length} lane${m.lanes.length === 1 ? '' : 's'} passed`));
      if (m.flaky.length) p.append(el('span', ` · flaky (passed on a rerun): ${m.flaky.join(', ')}`, 'warn'));
      if (m.known.length) p.append(el('span', ` · known failures only: ${m.known.join(', ')}`, 'muted'));
      host.append(p);
    }
    const followed = m.running ? tailLane(v, view.lane) : view.lane;
    if (m.lanes.length) lanesTable(host, ctx, m, view, followed);
    if (m.running) {
      const s = section(host, m.mode === 'lanes' && followed ? `Live output: ${followed}` : 'Live output', 'vfy-tail');
      tail(s, ctx, m.mode === 'lanes' && followed ? followed : null, fresh, 'vfy-live-tail');
    } else if (m.mode === 'lanes' && view.lane) {
      const s = section(host, `Output: ${view.lane}`, 'vfy-tail');
      tail(s, ctx, view.lane, fresh, 'vfy-lane-tail');
    }
  }

  return { render, model, laneRow, groupFailing, idleWarning, openStep, countsText, runsText, tailLane, logPath, linesPath,
    tailLines, dur, IDLE_WARN_S };
});
