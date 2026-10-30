/* forge-ui controls: the run and task panels that change a live forge run (window.ForgeControls, and a CommonJS
   module under node:test). panel.js mounts them in the inspector; every change goes through ForgeUI (uikit.js) to
   POST /api/runs/<key>/controls, which only ever calls `forge control` (ui/docs/api.md, "Controls (write endpoints)").
   The first half is pure: what a control shows (effective), whether it can change (canEdit), the request a draft
   becomes (prepare, bodyFor), and whether a rebuilt inspector takes a half-edited section back (keepEditing).
   The second half draws the keys and fields; all API values enter the DOM as text. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeControls = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const LEVELS = ['P0', 'P1', 'P2', 'P3'];
  const DEFAULT_LEVEL = 'P2';   // scripts/forge-contract.py DEFAULT_THRESHOLD
  const EFFORTS = ['default', 'low', 'medium', 'high', 'xhigh', 'max'];
  const MAX_FIELD = 256;                                     // control.py: a body string is at most this long
  const LIVE_ONLY = 'only while a run is in progress';
  const PIN_APPLIES = 'at the next full retry, not a QA-only resume';
  // The CLI's keys (scripts/forge-control.py KEYS): the label, when a change applies, and where it can be set.
  const KEYS = {
    'qa-threshold': { label: 'QA threshold', run: "at each task's next attempt", task: 'at its next attempt' },
    'timeout': { label: 'Timeout', run: "at each task's next attempt", planOnly: true },
    'escalate-after': { label: 'Escalate after', run: 'at the next retry decision', planOnly: true },
    'verify-retries': { label: 'Verify retries', run: 'at the next verification', planOnly: true },
    'retry-budget': { label: 'Retry budget', run: 'at the next retry decision', task: 'at the next retry decision', live: true },
    'infra-retries': { label: 'Infra retries', run: 'at the next retry decision', planOnly: true, live: true },
    'max-parallel': { label: 'Max parallel', run: 'at the next dispatch', planOnly: true, live: true },
    'infra-backoff': { label: 'Infra backoff', run: 'at the next infrastructure wait', planOnly: true },
    'escalate-effort': { label: 'Effort step', run: 'at the next retry decision', planOnly: true },
  };
  const BACKOFF_FORMS = 'exp:<base>[,<cap>], linear:<start>,<step>[,<cap>] or list:<a>,<b>,… (seconds)';
  // A task status after which "Retry now" applies (or the policy stopped it).
  const RETRYABLE = ['FAIL', 'UNKNOWN', 'TIMEOUT', 'ERROR', 'INTERRUPTED', 'BLOCKED', 'INVALIDATED'];   // scripts/forge-schedule.py RETRYABLE; NEEDS_INPUT is in neither list

  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const bad = error => ({ ok: false, error });

  /* ---------- pure: values, specs, requests ---------- */

  // The model spec alias[:effort[:harness]] from its three parts. The alias is required, the effort one of EFFORTS
  // ('default' leaves it out), the harness optional; whitespace or ':' inside a part is refused, and a leading '-'
  // (control.py takes none: the CLI would read it as an option).
  function specOf(parts) {
    const p = parts || {};
    const alias = String(p.alias === undefined || p.alias === null ? '' : p.alias).trim();
    const effort = String(p.effort === undefined || p.effort === null ? '' : p.effort).trim();
    const harness = String(p.harness === undefined || p.harness === null ? '' : p.harness).trim();
    const unsafe = s => /[\s:\u0000-\u001f\u007f]/.test(s);
    if (!alias) return bad('a model alias is required');
    if (unsafe(alias)) return bad('the alias cannot hold spaces or ":"');
    if (alias.startsWith('-')) return bad('the alias cannot start with "-"');
    if (effort && !EFFORTS.includes(effort)) return bad('the effort is one of ' + EFFORTS.join(', '));
    if (unsafe(harness)) return bad('the harness cannot hold spaces or ":"');
    const e = effort === 'default' ? '' : effort;
    const spec = harness ? `${alias}:${e}:${harness}` : e ? `${alias}:${e}` : alias;
    if (spec.length > MAX_FIELD) return bad(`the spec is longer than ${MAX_FIELD} characters`);
    return { ok: true, spec };
  }

  // A count the CLI takes: 1 to 9 digits (control.py `_int`).
  function countOf(text) {
    const t = String(text === undefined || text === null ? '' : text).trim();
    return /^[0-9]{1,9}$/.test(t) ? { ok: true, value: String(Number(t)) } : bad('needs a whole number, 0 to 999999999');
  }
  // Minutes in the UI, whole seconds to the CLI. 0 turns the dispatch watchdog off (forge-dispatch.sh --timeout 0).
  function secondsOf(text) {
    const t = String(text === undefined || text === null ? '' : text).trim();
    if (!/^[0-9]+(\.[0-9]+)?$/.test(t)) return bad('needs minutes, such as 45 or 7.5');
    const s = Math.round(Number(t) * 60);
    return s <= 999999999 ? { ok: true, value: String(s) } : bad('that is too many minutes');
  }
  const minutesOf = seconds => {
    const s = num(seconds);
    return s === null ? null : String(Math.round(s / 60 * 100) / 100);
  };

  // What the user typed (or pressed) for a key, as the CLI takes it. `task` is set for a task-level change.
  function valueFor(key, input, task) {
    const info = KEYS[key];
    if (!info) return bad('unknown control');
    if (task && info.planOnly) return bad(`${info.label} applies to the whole run, not a task`);
    const text = String(input === undefined || input === null ? '' : input).trim();
    if (key === 'qa-threshold') {
      return LEVELS.includes(text) || text === 'none' || (task ? text === 'inherit' : text === 'default') ? { ok: true, value: text }
        : bad(task ? 'P0, P1, P2, P3, none or inherit' : 'P0, P1, P2, P3, none or default');
    }
    if (task && key === 'retry-budget' && text === 'inherit') return { ok: true, value: 'inherit' };
    if (key === 'max-parallel') {
      return /^[0-9]{1,2}$/.test(text) && Number(text) >= 1 && Number(text) <= 64 ? { ok: true, value: String(Number(text)) } : bad('a whole number, 1 to 64');
    }
    if (key === 'escalate-effort') return text === 'on' || text === 'off' ? { ok: true, value: text } : bad('on or off');
    if (key === 'infra-backoff') {
      // The shape itself is the CLI's to check; here only what can never be one. "default" drops the setting.
      if (text === 'default' || text === 'none') return { ok: true, value: 'none' };
      return text && !/[\s\u0000-\u001f\u007f]/.test(text) && !text.startsWith('-') && text.length <= MAX_FIELD
        ? { ok: true, value: text } : bad('a backoff shape: ' + BACKOFF_FORMS);
    }
    return key === 'timeout' ? secondsOf(text) : countOf(text);
  }

  // The JSON body of POST /api/runs/<key>/controls (ui/docs/api.md). `value` is always a string.
  function bodyFor(action, a) {
    if (action === 'set') {
      const body = { action: 'set', key: a.key, value: String(a.value) };
      if (a.task) body.task = a.task;
      return body;
    }
    if (action === 'pin') return { action: 'pin', task: a.task, spec: a.spec };
    if (action === 'unpin') return { action: 'unpin', task: a.task };
    if (action === 'drain' || action === 'resume') return { action };
    if (action === 'stop') return a && a.now ? { action: 'stop', now: true } : { action: 'stop' };
    if (action === 'retry') return a.dwarf ? { action: 'retry', task: a.task, dwarf: a.dwarf } : { action: 'retry', task: a.task };
    throw new Error('unknown action ' + action);
  }

  const shown = (key, value, task) => key === 'timeout' ? `${minutesOf(Number(value))} min`
    : key === 'qa-threshold' && value === 'none' ? (task ? 'P3 for this task' : 'P3 (nothing tolerated)')
    : key === 'qa-threshold' && value === 'default' ? 'the default (P2)'
    : key === 'infra-backoff' && value === 'none' ? 'the default (exp:60)' : value;
  const inTask = task => task ? ` for task \`${task}\`` : '';

  // A draft change → {ok, body, confirm} or {ok: false, error}. change: {kind: 'set', key, input, task?} |
  // {kind: 'pin', task, alias, effort, harness} | {kind: 'unpin', task}.
  function prepare(change) {
    const c = change || {};
    if (c.kind === 'set') {
      const v = valueFor(c.key, c.input, c.task);
      if (!v.ok) return v;
      const info = KEYS[c.key], when = c.task ? info.task || info.run : info.run;
      return { ok: true, body: bodyFor('set', { key: c.key, value: v.value, task: c.task }),
        confirm: `Set ${info.label}${inTask(c.task)} to ${shown(c.key, v.value, c.task)}? Takes effect ${when}` };
    }
    if (c.kind === 'pin') {
      if (!c.task) return bad('no task');
      const s = specOf(c);
      return s.ok ? { ok: true, body: bodyFor('pin', { task: c.task, spec: s.spec }),
        confirm: `Pin task \`${c.task}\`'s next retry to \`${s.spec}\`? Takes effect ${PIN_APPLIES}` } : s;
    }
    if (c.kind === 'unpin') {
      return c.task ? { ok: true, body: bodyFor('unpin', { task: c.task }),
        confirm: `Remove the pin on task \`${c.task}\`? Its next retry goes back to the escalation policy` } : bad('no task');
    }
    if (c.kind === 'drain') return { ok: true, body: bodyFor('drain'), confirm: 'Drain the run? Running tasks finish; nothing new starts until you resume' };
    if (c.kind === 'resume') return { ok: true, body: bodyFor('resume'), confirm: 'Resume the run? The scheduler starts tasks again' };
    if (c.kind === 'stop') {
      return { ok: true, body: bodyFor('stop', { now: !!c.now }), confirm: c.now
        ? 'Stop the run now? Running tasks are interrupted, as Ctrl-C does; the run ends with no verification'
        : 'Stop the run once its running tasks finish? Nothing new starts, and the run ends with no verification (unless every task has merged by then)' };
    }
    if (c.kind === 'retry') {
      if (!c.task) return bad('no task');
      const named = String(c.alias === undefined || c.alias === null ? '' : c.alias).trim();
      const s = named ? specOf(c) : { ok: true, spec: null };
      if (!s.ok) return s;
      return { ok: true, body: bodyFor('retry', { task: c.task, dwarf: s.spec }),
        confirm: `Retry task \`${c.task}\` now${s.spec ? ` on \`${s.spec}\`` : ''}? It is queued into the live run` };
    }
    return bad('unknown change');
  }

  // The sentence after a change went through, from the CLI's answer ({changed, applies}).
  function savedText(data) {
    const d = obj(data), rec = obj(d.changed);
    if (d.changed && String(rec.previous === undefined || rec.previous === null ? '' : rec.previous) === String(rec.value === undefined || rec.value === null ? '' : rec.value)) {
      return 'Nothing changed: it was already so.';
    }
    if (d.queued === true) return 'Queued: the scheduler applies or refuses it at its next turn.';
    return typeof d.applies === 'string' && d.applies ? `Saved. Applies ${d.applies}.` : 'Saved.';
  }

  // The run's controls as they stand (`forge control show`'s `run`): what each shows.
  function runValues(controls) {
    const c = obj(controls), run = obj(c.run);
    return { live: c.live === true, fractal: c.fractal === true,
      qa: LEVELS.includes(run.qa_threshold) ? run.qa_threshold : run.qa_threshold === 'none' ? 'P3' : DEFAULT_LEVEL,
      qaSource: run.qa_threshold_source === 'set' ? 'set' : 'default',
      qaText: `${LEVELS.includes(run.qa_threshold) ? run.qa_threshold : run.qa_threshold === 'none' ? 'P3' : DEFAULT_LEVEL} (${run.qa_threshold_source === 'set' ? 'set' : 'default'})`,
      timeout: num(run.timeout), timeoutMin: minutesOf(run.timeout),
      escalateAfter: num(run.escalate_after), verifyRetries: num(run.verify_retries),
      retryBudget: num(run.retry_budget), infraBudget: num(run.infra_budget),
      maxParallel: num(run.max_parallel), infraBackoff: typeof run.infra_backoff === 'string' ? run.infra_backoff : null,
      escalateEffort: run.escalate_effort === 'on' ? 'on' : 'off', drained: run.drained === true,
      stopping: run.stopping === 'graceful' || run.stopping === 'now' ? run.stopping : null,
      pauses: Array.isArray(run.infra_paused) ? run.infra_paused.filter(x => x && typeof x === 'object') : [] };
  }
  // "3m05s" to a moment, from `now` (epoch seconds); "now" once it has come.
  function countdown(until, now) {
    const s = num(until) === null || num(now) === null ? null : Math.ceil(until - now);
    if (s === null) return null;
    if (s <= 0) return 'now';
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}m`;
  }
  // One line per infrastructure pause (`run.infra_paused`): what is paused, why, its tasks, the wait, and the time to
  // its end, or "budget used up" once its retries are spent.
  function pauseLines(controls, now) {
    return runValues(controls).pauses.map(x => {
      const scope = String(x.scope || '');
      const what = scope === 'all' ? 'every task' : scope.startsWith('model:') ? `model ${x.model || scope.slice(6)}${x.harness ? ' on ' + x.harness : ''}`
        : scope.startsWith('harness:') ? `harness ${x.harness || scope.slice(8)}` : scope || 'a scope';
      const tasks = Array.isArray(x.tasks) && x.tasks.length ? x.tasks.map(String).join(', ') : null;
      const left = x.exhausted === true ? 'budget used up' : countdown(x.until, now);
      return { scope, exhausted: x.exhausted === true, text: [what, x.class ? String(x.class) : null, tasks ? 'tasks ' + tasks : null,
        num(x.cycle) !== null ? `wait ${x.cycle}` : null, left === null ? null : left === 'budget used up' ? left : left === 'now' ? 'resuming' : `resumes in ${left}`]
        .filter(Boolean).join(' · ') };
    });
  }
  // Where a queued retry stands: {state: 'queued'} until the scheduler's line with the same request id is in the log,
  // then {state: 'applied'} or {state: 'refused', reason}. Only the request id matches a line.
  function requestState(request, log) {
    const line = (Array.isArray(log) ? log : []).find(e => e && e.actor === 'scheduler' && e.request === request);
    if (!request || !line) return { state: 'queued' };
    return line.outcome === 'applied' ? { state: 'applied' } : { state: 'refused', reason: typeof line.reason === 'string' ? line.reason : null };
  }
  const requestText = st => st.state === 'refused' ? `refused${st.reason ? ': ' + st.reason : ''}` : st.state;
  // Whether "Retry now" shows for a task: live only, after it ended without passing or once the policy stopped it.
  // Never for a task waiting for an answer: forge refuses a retry without one (retry --answer).
  const canRetry = (controls, id) => {
    const c = obj(controls), v = taskValues(c, id);
    return c.live === true && v.known && v.status !== 'MERGED' && v.status !== 'NEEDS_INPUT' && (RETRYABLE.includes(v.status) || v.stop);
  };
  // A task's controls with the effective value of each: an override wins, else the run's value is inherited.
  function taskValues(controls, id) {
    const c = obj(controls), t = obj(obj(c.tasks)[id]), run = runValues(c);
    const level = t.qa_threshold;
    const threshold = LEVELS.includes(level) ? { mode: level, effective: level, from: 'task' }
      : level === 'none' ? { mode: 'none', effective: 'P3', from: 'task' }
      : { mode: 'inherit', effective: run.qa, from: 'run' };
    const own = num(t.retry_budget), eff = own !== null ? own : run.retryBudget;
    return { id, known: !!c.tasks && id in c.tasks, status: typeof t.status === 'string' ? t.status : 'PENDING',
      dwarf: t.dwarf || null, qa: t.qa || null, threshold,
      retry: { mode: own !== null ? 'set' : 'inherit', own, effective: eff, used: num(t.retries_used) || 0, from: own !== null ? 'task' : eff !== null ? 'run' : null },
      pin: t.pin || null, next: t.escalation_next || null, stop: t.escalation_stop === true, run };
  }
  const effective = (controls, id) => id === undefined ? runValues(controls) : taskValues(controls, id);

  // Whether a control can be changed now: {enabled, why, hidden}. Retry and infra budgets only while a scheduler is
  // live; a merged task has nothing left to change; Fractal plans never apply pins (the control is hidden).
  function canEdit(controls, key, id) {
    const c = obj(controls), info = KEYS[key];
    if (key === 'pin') {
      if (c.fractal === true) return { enabled: false, hidden: true, why: 'Fractal plans never apply pins' };
      if (id !== undefined && taskValues(c, id).status === 'MERGED') return { enabled: false, hidden: false, why: 'merged: nothing left to change' };
      return { enabled: true, hidden: false, why: null };
    }
    if (!info) return { enabled: false, hidden: false, why: 'unknown control' };
    if (id !== undefined && taskValues(c, id).status === 'MERGED') return { enabled: false, hidden: false, why: 'merged: nothing left to change' };
    if (info.live && c.live !== true) return { enabled: false, hidden: false, why: LIVE_ONLY };
    return { enabled: true, hidden: false, why: null };
  }

  // One line of the change log (`control.log`): who, what, previous → new, when.
  function logLine(entry) {
    const e = obj(entry), key = typeof e.key === 'string' ? e.key : '';
    const label = e.action === 'pin' || e.action === 'unpin' || key === 'pin' ? 'Pin' : KEYS[key] ? KEYS[key].label : key || String(e.action || '?');
    const empty = key === 'pin' ? 'no pin' : 'unset';
    const show = v => v === null || v === undefined || v === '' ? empty : key === 'timeout' && minutesOf(Number(v)) !== null ? `${minutesOf(Number(v))} min` : String(v);
    return { who: String(e.actor || '?'), what: e.task ? `${label} · ${e.task}` : label, from: show(e.previous), to: show(e.value), at: num(e.at) };
  }
  function agoText(at, now) {
    const s = num(at) === null ? null : Math.max(0, Math.floor(now - at));
    if (s === null) return '—';
    return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
  }

  // The implementer aliases seen in runs (GET /api/models), for the pin's suggestions.
  function aliasesOf(models) {
    const out = new Set();
    for (const t of Array.isArray(obj(models).tasks) ? models.tasks : []) {
      for (const a of Array.isArray(obj(t).attempts) ? t.attempts : []) if (a && typeof a.alias === 'string' && a.alias) out.add(a.alias);
    }
    return Array.from(out).sort();
  }

  /* ---------- pure: an edit in progress survives a rebuild of the inspector ---------- */

  // Every section is one run (and task): `ctl:<run>:run` or `ctl:<run>:task:<id>`.
  const scopeOf = (runKey, taskId) => `ctl:${runKey}:${taskId ? 'task:' + taskId : 'run'}`;
  // A text field has the focus: typing is under way. A key (button) with the focus is not an edit: nothing is half typed.
  const typing = tag => /^(input|textarea|select)$/i.test(String(tag || ''));
  // A section is being edited while a text field has focus, holds an unsaved value, a confirm is open or a request is out.
  const isEditing = s => !!(s && (s.focus || s.dirty || s.confirm || s.busy));
  // Whether the rebuilt tree takes back the section that was being edited: only for the same run and task. Choosing
  // another run or task builds another scope, so what was half typed goes with it.
  const keepEditing = (held, next) => !!(held && next && held.editing && held.scope && held.scope === next.scope);
  // Controls that arrived while a section was being edited were held back: they are drawn once the edit is over and
  // the keyboard has left the section (a redraw under a pressed key would swallow its click).
  const redrawDue = (drawn, latest, editing, inside) => !!drawn && !!latest && drawn !== latest && !editing && !inside;

  /* ---------- the page: drawing ---------- */

  // requests: `<run key>|<task>` → the id of the retry this page queued, until the scheduler's line answers it
  const store = { enabled: null, runs: new Map(), models: null, modelsAsked: false, lists: new Set(), recent: new Map(), requests: new Map() };
  const RECENT_MS = 9000;                                    // how long a result line stays after the section is rebuilt
  const STALE_MS = 20000;
  let seq = 0;
  const ui = () => root.ForgeUI;
  const doc = () => root.document;
  const path = key => `/api/runs/${encodeURIComponent(key)}/controls`;
  const plateOf = spec => root.ForgeIcons ? root.ForgeIcons.modelPlate(spec) : ui().el('span', spec, 'mono');
  const textNode = t => doc().createTextNode(t);

  function sync(view) {
    const s = view.section;
    const at = doc().activeElement;
    const on = isEditing({ focus: !!at && s.contains(at) && typing(at.tagName), dirty: Object.keys(view.draft).length > 0,
      confirm: !!s.querySelector('.ui-confirm'), busy: view.inflight });
    if (on) s.dataset.editing = '1'; else delete s.dataset.editing;
    const e = store.runs.get(view.run.key);
    if (s.isConnected && redrawDue(view.drawn, e && e.data, on, !!at && s.contains(at))) show(view);
  }
  // A draft value: the field's text while it differs from what the run holds.
  function touch(view, field, value) {
    if (value === view.base[field]) delete view.draft[field]; else view.draft[field] = value;
    sync(view);
  }
  const field = (view, name, base) => {
    view.base[name] = base;
    if (view.draft[name] === base) delete view.draft[name];
    return name in view.draft ? view.draft[name] : base;
  };

  function note(view, text) {
    view.drawn = null;
    view.section.replaceChildren(ui().el('h3', 'Controls'), ui().el('p', text, 'muted ctl-note'));
    sync(view);
  }

  // A row: its label, the control, notes, and slots for the confirm strip and the result line.
  function shell(view, key, label, forInput) {
    const u = ui(), id = 'ctl-' + (++seq), r = { key, id };
    r.box = u.el('div', undefined, 'ctl-row');
    r.box.dataset.row = key;
    r.name = u.el(forInput ? 'label' : 'span', label, 'ctl-label');
    r.name.id = id;
    if (forInput) r.name.htmlFor = id + '-in';
    r.status = u.el('div', undefined, 'ctl-status');
    r.control = u.el('div', undefined, 'ctl-control');
    r.notes = u.el('div', undefined, 'ctl-notes');
    r.confirm = u.el('div', undefined, 'ctl-confirm');
    r.out = u.el('div', undefined, 'ctl-out');
    r.box.append(r.name, r.status, r.control, r.notes, r.confirm, r.out);
    const recent = store.recent.get(view.section.dataset.hold + '|' + key);
    if (recent && Date.now() - recent.at < RECENT_MS) u.outcome(r.out, recent.res, recent.okText, { silent: true });
    return r;
  }
  const help = (r, text, cls) => r.notes.append(ui().el('p', text, 'ctl-help' + (cls ? ' ' + cls : '')));
  const why = (r, text) => { if (text) r.notes.append(ui().el('p', text, 'ctl-why')); };

  // A bank of push keys (design: a push-button bank whose lamp lights on the pressed key).
  function bank(r, options, current, enabled, pick) {
    const seg = ui().el('div', undefined, 'seg ctl-bank');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-labelledby', r.id);
    for (const o of options) {
      const b = ui().el('button', o.label);
      b.type = 'button'; b.dataset.value = o.value; b.disabled = !enabled;
      b.setAttribute('aria-pressed', String(o.value === current));
      if (o.title) b.title = o.title;
      b.addEventListener('click', () => pick(o.value, b));
      seg.append(b);
    }
    return seg;
  }

  // Ask, send, show the answer. A success redraws every row from the controls the answer carries; `done` gets a
  // success's answer (a queued retry keeps its request id).
  async function commit(view, r, change, fields, button, done) {
    const u = ui();
    if (view.inflight) { u.outcome(r.out, { ok: false, status: 409, error: 'busy' }); return; }
    clearOutcome(view, r);
    const plan = prepare(change);
    if (!plan.ok) { remember(view, r, { ok: false, status: 400, error: 'invalid', detail: plan.error }); return; }
    if (button) button.classList.add('is-draft');
    const yes = await u.confirmInline(r.confirm, plan.confirm);
    if (button) button.classList.remove('is-draft');
    if (!yes) { sync(view); return; }
    view.inflight = true; view.section.setAttribute('aria-busy', 'true'); sync(view);
    const res = await u.post(path(view.run.key), plan.body);
    view.inflight = false; view.section.removeAttribute('aria-busy');
    remember(view, r, res, savedText(res.data));
    // sent: the fields are no longer an edit, also when the answer (a queued request) carries no controls
    if (res.ok) for (const f of fields) delete view.draft[f];
    if (res.ok && done) done(res.data);
    if (res.ok && obj(res.data).controls && res.data.controls.run) {
      accept(view.run.key, res.data.controls, view.run.updated);
      draw(view, res.data.controls);
    } else sync(view);
  }
  // Where the keyboard is inside a section, as something that survives the rows being drawn again.
  function focusMark(section) {
    const at = doc().activeElement;
    if (!at || !section.contains(at) || !at.closest) return null;
    const row = at.closest('[data-row]');
    return row ? { row: row.dataset.row, tag: at.tagName, value: at.dataset.value, text: at.tagName === 'BUTTON' ? at.textContent : null } : null;
  }
  // The rows are drawn again (after a change, or a poll): the keyboard goes back to the same key, else to the row's field.
  function refocus(section, mark) {
    const row = mark && Array.from(section.querySelectorAll('[data-row]')).find(n => n.dataset.row === mark.row);
    if (!row) return;
    const live = Array.from(row.querySelectorAll('button:not(:disabled), input:not(:disabled)'));
    const target = live.find(n => n.tagName === mark.tag && n.dataset.value === mark.value && (mark.text === null || n.textContent === mark.text))
      || live.find(n => n.tagName === 'INPUT') || live[0];
    if (target) target.focus({ preventScroll: true });
  }
  // A result line belongs to the edit it answered: a new edit clears it.
  function clearOutcome(view, r) {
    store.recent.delete(view.section.dataset.hold + '|' + r.key);
    Array.from(r.out.children).forEach(c => { if (c.classList.contains('ui-outcome')) c.remove(); });
  }
  function remember(view, r, res, okText) {
    store.recent.set(view.section.dataset.hold + '|' + r.key, { res, okText, at: Date.now() });
    ui().outcome(r.out, res, okText);
  }

  function liveLamp(view, c) {
    const u = ui(), live = c.live === true, box = u.el('p', undefined, 'ctl-live'), v = runValues(c);
    box.append(u.el('i', undefined, 'lamp ' + (live ? 'st-running' : 'st-pending')),
      u.el('span', live ? 'live' : 'not running', 'plate'));
    if (live && v.stopping) box.append(u.el('span', v.stopping === 'now' ? 'stopping now' : 'stopping', 'tag ctl-state ctl-stopping'));
    else if (live && v.drained) box.append(u.el('span', 'drained', 'tag ctl-state ctl-drained'));
    box.append(u.el('span', !live ? 'no scheduler is running this plan' : v.stopping ? 'the run ends once its running tasks are done'
      : v.drained ? 'running tasks finish; nothing new starts until resumed' : 'a scheduler is running this plan', 'muted'));
    return box;
  }

  // Drain or Resume, Stop and Stop now: live only, each asked first.
  function runKeys(view, c) {
    const u = ui(), v = runValues(c), r = shell(view, 'run', 'Run', false);
    const keys = u.el('div', undefined, 'ctl-keys');
    const key = (label, change, title, cls) => {
      const b = u.el('button', label, cls); b.type = 'button'; b.title = title;
      b.addEventListener('click', () => commit(view, r, change, [], b));
      keys.append(b);
    };
    if (v.drained) key('Resume', { kind: 'resume' }, 'Start tasks again');
    else key('Drain', { kind: 'drain' }, 'Start nothing new; running tasks finish and the run waits');
    if (!v.stopping) {
      key('Stop', { kind: 'stop' }, 'Let running tasks finish, then end the run (no verification, unless every task merged)');
      key('Stop now', { kind: 'stop', now: true }, 'Interrupt running tasks and end the run', 'ui-danger');
    }
    r.control.append(keys);
    help(r, v.stopping ? 'Stopping: nothing more to ask of the run.' : 'Stop lets running tasks finish; Stop now interrupts them.');
    return r.box;
  }

  // One line per infrastructure pause; its countdown ticks from the page's clock between polls.
  function pauseBox(view, c) {
    const u = ui(), box = u.el('div', undefined, 'ctl-pauses');
    box.append(u.el('span', 'Paused on infrastructure', 'ctl-label'));
    const ul = u.el('ul', undefined, 'ctl-pause-list');
    const paint = () => {
      const lines = pauseLines(c, Date.now() / 1000);
      ul.replaceChildren(...lines.map(l => {
        const li = u.el('li', undefined, 'ctl-pause' + (l.exhausted ? ' ctl-exhausted' : ''));
        li.append(u.el('i', undefined, 'lamp ' + (l.exhausted ? 'st-fail' : 'st-running')), u.el('span', l.text, 'ctl-pause-text'));
        return li;
      }));
    };
    paint();
    if (view.ticker) clearInterval(view.ticker);
    view.ticker = setInterval(() => { if (box.isConnected) paint(); else { clearInterval(view.ticker); view.ticker = null; } }, 1000);
    box.append(ul);
    return box;
  }

  // An on/off setting as a two-key bank.
  function switchRow(view, c, o) {
    const r = shell(view, o.key, o.label, false), perm = o.perm;
    r.control.append(bank(r, [{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }], o.current, perm.enabled, (value, button) => {
      if (value !== o.current) commit(view, r, { kind: 'set', key: o.key, input: value }, [], button);
    }));
    help(r, o.help);
    if (!perm.enabled) why(r, perm.why);
    return r.box;
  }

  // An integer or minutes setting: a field and a Set key; Enter sets.
  function countRow(view, c, o) {
    const u = ui(), r = shell(view, o.key, o.label, true), perm = o.perm;
    const form = u.el('form', undefined, 'ctl-set');
    form.noValidate = true;
    const input = u.el('input');
    input.type = 'text'; input.inputMode = o.text ? 'text' : o.decimal ? 'decimal' : 'numeric'; input.autocomplete = 'off'; input.spellcheck = false;
    input.id = r.id + '-in'; input.placeholder = o.placeholder || ''; input.disabled = !perm.enabled;
    const name = o.key + (o.task ? '@' + o.task : '');
    input.value = field(view, name, o.value === null ? '' : String(o.value));
    const set = u.el('button', 'Set'); set.type = 'submit';
    const settle = () => { set.disabled = !perm.enabled || input.value === view.base[name]; };
    settle();
    input.addEventListener('input', () => { touch(view, name, input.value); settle(); clearOutcome(view, r); });
    form.append(input);
    if (o.unit) form.append(u.el('span', o.unit, 'ctl-unit'));
    if (o.inherit) {
      const inh = u.el('button', 'Inherit'); inh.type = 'button'; inh.disabled = !perm.enabled || o.inherit === 'pressed';
      inh.setAttribute('aria-pressed', String(o.inherit === 'pressed')); inh.title = "Drop this task's own value and use the run's";
      inh.addEventListener('click', () => commit(view, r, { kind: 'set', key: o.key, input: 'inherit', task: o.task }, [name], inh));
      form.append(set, inh);
    } else form.append(set);
    form.addEventListener('submit', e => {
      e.preventDefault();
      if (!set.disabled) commit(view, r, { kind: 'set', key: o.key, input: input.value, task: o.task }, [name], set);
    });
    r.control.append(form);
    if (o.help) help(r, o.help);
    if (!perm.enabled) why(r, perm.why);
    return r.box;
  }

  function levelRow(view, c, o) {
    const r = shell(view, o.key, o.label, false), perm = o.perm;
    const options = (o.inherit ? [{ value: 'inherit', label: 'Inherit', title: "Use the run's threshold" }]
      : [{ value: 'default', label: 'Default', title: 'Remove the setting: the default (P2) applies' }])
      .concat(LEVELS.map(l => ({ value: l, label: l })), [{ value: 'none', label: 'None', title: o.noneTitle }]);
    r.control.append(bank(r, options, o.current, perm.enabled, (value, button) => {
      if (value !== o.current) commit(view, r, { kind: 'set', key: 'qa-threshold', input: value, task: o.task }, [], button);
    }));
    help(r, o.help);
    if (!perm.enabled) why(r, perm.why);
    return r.box;
  }

  function changeLog(c) {
    const u = ui(), log = Array.isArray(c.log) ? c.log : [];
    const box = u.el('div', undefined, 'ctl-log');
    box.append(u.el('span', 'Last changes', 'ctl-label'));
    if (!log.length) { box.append(u.el('p', 'none yet', 'muted ctl-help')); return box; }
    const ul = u.el('ul', undefined, 'ctl-changes'), now = Date.now() / 1000;
    for (const entry of log.slice(0, 5)) {
      const l = logLine(entry), li = u.el('li');
      const when = u.el('span', agoText(l.at, now), 'muted');
      if (l.at !== null) when.title = new Date(l.at * 1000).toLocaleString();
      li.append(u.el('span', l.who, 'tag'), u.el('span', l.what, 'ctl-what'), u.el('span', `${l.from} → ${l.to}`, 'mono'), when);
      ul.append(li);
    }
    box.append(ul);
    return box;
  }

  function drawRun(view, c) {
    const v = runValues(c), body = ui().el('div', undefined, 'ctl-body');
    const perm = key => canEdit(c, key);
    body.append(liveLamp(view, c));
    if (c.live === true) {
      body.append(runKeys(view, c));
      if (v.pauses.length) body.append(pauseBox(view, c));
    }
    body.append(levelRow(view, c, { key: 'qa-threshold', label: 'QA threshold', current: v.qaSource === 'set' ? v.qa : 'default', perm: perm('qa-threshold'),
      help: `The least severe finding that still blocks: ${v.qaText} (${LEVELS.slice(0, LEVELS.indexOf(v.qa) + 1).join(', ')} block).`,
      noneTitle: 'P3: nothing is tolerated' }));
    body.append(countRow(view, c, { key: 'timeout', label: 'Timeout', unit: 'minutes', decimal: true, value: v.timeoutMin, placeholder: 'default', perm: perm('timeout'),
      help: v.timeoutMin === null ? "Not set: each model's own limit applies. 0 turns the limit off." : `Each model call stops after ${v.timeoutMin} min. 0 turns the limit off.` }));
    body.append(countRow(view, c, { key: 'escalate-after', label: 'Escalate after', unit: 'failures', value: v.escalateAfter, perm: perm('escalate-after'),
      help: 'Failed attempts before the policy moves a task to a stronger model. 0 turns escalation off.' }));
    body.append(countRow(view, c, { key: 'verify-retries', label: 'Verify retries', unit: 'retries', value: v.verifyRetries, perm: perm('verify-retries'),
      help: 'Times verification is retried after it fails.' }));
    body.append(countRow(view, c, { key: 'retry-budget', label: 'Retry budget', unit: 'retries', value: v.retryBudget, placeholder: 'not set', perm: perm('retry-budget'),
      help: 'Retries a task may use, run-wide.' }));
    body.append(countRow(view, c, { key: 'infra-retries', label: 'Infra retries', unit: 'retries', value: v.infraBudget, placeholder: 'not set', perm: perm('infra-retries'),
      help: 'Retries for infrastructure failures (auth, quota, network), run-wide.' }));
    body.append(countRow(view, c, { key: 'max-parallel', label: 'Max parallel', unit: 'tasks', value: v.maxParallel, placeholder: 'as started', perm: perm('max-parallel'),
      help: 'Tasks running at once, 1 to 64. A lower limit lets running tasks finish; it never stops one.' }));
    body.append(countRow(view, c, { key: 'infra-backoff', label: 'Infra backoff', text: true, value: v.infraBackoff, placeholder: 'exp:60', perm: perm('infra-backoff'),
      help: `The wait between infrastructure retries: ${BACKOFF_FORMS}. "default" goes back to exp:60.` }));
    body.append(switchRow(view, c, { key: 'escalate-effort', label: 'Effort step', current: v.escalateEffort, perm: perm('escalate-effort'),
      help: v.escalateEffort === 'on' ? 'On: at the top tier, a task is retried once more at the next effort its model accepts before it stops.'
        : 'Off: at the top tier, a task that used up its escalation stops with a split suggestion.' }));
    body.append(changeLog(c));
    return body;
  }

  function drawTask(view, c, id) {
    const u = ui(), v = taskValues(c, id), body = u.el('div', undefined, 'ctl-body');
    body.append(liveLamp(view, c));
    if (!v.known) { body.append(u.el('p', 'This task is not in the plan\'s controls yet.', 'muted ctl-note')); return body; }
    if (v.status === 'MERGED') { body.append(u.el('p', 'Merged: there is nothing left to change for this task.', 'muted ctl-note')); return body; }
    const th = v.threshold, task = id;
    body.append(levelRow(view, c, { key: 'qa-threshold', label: 'Threshold override', inherit: true, task, current: th.mode, perm: canEdit(c, 'qa-threshold', id),
      noneTitle: 'P3 for this task: nothing is tolerated',
      help: th.mode === 'inherit' ? `Inherits ${v.run.qa} from the run.`
        : th.mode === 'none' ? 'P3 for this task, whatever the run sets.' : `${th.mode} for this task, whatever the run sets.` }));
    const rb = v.retry, used = rb.effective === null ? `used ${rb.used}` : `used ${rb.used} of ${rb.effective}`;
    body.append(countRow(view, c, { key: 'retry-budget', label: 'Retry budget', unit: 'retries', task, value: rb.own, placeholder: rb.effective === null ? 'inherit' : String(rb.effective),
      inherit: rb.mode === 'inherit' ? 'pressed' : 'ready', perm: canEdit(c, 'retry-budget', id),
      help: `${rb.mode === 'inherit' ? (rb.effective === null ? 'Inherits the run\'s budget.' : `Inherits ${rb.effective} from the run.`) : `${rb.own} for this task.`} ${used}.` }));
    if (!canEdit(c, 'pin', id).hidden) body.append(pinRow(view, c, v));
    if (canRetry(c, id) || store.requests.has(view.run.key + '|' + id)) body.append(retryRow(view, c, v));
    return body;
  }

  // The implementer picker the pin and Retry now share: alias (suggestions from the models seen in runs, free text too),
  // effort, optional harness. Drafts live under `<prefix>.alias|effort|harness`; read() gives the change's parts.
  function modelFields(view, r, prefix) {
    const u = ui(), alias = u.el('input'), harness = u.el('input'), list = u.el('datalist');
    alias.type = 'text'; harness.type = 'text';
    for (const i of [alias, harness]) { i.autocomplete = 'off'; i.spellcheck = false; }
    alias.setAttribute('list', r.id + '-models');
    list.id = r.id + '-models'; fillList(list);
    if (!store.models) trackList(list);
    alias.value = field(view, prefix + '.alias', ''); harness.value = field(view, prefix + '.harness', '');
    alias.addEventListener('input', () => touch(view, prefix + '.alias', alias.value));
    harness.addEventListener('input', () => touch(view, prefix + '.harness', harness.value));
    const captioned = (caption, control, cls) => { const l = u.el('label', undefined, 'filter ' + cls); l.append(u.el('span', caption), control); return l; };
    const effortCaption = u.el('span', 'Effort'), effortBox = u.el('div', undefined, 'filter ctl-effort');
    effortCaption.id = r.id + '-effort';
    const effortBank = bank(r, EFFORTS.map(e => ({ value: e, label: e })), field(view, prefix + '.effort', 'default'), true, value => {
      touch(view, prefix + '.effort', value);
      for (const b of effortBank.children) b.setAttribute('aria-pressed', String(b.dataset.value === value));
    });
    effortBank.setAttribute('aria-labelledby', effortCaption.id);
    effortBox.append(effortCaption, effortBank);
    loadModels();
    return { nodes: [captioned('Model alias', alias, 'ctl-alias'), list, captioned('Harness (optional)', harness, 'ctl-harness'), effortBox],
      read: () => ({ alias: alias.value, effort: view.draft[prefix + '.effort'] || 'default', harness: harness.value }),
      fields: [prefix + '.alias', prefix + '.effort', prefix + '.harness'] };
  }

  // Retry now: queue a retry of a failed or stopped task into the live run, on its policy's model or one named here.
  function retryRow(view, c, v) {
    const u = ui(), r = shell(view, 'retry', 'Retry now', false), id = v.id, hold = view.run.key + '|' + id;
    const request = store.requests.get(hold);
    if (request) {
      const st = requestState(request, c.log);
      const line = u.el('p', undefined, 'ctl-request ctl-request-' + st.state);
      line.append(u.el('i', undefined, 'lamp ' + (st.state === 'applied' ? 'st-pass' : st.state === 'refused' ? 'st-fail' : 'st-running')),
        u.el('span', requestText(st)));
      r.status.append(line);
    }
    if (!canRetry(c, id)) return r.box;
    const form = u.el('form', undefined, 'ctl-pin ctl-retry');
    form.noValidate = true;
    const picker = modelFields(view, r, 'retry');
    const go = u.el('button', 'Retry now', 'primary'); go.type = 'submit';
    form.append(...picker.nodes, go);
    form.addEventListener('submit', e => {
      e.preventDefault();
      commit(view, r, Object.assign({ kind: 'retry', task: id }, picker.read()), picker.fields, go, data => {
        if (data && data.queued === true && typeof data.request === 'string') store.requests.set(hold, data.request);
        show(view);
      });
    });
    r.control.append(form);
    help(r, 'Leave the model empty for the escalation policy\'s choice.');
    return r.box;
  }

  function pinRow(view, c, v) {
    const u = ui(), r = shell(view, 'pin', 'Pin next retry to', false), id = v.id;
    const now = u.el('div', undefined, 'ctl-pin-now');
    if (v.pin) {
      const un = u.el('button', 'Unpin'); un.type = 'button';
      un.title = 'Drop the pin: the next retry goes back to the escalation policy';
      un.addEventListener('click', () => commit(view, r, { kind: 'unpin', task: id }, ['pin.alias', 'pin.effort', 'pin.harness'], un));
      now.append(u.el('span', 'pinned', 'tag'), plateOf(v.pin), un);
    } else now.append(u.el('span', 'no pin', 'muted'));
    r.status.append(now);
    if (v.next) {
      const next = u.el('p', undefined, 'ctl-help');
      next.append(textNode('the policy picked '), plateOf(v.next), textNode(' for the next retry'));
      r.status.append(next);
    }
    if (v.stop) {
      const stop = u.el('p', undefined, 'ctl-stop');
      // "Retry now" is only offered while the run is live
      stop.append(u.el('i', undefined, 'lamp st-fail'), u.el('span', canRetry(c, id) ? 'policy stopped: split, pin a model, or Retry now' : 'policy stopped: split or pin a model'));
      r.status.append(stop);
    }
    const form = u.el('form', undefined, 'ctl-pin');
    form.noValidate = true;
    const picker = modelFields(view, r, 'pin');
    const go = u.el('button', 'Pin', 'primary'); go.type = 'submit';
    form.append(...picker.nodes, go);
    form.addEventListener('submit', e => {
      e.preventDefault();
      commit(view, r, Object.assign({ kind: 'pin', task: id }, picker.read()), picker.fields, go);
    });
    r.control.append(form);
    help(r, 'Applies at the next full retry, not a QA-only resume.');
    return r.box;
  }
  function fillList(list) {
    list.replaceChildren();
    for (const a of store.models || []) { const o = ui().el('option'); o.value = a; list.append(o); }
  }
  // Only lists drawn while the aliases are still on their way wait for them (and only while attached: a
  // detached one would keep its whole old section alive); once loaded, fillList reads store.models itself.
  function trackList(list) {
    for (const l of Array.from(store.lists)) if (!l.isConnected) store.lists.delete(l);
    store.lists.add(list);
  }
  function loadModels() {
    if (store.modelsAsked) return;
    store.modelsAsked = true;
    ui().get('/api/models').then(res => {
      if (!res.ok) { store.modelsAsked = false; return; }
      store.models = aliasesOf(res.data);
      for (const l of store.lists) if (l.isConnected) fillList(l);
      store.lists.clear();
    });
  }

  // Draw a section from `forge control show`.
  function draw(view, c) {
    const s = view.section, mark = focusMark(s);
    view.base = {}; view.drawn = c;
    s.replaceChildren(ui().el('h3', 'Controls'), view.task ? drawTask(view, c, view.task) : drawRun(view, c));
    refocus(s, mark);
    for (const f of Object.keys(view.draft)) if (!(f in view.base)) delete view.draft[f];   // its row is gone: so is the draft
    sync(view);
  }

  // What the section shows from what is known now: no fetch.
  function show(view) {
    if (!store.enabled) { note(view, 'Read-only: this page cannot change the run.'); return; }
    const e = store.runs.get(view.run.key);
    if (e && e.data) draw(view, e.data);
    else note(view, e && e.error ? `Controls are unavailable: ${e.error}` : 'Loading controls…');
  }
  // New data for a run reaches every section of it that is not being edited.
  function arrive(e) {
    for (const view of Array.from(e.views)) {
      if (!view.section.isConnected) e.views.delete(view);
      else if (!view.section.dataset.editing) show(view);
    }
  }
  function accept(key, controls, fresh) {
    const e = store.runs.get(key);
    if (!e) return;
    e.data = controls; e.text = JSON.stringify(controls); e.error = null; e.at = Date.now(); e.fresh = fresh;
    arrive(e);
  }
  function fetchControls(view) {
    const key = view.run.key, run = view.run;
    let e = store.runs.get(key);
    if (!e) store.runs.set(key, e = { views: new Set(), data: null, text: null, error: null, at: 0, fresh: undefined, pending: false });
    if (e.views.size > 20) for (const v of Array.from(e.views)) if (!v.section.isConnected) e.views.delete(v);
    e.views.add(view);
    if (e.pending || (e.fresh === run.updated && Date.now() - e.at < STALE_MS)) return;
    e.pending = true; e.fresh = run.updated;
    ui().get(path(key)).then(res => {
      e.pending = false; e.at = Date.now();
      if (res.ok) {
        const text = JSON.stringify(res.data);
        if (text === e.text && !e.error) return;               // nothing moved: no redraw
        e.data = res.data; e.text = text; e.error = null;
      } else {
        e.error = res.status === 404 ? 'this server has no controls for the run. If forge-ui was started before this version, run `forge ui open` again to restart it.' : res.detail || res.error;
        if (e.data) return;                                    // keep the last good data on screen
      }
      arrive(e);
    });
  }

  // Mount the Controls section of a run (task undefined) or of one task into `host`.
  function mount(host, ctx, task) {
    const u = ui(), run = ctx.run;
    const section = u.el('section', undefined, 'insp-section ctl');
    section.dataset.hold = scopeOf(run.key, task && task.id);
    section.append(u.el('h3', 'Controls'));
    host.append(section);
    const view = { section, run, task: task ? task.id : null, draft: {}, base: {}, inflight: false, drawn: null };
    section.addEventListener('focusin', () => sync(view));
    section.addEventListener('focusout', () => setTimeout(() => sync(view), 0));
    if (store.enabled === null) {
      note(view, 'Checking whether this page can change the run…');
      u.controlsEnabled().then(on => {
        store.enabled = on || !u.token() ? on : null;           // a token but no answer: ask again at the next mount
        if (on) fetchControls(view);
        if (section.isConnected && !section.dataset.editing) show(view);
      });
      return section;
    }
    if (store.enabled) fetchControls(view);
    show(view);
    return section;
  }
  const mountRun = (host, ctx) => mount(host, ctx);
  const mountTask = (host, ctx, task) => mount(host, ctx, task);

  // Before the inspector is rebuilt: the sections of `parent` being edited, to be put back after. Their focus and
  // caret come back with them (a node taken out of the page loses its focus). A key that only has the keyboard is not an
  // edit: its section is drawn again from the new data, and the keyboard is put back on the same key.
  function hold(parent) {
    const held = Array.from(parent.querySelectorAll('[data-hold][data-editing]'));
    const active = doc().activeElement;
    const sec = active && active.closest ? active.closest('[data-hold]') : null;
    const key = sec && parent.contains(sec) && !held.includes(sec) ? { scope: sec.dataset.hold, mark: focusMark(sec) } : null;
    if (!held.length && !(key && key.mark)) return null;
    const focus = held.some(n => n.contains(active)) ? { node: active, from: active.selectionStart, to: active.selectionEnd, dir: active.selectionDirection } : null;
    const fresh = scope => Array.from(parent.querySelectorAll('[data-hold]')).find(n => keepEditing({ scope, editing: true }, { scope: n.dataset.hold }));
    return { restore() {
      for (const node of held) {
        const next = fresh(node.dataset.hold);
        if (next) next.replaceWith(node);
      }
      if (focus && focus.node.isConnected) {
        focus.node.focus({ preventScroll: true });
        try { if (focus.from !== null && focus.from !== undefined) focus.node.setSelectionRange(focus.from, focus.to, focus.dir || 'none'); } catch (_) { /* not a text field */ }
      }
      const next = key && key.mark ? fresh(key.scope) : null;
      if (next) refocus(next, key.mark);
    } };
  }

  return { LEVELS, EFFORTS, KEYS, specOf, countOf, secondsOf, minutesOf, valueFor, bodyFor, prepare, savedText, effective, canEdit, logLine, agoText,
    aliasesOf, scopeOf, typing, isEditing, keepEditing, redrawDue, countdown, pauseLines, requestState, requestText, canRetry,
    mountRun, mountTask, hold };
});
