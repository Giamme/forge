/* forge-ui checks and where each task stands (window.ForgeChecks, and a CommonJS module under node:test).
   - Now: what a running task does (the agent at work, the call it is making and since when, how long its log has been
     quiet), or why a task that is not running waits (RunDetail task.now, task.waiting and run.scheduler in
     ui/docs/api.md): a cell of the task table, a section of the task inspector, the run inspector's scheduler fields.
   - Checks: the tests, type checks, lints and builds each dwarf and reviewer ran, attempt by attempt, against the
     commands the brief's acceptance criteria name (/tasks/<id>/checks, /executions/<name>/checks).
   - Environment: a banner in the run header for each environment problem (Docker, a sandbox's EPERM, a port in use…)
     that several tasks met (/environment).
   Times that move between polls (an elapsed time, a countdown, a quiet log) are elements with data-since, data-until
   or data-quiet; tick(root, now) brings them up to date, and app.js calls it on every refresh.
   The pure helpers are tested in ui/tests/js/checksview.test.js. Commands, output lines and task ids are untrusted:
   they only ever enter the page as text.
   ctx = { run, now (epoch seconds), fetchJSON(path), rerender(), hash(sel), onSelect(sel), convoHash(sel, event) } */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeChecks = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const QUIET_WARN_S = 300;                // a log quiet past this is flagged, as the verification view flags its own
  const ENV_EVERY_S = 15;                  // a running run's environment problems are read again at most this often
  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const ROLE = { dwarf: 'dwarf', qa: 'reviewer', task: 'task', solo: 'run' };
  const KIND = { test: 'tests', typecheck: 'type check', lint: 'lint', build: 'build' };
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  /* ---------- pure ---------- */
  function dur(s) {
    s = num(s); if (s === null) return '—';
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
    return Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm';
  }
  const since = (start, now) => num(start) !== null && num(now) !== null ? Math.max(0, now - start) : null;
  const firstLine = text => (String(text || '').split('\n').find(line => line.trim()) || '').trim();
  // "quiet 12 min" once a log has said nothing for over five minutes, else null
  function quietText(lastOutput, now) {
    const q = since(lastOutput, now);
    return q !== null && q > QUIET_WARN_S ? `quiet ${Math.floor(q / 60)} min` : null;
  }
  // "resumes in 2m10s", "resuming" once its time has come, or null without a time
  function untilText(until, now) {
    if (num(until) === null || num(now) === null) return null;
    return until > now ? 'resumes in ' + dur(until - now) : 'resuming';
  }
  // What a running task does now: {who, role, execution, attempt, phase, phaseSince, call, quietSince}; call is
  // {text (its first line, or the tool), command, check, since} or null while the agent thinks or writes.
  function nowModel(task) {
    const n = task && task.now;
    if (!n || typeof n !== 'object') return null;
    const call = n.call && typeof n.call === 'object' ? n.call : null;
    return {
      who: ROLE[n.role] || String(n.role || 'task'), role: n.role || null, execution: n.execution || null, attempt: num(n.attempt),
      phase: n.phase || null, phaseSince: num(n.phase_started),
      call: call ? { text: firstLine(call.command) || call.tool || call.kind || 'a call', command: call.command || null,
        tool: call.tool || null, check: call.check || null, since: num(call.started) } : null,
      quietSince: num(n.last_output_at),
    };
  }
  function scopeText(w) {
    const scope = String(w.scope || '');
    if (scope === 'all') return 'every model';
    if (scope.startsWith('model:')) return `model ${w.model || scope.slice(6)}${w.harness ? ' on ' + w.harness : ''}`;
    if (scope.startsWith('harness:')) return `harness ${w.harness || scope.slice(8)}`;
    return scope || 'its model';
  }
  // Why a task waits: {kind, text, tasks: [{id, stopped}], until, exhausted, split}, or null
  function waitingModel(w) {
    if (!w || typeof w !== 'object') return null;
    const ids = v => list(v).map(String);
    const out = { kind: w.kind, tasks: [], until: null, exhausted: false, split: null };
    if (w.kind === 'deps') {
      const stopped = new Set(ids(w.stopped));
      return { ...out, text: 'waits on', tasks: ids(w.on).map(id => ({ id, stopped: stopped.has(id) })) };
    }
    if (w.kind === 'overlap') return { ...out, text: 'shares files with', tasks: ids(w.on).map(id => ({ id, stopped: false })) };
    if (w.kind === 'slot') return { ...out, text: `no free slot: ${num(w.running) ?? '?'} of ${num(w.max_parallel) ?? '?'} running` };
    if (w.kind === 'drained') return { ...out, text: 'drained: nothing new starts until the run resumes' };
    if (w.kind === 'stopping') return { ...out, text: w.mode === 'now' ? 'the run is stopping now' : 'the run stops once its running tasks end' };
    if (w.kind === 'next') return { ...out, text: "starts at the scheduler's next turn" };
    if (w.kind === 'infra') {
      return { ...out, text: `paused: ${scopeText(w)}${w.class ? ' · ' + String(w.class).replace(/_/g, ' ') : ''}`,
        until: w.exhausted === true ? null : num(w.until), exhausted: w.exhausted === true };
    }
    if (w.kind === 'escalation') {
      return { ...out, text: `stopped by the retry policy${num(w.attempts) !== null ? ` after ${plural(w.attempts, 'attempt')}` : ''}${w.model ? ' on ' + w.model : ''}`,
        split: w.split || null };
    }
    return null;
  }
  // The Now column's order: running, then what starts soonest, then what waits on others, then what is held
  const RANK = { running: 0, next: 1, slot: 2, overlap: 2, deps: 3, infra: 4, drained: 5, stopping: 5, escalation: 6 };
  function nowRank(task) {
    if (task && task.now) return RANK.running;
    const w = task && task.waiting;
    return w && RANK[w.kind] !== undefined ? RANK[w.kind] : null;
  }
  function countsText(c) {
    if (!c || typeof c !== 'object') return null;
    if ('errors' in c || 'warnings' in c) {
      const parts = [plural(num(c.errors) || 0, 'error')];
      if (num(c.warnings)) parts.push(plural(c.warnings, 'warning'));
      return parts.join(' · ');
    }
    const parts = [`${num(c.passed) || 0} passed`];
    if (num(c.failed)) parts.push(`${c.failed} failed`);
    if (num(c.skipped)) parts.push(`${c.skipped} skipped`);
    return parts.join(' · ');
  }
  // One check as a row: {index, state (pass, fail, running, unknown), kind, command, full, exit, time, since, counts, tail}
  function checkRow(c, now) {
    const running = c.running === true;
    const state = running ? 'running' : c.failed === true ? 'fail' : c.exit === 0 ? 'pass' : num(c.exit) !== null ? 'fail' : 'unknown';
    const took = running ? since(c.started, now) : num(c.duration_s);
    return { index: num(c.index), state, kind: KIND[c.kind] || String(c.kind || 'check'), command: firstLine(c.command),
      full: String(c.command || ''), exit: !running && num(c.exit) !== null ? 'exit ' + c.exit : null,
      time: took === null ? null : dur(took), since: running ? num(c.started) : null, counts: countsText(c.counts), tail: c.tail || null };
  }
  // Which acceptance-criteria checks an attempt's agents ran: {total, ran, similar: [cmd], missing: [cmd], text}, or null
  function coverageModel(criteria) {
    const items = list(criteria).filter(c => c && typeof c === 'object');
    if (!items.length) return null;
    const of = status => items.filter(c => c.status === status).map(c => String(c.command));
    const ran = of('ran').length;
    return { total: items.length, ran, similar: of('similar'), missing: of('missing'),
      text: `${ran} of ${items.length} acceptance-criteria check${items.length === 1 ? '' : 's'} ran as written` };
  }
  // The environment problems that several tasks met, for the run header's banner
  const bannerProblems = (env, least = 2) => list(env && env.problems).filter(p => p && list(p.tasks).length >= least);
  // The run inspector's scheduler fields: [[label, text]], or null when no scheduler runs the plan
  function schedulerModel(run) {
    const s = run && run.scheduler;
    if (!s || s.live !== true) return null;
    const tasks = list(run.tasks), count = kind => tasks.filter(t => t.waiting && t.waiting.kind === kind).length;
    const state = s.stopping ? (s.stopping === 'now' ? 'stopping now' : 'stopping once its running tasks end') : s.drained ? 'drained: nothing new starts' : 'dispatching';
    const waiting = [['deps', 'on dependencies'], ['overlap', 'on shared files'], ['slot', 'for a free slot'], ['infra', 'paused'],
      ['next', 'next'], ['escalation', 'stopped by the retry policy']]
      .map(([kind, label]) => [count(kind), label]).filter(([n]) => n).map(([n, label]) => `${n} ${label}`);
    const rows = [['Scheduler', state], ['Running', String(tasks.filter(t => t.status === 'RUNNING').length) + (num(s.max_parallel) !== null ? ` of ${s.max_parallel} slots` : '')]];
    if (waiting.length) rows.push(['Waiting', waiting.join(' · ')]);
    return rows;
  }

  /* ---------- DOM ---------- */
  const cache = new Map();                 // path → { data, error, pending, fresh, at }
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function section(parent, title, cls) {
    const s = el('section', undefined, 'insp-section' + (cls ? ' ' + cls : ''));
    s.append(el('h3', title)); parent.append(s); return s;
  }
  function load(ctx, path, fresh) {
    const c = cache.get(path);
    if (c && (c.fresh === fresh || c.pending)) return c;
    const entry = { pending: true, fresh, data: c ? c.data : null, error: null, at: Date.now() };
    cache.set(path, entry);
    for (const old of cache.keys()) if (cache.size > 40 && old !== path) cache.delete(old);
    ctx.fetchJSON(path).then(data => { entry.data = data; entry.error = null; }, err => { entry.error = err; })
      .finally(() => { entry.pending = false; ctx.rerender(); });
    return entry;
  }
  const enc = encodeURIComponent;
  const taskPath = (run, id) => `/api/runs/${enc(run.key)}/tasks/${enc(id)}/checks`;
  const execPath = (run, name) => `/api/runs/${enc(run.key)}/executions/${enc(name)}/checks`;
  // a finished task's checks never change; a running one's are read again when the run moved
  const freshness = (run, task) => task && task.status === 'RUNNING' ? 'live:' + run.updated : `done:${task && task.status}:${task && task.attempt}`;
  function timeNode(start, now, cls) {
    const n = el('span', dur(since(start, now)), 'vfy-num ' + (cls || 'now-time'));
    if (num(start) !== null) n.dataset.since = String(start);
    return n;
  }
  function quietNode(lastOutput, now) {
    const t = quietText(lastOutput, now), n = el('span', t || '', 'warn now-quiet');
    n.hidden = !t;
    if (num(lastOutput) !== null) n.dataset.quiet = String(lastOutput);
    return n;
  }
  function untilNode(until, now) {
    const n = el('span', untilText(until, now), 'vfy-num now-until');
    n.dataset.until = String(until);
    return n;
  }
  function taskLinks(parent, ctx, tasks) {
    tasks.forEach((t, i) => {
      if (i) parent.append(document.createTextNode(', '));
      const a = el('a', t.id, 'mono'); a.href = ctx.hash({ kind: 'task', id: t.id });
      parent.append(a);
      if (t.stopped) parent.append(el('span', ' (stopped)', 'warn'));
    });
  }
  // Brings the times under `root` up to `now` (epoch seconds) between redraws.
  function tick(rootNode, now) {
    if (!rootNode || !rootNode.querySelectorAll || num(now) === null) return;
    for (const n of rootNode.querySelectorAll('[data-since]')) n.textContent = dur(since(Number(n.dataset.since), now));
    for (const n of rootNode.querySelectorAll('[data-until]')) n.textContent = untilText(Number(n.dataset.until), now) || '';
    for (const n of rootNode.querySelectorAll('[data-quiet]')) { const t = quietText(Number(n.dataset.quiet), now); n.textContent = t || ''; n.hidden = !t; }
  }

  // The task table's Now cell
  function cell(task, now, ctx) {
    const box = el('span', undefined, 'now-cell');
    const m = nowModel(task);
    if (m) {
      if (m.call) {
        const code = el('code', m.call.text, 'now-call'); code.title = m.call.command || m.call.text;
        box.append(code, timeNode(m.call.since, now));
      } else box.append(el('span', `${m.who}${m.phase ? ' · ' + m.phase : ''}`, 'now-who'), timeNode(m.phaseSince, now));
      box.append(quietNode(m.quietSince, now));
      return box;
    }
    const w = waitingModel(task && task.waiting);
    if (!w) { box.append(el('span', '—', 'muted')); return box; }
    box.classList.add('now-waiting');
    box.append(el('span', w.text + (w.tasks.length ? ' ' : ''), 'now-why'));
    taskLinks(box, ctx, w.tasks);
    if (w.until !== null) box.append(untilNode(w.until, now));
    if (w.exhausted) box.append(el('span', 'waits used up', 'warn'));
    return box;
  }

  // The task inspector's Now section: what it does, or why it waits
  function nowSection(host, ctx, task) {
    const m = nowModel(task), w = m ? null : waitingModel(task.waiting);
    if (!m && !w) return;
    const s = section(host, 'Now', 'now-section');
    const box = el('div', undefined, 'vfy-live now-live');
    if (m) {
      const exec = list(task.executions).find(e => e.id === m.execution);
      const who = el('p', undefined, 'vfy-now');
      who.append(el('span', m.who, 'now-role'));
      const spec = exec && exec.model && exec.model.spec;
      if (spec && root.ForgeIcons) who.append(document.createTextNode(' '), root.ForgeIcons.modelPlate(spec));
      if (m.attempt !== null) who.append(el('span', ` · attempt ${m.attempt}`, 'muted'));
      if (m.phase) who.append(el('span', ` · ${m.phase} for `, 'muted'), timeNode(m.phaseSince, ctx.now));
      box.append(who);
      const call = el('p', undefined, 'vfy-now now-doing');
      if (m.call) {
        call.append(el('span', m.call.command ? 'runs ' : 'calls ', 'muted'));
        const code = el('code', m.call.text, 'now-call'); code.title = m.call.command || m.call.text;
        call.append(code);
        if (m.call.check) call.append(el('span', KIND[m.call.check] || m.call.check, 'tag'));
        call.append(el('span', ' for ', 'muted'), timeNode(m.call.since, ctx.now));
      } else if (m.role === 'dwarf' || m.role === 'qa') call.append(el('span', 'no call running: the model is thinking or writing', 'muted'));
      if (call.childNodes.length) box.append(call);
      if (m.quietSince !== null) {
        const last = el('p', undefined, 'vfy-now muted');
        last.append(document.createTextNode('last output '), timeNode(m.quietSince, ctx.now), document.createTextNode(' ago'));
        box.append(last);
      }
      const quiet = quietNode(m.quietSince, ctx.now); quiet.classList.add('vfy-idle');
      box.append(quiet);
      if (m.execution && (m.role === 'dwarf' || m.role === 'qa') && ctx.convoHash) {
        const a = el('a', 'Follow it in its conversation →', 'diff-link small');
        a.href = ctx.convoHash({ kind: 'execution', task: task.id === 'solo' ? null : task.id, id: task.id, exec: m.execution });
        box.append(a);
      }
    } else {
      const p = el('p', undefined, 'vfy-now');
      p.append(el('span', w.text + (w.tasks.length ? ' ' : '')));
      taskLinks(p, ctx, w.tasks);
      if (w.until !== null) p.append(document.createTextNode(' · '), untilNode(w.until, ctx.now));
      if (w.exhausted) p.append(el('span', ' · its waits are used up', 'warn'));
      box.append(p);
      if (w.split) {
        const d = el('details', undefined, 'now-split'); d.dataset.keep = `now-split-${task.id}`;
        d.append(el('summary', 'The split it suggests'), el('pre', w.split));
        box.append(d);
      }
    }
    s.append(box);
  }

  // One execution's checks, its environment problems and, while it runs, its call
  function execChecks(parent, ctx, task, data) {
    const rows = list(data && data.checks).map(c => checkRow(c, ctx.now));
    const target = { kind: 'execution', task: task && task.id !== 'solo' ? task.id : null, id: task ? task.id : null, exec: data.execution };
    if (rows.length) {
      const ul = el('ul', undefined, 'plain checks');
      for (const r of rows) {
        const li = el('li', undefined, 'check check-' + r.state);
        const head = el('div', undefined, 'check-head');
        head.append(el('i', undefined, 'lamp st-' + (r.state === 'unknown' ? 'unknown' : r.state)), el('span', r.kind, 'tag'));
        const cmd = r.index !== null && ctx.convoHash ? el('a', r.command, 'mono check-cmd') : el('span', r.command, 'mono check-cmd');
        if (cmd.tagName === 'A') cmd.href = ctx.convoHash(target, r.index);
        cmd.title = r.full;
        head.append(cmd);
        const meta = el('span', undefined, 'check-meta vfy-num');
        if (r.exit) meta.append(el('span', r.exit, r.state === 'fail' ? 'check-exit warn' : 'check-exit'));
        if (r.since !== null) meta.append(timeNode(r.since, ctx.now));
        else if (r.time) meta.append(el('span', r.time));
        if (r.counts) meta.append(el('span', r.counts, 'check-counts'));
        if (meta.childNodes.length) head.append(meta);
        li.append(head);
        if (r.tail) {
          const d = el('details', undefined, 'check-tail'); d.dataset.keep = `check-${data.execution}-${r.index}`;
          d.append(el('summary', 'last lines'), el('pre', r.tail));
          li.append(d);
        }
        ul.append(li);
      }
      parent.append(ul);
    } else if (data.state === 'done') parent.append(el('p', 'ran no tests, type checks or lints', 'muted checks-none'));
    for (const hit of list(data.environment)) {
      const p = el('p', undefined, 'check-env');
      p.append(el('span', hit.label || hit.problem, 'check-env-what'));
      const cmd = ctx.convoHash ? el('a', firstLine(hit.command), 'mono') : el('span', firstLine(hit.command), 'mono');
      if (ctx.convoHash) cmd.href = ctx.convoHash(target, hit.index);
      p.append(cmd);
      if (hit.line) p.title = hit.line;
      parent.append(p);
    }
  }

  function coverageBlock(parent, cov, running) {
    if (!cov) return;
    const box = el('div', undefined, 'checks-criteria');
    box.append(el('p', cov.text, cov.missing.length && !running ? 'warn checks-cov' : 'muted checks-cov'));
    const items = [...cov.missing.map(c => [c, running ? 'not run yet' : 'not run']), ...cov.similar.map(c => [c, 'run another way'])];
    if (items.length) {
      const ul = el('ul', undefined, 'plain checks-missing');
      for (const [command, why] of items) {
        const li = el('li');
        li.append(el('span', why + ': ', why === 'not run' ? 'warn' : 'muted'), el('code', command, 'mono'));
        ul.append(li);
      }
      box.append(ul);
    }
    parent.append(box);
  }

  // The task inspector's attempts get their checks: one lazy read for the whole task.
  function taskChecks(ctx, task) {
    if (!ctx.fetchJSON || !task) return null;
    return load(ctx, taskPath(ctx.run, task.id), freshness(ctx.run, task));
  }
  // under an attempt's dwarf or reviewer line
  function underExecution(parent, ctx, task, entry, attempt, exec) {
    const a = entry && entry.data && list(entry.data.attempts).find(x => x.attempt === attempt);
    const data = a && list(a.executions).find(x => x.execution === exec);
    if (!data) return;
    const box = el('div', undefined, 'checks-of');
    execChecks(box, ctx, task, data);
    if (box.childNodes.length) parent.append(box);
  }
  // at the foot of an attempt's box
  function attemptCriteria(parent, entry, attempt) {
    const a = entry && entry.data && list(entry.data.attempts).find(x => x.attempt === attempt);
    if (a) coverageBlock(parent, coverageModel(a.criteria), a.running === true);
  }
  function loadingLine(parent, entry) {
    if (!entry) return;
    if (entry.error && !entry.data) {
      parent.append(el('p', entry.error.snapshot ? 'The checks are not in this export: it was made without prompts and texts.'
        : `Could not read the checks${entry.error.status === 404 ? ': this server predates them (forge ui open restarts it)' : ''}.`, 'muted'));
    } else if (!entry.data) parent.append(el('p', 'Reading the checks each agent ran…', 'muted'));
  }

  // The execution inspector's Checks section
  function execSection(host, ctx, task, sel, brief) {
    const role = brief ? brief.role : String(sel.exec).split('-')[0];
    if (role !== 'dwarf' && role !== 'qa') return;
    const s = section(host, 'Checks', 'checks-section');
    const fresh = brief && brief.state === 'done' ? 'done' : 'live:' + ctx.run.updated;
    const entry = load(ctx, execPath(ctx.run, sel.exec), fresh);
    loadingLine(s, entry);
    const d = entry.data;
    if (!d) return;
    if (d.call && d.state === 'running') {
      const p = el('p', undefined, 'vfy-now now-doing');
      p.append(el('span', d.call.command ? 'runs ' : 'calls ', 'muted'), el('code', firstLine(d.call.command) || d.call.tool || 'a call', 'now-call'),
        el('span', ' for ', 'muted'), timeNode(d.call.started, ctx.now));
      s.append(p);
    }
    execChecks(s, ctx, task, d);
    coverageBlock(s, coverageModel(d.criteria), d.state === 'running');
  }

  // The run inspector's scheduler fields, under its timeline
  function schedulerFields(parent, run, now) {
    const rows = schedulerModel(run);
    if (!rows) return;
    const dl = el('dl', undefined, 'fields');
    for (const [label, value] of rows) { dl.append(el('dt', label), el('dd', value)); }
    for (const p of list(run.scheduler.pauses)) {
      const w = waitingModel({ ...p, kind: 'infra' });
      const dd = el('dd', w.text);
      if (w.until !== null) dd.append(document.createTextNode(' · '), untilNode(w.until, now));
      if (w.exhausted) dd.append(el('span', ' · its waits are used up', 'warn'));
      dl.append(el('dt', 'Paused'), dd);
    }
    parent.append(dl);
  }

  // The run header's banner: environment problems more than one task met
  const banners = new Map();               // run key → { entry, updated, at }
  function banner(head, ctx) {
    const run = ctx.run, path = `/api/runs/${enc(run.key)}/environment`;
    const running = list(run.tasks).some(t => t.status === 'RUNNING');
    const c = cache.get(path);
    // once per change of the run, and while it runs at most every fifteen seconds
    const due = !c || (c.fresh !== run.updated && (!running || Date.now() - c.at > ENV_EVERY_S * 1000));
    const entry = due ? load(ctx, path, run.updated) : c;
    const problems = bannerProblems(entry && entry.data);
    if (!problems.length) return;
    const box = el('div', undefined, 'env-banner'); box.setAttribute('role', 'note');
    box.append(el('span', 'Environment', 'plate'));
    const ul = el('ul', undefined, 'plain env-list');
    for (const p of problems) {
      const li = el('li', undefined, 'env-problem');
      const head2 = el('span', undefined, 'env-head');
      head2.append(el('strong', p.label || p.problem), el('span', ` in ${plural(list(p.tasks).length, 'task')}: `, 'muted'));
      list(p.tasks).forEach((id, i) => {
        if (i) head2.append(document.createTextNode(', '));
        const a = el('a', id, 'mono'); a.href = ctx.hash({ kind: 'task', id });
        head2.append(a);
      });
      li.append(head2);
      const d = el('details', undefined, 'env-hits'); d.dataset.keep = `env-${p.problem}`;
      d.append(el('summary', `${plural(num(p.count) || list(p.hits).length, 'failed command')}, what they said`));
      const hits = el('ul', undefined, 'plain env-hit-list');
      for (const h of list(p.hits)) {
        const item = el('li');
        const where = el('span', `${h.task} · attempt ${h.attempt ?? '?'} · ${ROLE[h.role] || h.role}: `, 'muted');
        const cmd = ctx.convoHash ? el('a', firstLine(h.command), 'mono') : el('span', firstLine(h.command), 'mono');
        if (ctx.convoHash) cmd.href = ctx.convoHash({ kind: 'execution', task: h.task === 'solo' ? null : h.task, id: h.task, exec: h.execution }, h.index);
        item.append(where, cmd);
        if (h.line) item.append(el('div', h.line, 'mono env-line'));
        hits.append(item);
      }
      d.append(hits);
      li.append(d);
      ul.append(li);
    }
    box.append(ul);
    head.append(box);
  }

  return { dur, quietText, untilText, nowModel, waitingModel, nowRank, countsText, checkRow, coverageModel, bannerProblems,
    schedulerModel, tick, cell, nowSection, taskChecks, underExecution, attemptCriteria, loadingLine, execSection,
    schedulerFields, banner, QUIET_WARN_S, cache };
});
