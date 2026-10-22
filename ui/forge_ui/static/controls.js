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
  };

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
      return LEVELS.includes(text) || text === 'none' || (task && text === 'inherit') ? { ok: true, value: text } : bad('P0, P1, P2, P3 or none');
    }
    if (task && key === 'retry-budget' && text === 'inherit') return { ok: true, value: 'inherit' };
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
    throw new Error('unknown action ' + action);
  }

  const shown = (key, value, task) => key === 'timeout' ? `${minutesOf(Number(value))} min`
    : key === 'qa-threshold' && value === 'none' && task ? 'none (no threshold for this task)' : value;
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
    return bad('unknown change');
  }

  // The sentence after a change went through, from the CLI's answer ({changed, applies}).
  function savedText(data) {
    const d = obj(data), rec = obj(d.changed);
    if (d.changed && String(rec.previous === undefined || rec.previous === null ? '' : rec.previous) === String(rec.value === undefined || rec.value === null ? '' : rec.value)) {
      return 'Nothing changed: it was already so.';
    }
    return typeof d.applies === 'string' && d.applies ? `Saved. Applies ${d.applies}.` : 'Saved.';
  }

  // The run's controls as they stand (`forge control show`'s `run`): what each shows.
  function runValues(controls) {
    const c = obj(controls), run = obj(c.run);
    return { live: c.live === true, fractal: c.fractal === true,
      qa: LEVELS.includes(run.qa_threshold) ? run.qa_threshold : 'none',
      timeout: num(run.timeout), timeoutMin: minutesOf(run.timeout),
      escalateAfter: num(run.escalate_after), verifyRetries: num(run.verify_retries),
      retryBudget: num(run.retry_budget), infraBudget: num(run.infra_budget) };
  }
  // A task's controls with the effective value of each: an override wins, else the run's value is inherited.
  function taskValues(controls, id) {
    const c = obj(controls), t = obj(obj(c.tasks)[id]), run = runValues(c);
    const level = t.qa_threshold;
    const threshold = LEVELS.includes(level) ? { mode: level, effective: level, from: 'task' }
      : level === 'none' ? { mode: 'none', effective: 'none', from: 'task' }
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
  // A section is being edited while a field has focus, holds an unsaved value, a confirm is open or a request is out.
  const isEditing = s => !!(s && (s.focus || s.dirty || s.confirm || s.busy));
  // Whether the rebuilt tree takes back the section that was being edited: only for the same run and task. Choosing
  // another run or task builds another scope, so what was half typed goes with it.
  const keepEditing = (held, next) => !!(held && next && held.editing && held.scope && held.scope === next.scope);

  /* ---------- the page: drawing ---------- */

  const store = { enabled: null, runs: new Map(), models: null, modelsAsked: false, lists: new Set(), recent: new Map() };
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
    const on = isEditing({ focus: s.contains(doc().activeElement), dirty: Object.keys(view.draft).length > 0,
      confirm: !!s.querySelector('.ui-confirm'), busy: view.inflight });
    if (on) s.dataset.editing = '1'; else delete s.dataset.editing;
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

  // Ask, send, show the answer. A success redraws every row from the controls the answer carries.
  async function commit(view, r, change, fields, button) {
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
    if (res.ok && obj(res.data).controls && res.data.controls.run) {
      for (const f of fields) delete view.draft[f];
      const keyboard = view.section.contains(doc().activeElement);
      accept(view.run.key, res.data.controls, view.run.updated);
      draw(view, res.data.controls);
      if (keyboard) refocus(view, r.key, button);
    } else sync(view);
  }
  // The rows are drawn again after a change; the keyboard goes back to the same key, else to the row's field.
  function refocus(view, rowKey, button) {
    const row = Array.from(view.section.querySelectorAll('[data-row]')).find(n => n.dataset.row === rowKey);
    if (!row) return;
    const value = button && button.dataset.value;
    const target = (value && Array.from(row.querySelectorAll('button')).find(b => b.dataset.value === value && !b.disabled))
      || row.querySelector('input:not(:disabled)') || row.querySelector('button:not(:disabled)');
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
    const u = ui(), live = c.live === true, box = u.el('p', undefined, 'ctl-live');
    box.append(u.el('i', undefined, 'lamp ' + (live ? 'st-running' : 'st-pending')),
      u.el('span', live ? 'live' : 'not running', 'plate'), u.el('span', live ? 'a scheduler is running this plan' : 'no scheduler is running this plan', 'muted'));
    return box;
  }

  // An integer or minutes setting: a field and a Set key; Enter sets.
  function countRow(view, c, o) {
    const u = ui(), r = shell(view, o.key, o.label, true), perm = o.perm;
    const form = u.el('form', undefined, 'ctl-set');
    form.noValidate = true;
    const input = u.el('input');
    input.type = 'text'; input.inputMode = o.decimal ? 'decimal' : 'numeric'; input.autocomplete = 'off'; input.spellcheck = false;
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
    const options = (o.inherit ? [{ value: 'inherit', label: 'Inherit', title: "Use the run's threshold" }] : [])
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
    body.append(levelRow(view, c, { key: 'qa-threshold', label: 'QA threshold', current: v.qa, perm: perm('qa-threshold'),
      help: v.qa === 'none' ? 'The least severe finding that still blocks: no threshold is set.'
        : `The least severe finding that still blocks: ${v.qa} (${LEVELS.slice(0, LEVELS.indexOf(v.qa) + 1).join(', ')} block).`, noneTitle: 'No threshold' }));
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
      noneTitle: 'No threshold for this task',
      help: th.mode === 'inherit' ? `Inherits ${v.run.qa === 'none' ? 'no threshold' : v.run.qa} from the run.`
        : th.mode === 'none' ? 'No threshold for this task, whatever the run sets.' : `${th.mode} for this task, whatever the run sets.` }));
    const rb = v.retry, used = rb.effective === null ? `used ${rb.used}` : `used ${rb.used} of ${rb.effective}`;
    body.append(countRow(view, c, { key: 'retry-budget', label: 'Retry budget', unit: 'retries', task, value: rb.own, placeholder: rb.effective === null ? 'inherit' : String(rb.effective),
      inherit: rb.mode === 'inherit' ? 'pressed' : 'ready', perm: canEdit(c, 'retry-budget', id),
      help: `${rb.mode === 'inherit' ? (rb.effective === null ? 'Inherits the run\'s budget.' : `Inherits ${rb.effective} from the run.`) : `${rb.own} for this task.`} ${used}.` }));
    if (!canEdit(c, 'pin', id).hidden) body.append(pinRow(view, c, v));
    return body;
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
      stop.append(u.el('i', undefined, 'lamp st-fail'), u.el('span', 'policy stopped: split or pin a model'));
      r.status.append(stop);
    }
    // alias (suggestions from the models seen in runs, free text too), effort, optional harness
    const form = u.el('form', undefined, 'ctl-pin');
    form.noValidate = true;
    const alias = u.el('input'), harness = u.el('input'), list = u.el('datalist');
    alias.type = 'text'; harness.type = 'text';
    for (const i of [alias, harness]) { i.autocomplete = 'off'; i.spellcheck = false; }
    alias.setAttribute('list', r.id + '-models');
    list.id = r.id + '-models'; store.lists.add(list); fillList(list);
    alias.value = field(view, 'pin.alias', ''); harness.value = field(view, 'pin.harness', '');
    alias.addEventListener('input', () => touch(view, 'pin.alias', alias.value));
    harness.addEventListener('input', () => touch(view, 'pin.harness', harness.value));
    const captioned = (caption, control, cls) => { const l = u.el('label', undefined, 'filter ' + cls); l.append(u.el('span', caption), control); return l; };
    const effortCaption = u.el('span', 'Effort'), effortBox = u.el('div', undefined, 'filter ctl-effort');
    effortCaption.id = r.id + '-effort';
    const effortBank = bank(r, EFFORTS.map(e => ({ value: e, label: e })), field(view, 'pin.effort', 'default'), true, value => {
      touch(view, 'pin.effort', value);
      for (const b of effortBank.children) b.setAttribute('aria-pressed', String(b.dataset.value === value));
    });
    effortBank.setAttribute('aria-labelledby', effortCaption.id);
    effortBox.append(effortCaption, effortBank);
    const go = u.el('button', 'Pin', 'primary'); go.type = 'submit';
    form.append(captioned('Model alias', alias, 'ctl-alias'), list, captioned('Harness (optional)', harness, 'ctl-harness'), effortBox, go);
    form.addEventListener('submit', e => {
      e.preventDefault();
      commit(view, r, { kind: 'pin', task: id, alias: alias.value, effort: view.draft['pin.effort'] || 'default', harness: harness.value }, ['pin.alias', 'pin.effort', 'pin.harness'], go);
    });
    r.control.append(form);
    help(r, 'Applies at the next full retry, not a QA-only resume.');
    loadModels();
    return r.box;
  }
  function fillList(list) {
    list.replaceChildren();
    for (const a of store.models || []) { const o = ui().el('option'); o.value = a; list.append(o); }
  }
  function loadModels() {
    if (store.modelsAsked) return;
    store.modelsAsked = true;
    ui().get('/api/models').then(res => {
      if (!res.ok) { store.modelsAsked = false; return; }
      store.models = aliasesOf(res.data);
      for (const l of Array.from(store.lists)) { if (l.isConnected) fillList(l); else store.lists.delete(l); }
    });
  }

  // Draw a section from `forge control show`.
  function draw(view, c) {
    const s = view.section;
    view.base = {};
    s.replaceChildren(ui().el('h3', 'Controls'), view.task ? drawTask(view, c, view.task) : drawRun(view, c));
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
    const view = { section, run, task: task ? task.id : null, draft: {}, base: {}, inflight: false };
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
  // caret come back with them (a node taken out of the page loses its focus).
  function hold(parent) {
    const held = Array.from(parent.querySelectorAll('[data-hold][data-editing]'));
    if (!held.length) return null;
    const active = doc().activeElement;
    const focus = held.some(n => n.contains(active)) ? { node: active, from: active.selectionStart, to: active.selectionEnd, dir: active.selectionDirection } : null;
    return { restore() {
      for (const node of held) {
        const fresh = Array.from(parent.querySelectorAll('[data-hold]')).find(n => keepEditing({ scope: node.dataset.hold, editing: true }, { scope: n.dataset.hold }));
        if (fresh) fresh.replaceWith(node);
      }
      if (focus && focus.node.isConnected) {
        focus.node.focus({ preventScroll: true });
        try { if (focus.from !== null && focus.from !== undefined) focus.node.setSelectionRange(focus.from, focus.to, focus.dir || 'none'); } catch (_) { /* not a text field */ }
      }
    } };
  }

  return { LEVELS, EFFORTS, KEYS, specOf, countOf, secondsOf, minutesOf, valueFor, bodyFor, prepare, savedText, effective, canEdit, logLine, agoText,
    aliasesOf, scopeOf, isEditing, keepEditing, mountRun, mountTask, hold };
});
