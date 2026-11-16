/* forge-ui triage panel (#161): on a project's Findings tab a person starts a read-only triage agent on a model they
   name, watches it, and approves or rejects each proposal it wrote. window.ForgeTriage, and a CommonJS module under
   node:test. The pure helpers (proposalSummary, cliFor, isActive, pollDelay, confirmFor, elapsed) and the panel are
   tested in ui/tests/js/triageview.test.js. Everything the agent wrote (reasons, titles, bodies, plans) is untrusted:
   it only ever enters the page as text.
   An approval sends exactly the hash of the card it was made on, so a proposal that changed since it was read is
   refused by the server. Read-only (no write token, or controls off) the panel shows the `forge findings` command
   of each action to copy, and the state and the proposals as they are.
   render(host, ctx) returns {stop, refresh}. ctx = { id, root, writes, reload(), store?, openCount?(), rows?(), show?(fp) }:
   `store` is a plain object the caller keeps between redraws (open, spec, max, the last answer, the plan dirs made);
   `rows()` are the Findings tab's rows (to name a finding by its summary), `show(fp)` shows one finding in the table.
   Polling: one timer at most, only while the triage runs, gone on stop(), and when the host leaves the page. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeTriage = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const POLL_MS = 5000, CHECK_MS = 400, MAX_LIMIT = 2000, MAX_REASON = 500;
  const KINDS = ['duplicate', 'fixed', 'tag', 'issue', 'plan'];
  const KIND_TITLE = { duplicate: 'Duplicates', fixed: 'Already fixed', tag: 'Tags', issue: 'Issues to file', plan: 'Plans' };
  const str = v => typeof v === 'string' ? v : v == null ? '' : String(v);
  const noun = n => `${n} finding${n === 1 ? '' : 's'}`;

  /* ---------- pure ---------- */
  function shellQuote(value) {
    const s = str(value);
    return /^[A-Za-z0-9_@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;
  }
  // The fingerprints a proposal names, in order, each once.
  function fpsOf(p) {
    if (!p || typeof p !== 'object') return [];
    const raw = p.kind === 'duplicate' || p.kind === 'fixed' ? [p.fp] : list(p.fps);
    return Array.from(new Set(raw.filter(fp => typeof fp === 'string' && fp)));
  }
  // One line per proposal.
  function proposalSummary(p) {
    if (!p || typeof p !== 'object') return '—';
    const n = fpsOf(p).length;
    if (p.kind === 'duplicate') return `duplicate: ${str(p.fp)} repeats ${str(p.of)}`;
    if (p.kind === 'fixed') return `fixed: ${str(p.fp)}` + (p.commit ? ` by ${str(p.commit).slice(0, 12)}` : '');
    if (p.kind === 'tag') return `tag ${noun(n)}: ${list(p.tags).join(', ')}`;
    if (p.kind === 'issue') return `issue for ${noun(n)}: ${str(p.title)}`;
    if (p.kind === 'plan') return `plan for ${noun(n)}: ${str(p.title)}` + (list(p.plan && p.plan.tasks).length ? ` (${list(p.plan.tasks).length} tasks)` : '');
    return str(p.kind) || 'proposal';
  }
  // What an approval changes, said before it happens.
  function confirmFor(p) {
    const n = fpsOf(p).length;
    if (!p || typeof p !== 'object') return 'Approve this proposal?';
    if (p.kind === 'duplicate') return `Mark ${noun(1)} ${str(p.fp)} as a duplicate of ${str(p.of)}?`;
    if (p.kind === 'fixed') return `Mark ${noun(1)} ${str(p.fp)} fixed${p.commit ? ` by ${str(p.commit)}` : ''}?`;
    if (p.kind === 'tag') return `Add ${list(p.tags).join(', ')} to ${noun(n)}?`;
    if (p.kind === 'issue') return `Mark ${noun(n)} to file as "${str(p.title)}"? (No GitHub issue is made yet.)`;
    if (p.kind === 'plan') return 'Create the plan dir and print its plan command (nothing runs)?';
    return 'Approve this proposal?';
  }
  // The `forge findings …` command of each action. `p` is the proposal (id, hash) or, for `triage`, {spec}.
  function cliFor(op, repo, n, p) {
    const r = shellQuote(repo), o = p || {};
    if (op === 'triage') return `forge findings triage ${r} --spec ${shellQuote(o.spec || 'SPEC')} --detach`;
    if (op === 'proposals') return `forge findings proposals ${r}`;
    if (op === 'apply') return `forge findings apply ${r} ${shellQuote(n)} ${shellQuote(o.id)} --approve ${shellQuote(o.hash)}`;
    if (op === 'reject') return `forge findings reject ${r} ${shellQuote(n)} ${shellQuote(o.id)}`;
    return '';
  }
  const isActive = state => !!state && typeof state === 'object' && state.state === 'running';
  const pollDelay = state => isActive(state) ? POLL_MS : null;
  // "1m 05s" from an ISO start (and an ISO or ms end) to now; null when the start is not a time.
  function elapsed(started, end) {
    const from = Date.parse(started);
    if (!Number.isFinite(from)) return null;
    const to = end == null ? Date.now() : typeof end === 'number' ? end : Date.parse(end);
    if (!Number.isFinite(to)) return null;
    const s = Math.max(0, Math.round((to - from) / 1000));
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h ${String(Math.floor(s / 60) % 60).padStart(2, '0')}m`;
  }
  // The harness and model a spec resolved to, as one line.
  function resolvedText(resolved) {
    if (typeof resolved === 'string') return resolved;
    const r = resolved && typeof resolved === 'object' ? resolved : {};
    const head = [r.harness, r.model].filter(Boolean).join(' · ');
    const extra = [r.effort ? `effort ${r.effort}` : '', r.alias ? `alias ${r.alias}` : ''].filter(Boolean).join(', ');
    return (head || 'resolved') + (extra ? ` (${extra})` : '');
  }
  // max findings: '' → null (the server's default); 1..2000 → the number; anything else → NaN.
  function parseMax(text) {
    const t = str(text).trim();
    if (!t) return null;
    const n = Number(t);
    return /^\d+$/.test(t) && n >= 1 && n <= MAX_LIMIT ? n : NaN;
  }

  /* ---------- DOM ---------- */
  const forgeUI = () => root && root.ForgeUI ? root.ForgeUI : globalThis.ForgeUI;
  function el(tag, text, className) { return forgeUI().el(tag, text, className); }
  function timers() { return globalThis; }

  function render(host, ctx) {
    ctx = ctx || {};
    const UI = forgeUI();
    const store = ctx.store && typeof ctx.store === 'object' ? ctx.store : {};
    if (!store.notes) store.notes = {};
    const enc = encodeURIComponent, base = `/api/projects/${enc(ctx.id)}/triage`;
    let live = true, writes = ctx.writes === true, pollTimer = null, checkTimer = null, checkSeq = 0, loadSeq = 0;
    let check = { ok: false, text: '', tone: 'muted' }, starting = false, loadError = null;
    const rowsByFp = () => new Map(list(ctx.rows && ctx.rows()).filter(r => r && r.record_fp).map(r => [r.record_fp, r]));

    const box = el('details', undefined, 'triage');
    const title = el('summary', undefined, 'triage-title');
    const badge = el('span', '', 'triage-badge');
    title.append(el('span', 'Triage with an agent', 'plate'), badge);
    const startHost = el('div', undefined, 'triage-start'), view = el('div', undefined, 'triage-view');
    const inner = el('div', undefined, 'triage-body');
    inner.append(startHost, view);
    box.append(title, inner);
    host.replaceChildren(box);
    box.open = !!store.open;

    function cancelConfirms() {
      for (const strip of [...(startHost.children || [])]) {
        if (!strip.classList || !strip.classList.contains('ui-confirm')) continue;
        for (const b of strip.children || []) if (b.tagName === 'BUTTON' && b.textContent === 'Cancel' && b.click) { b.click(); break; }
      }
    }
    function stop() {
      live = false;
      timers().clearTimeout(pollTimer); pollTimer = null;
      timers().clearTimeout(checkTimer); checkTimer = null;
      cancelConfirms();
    }
    const alive = () => live && host.isConnected !== false;

    /* the state, and the one polling timer */
    const stateNow = () => store.data && store.data.state || null;
    function schedule() {
      timers().clearTimeout(pollTimer); pollTimer = null;
      if (!alive()) { if (live && host.isConnected === false) stop(); return; }
      const delay = pollDelay(stateNow());
      if (delay == null) return;
      pollTimer = timers().setTimeout(async () => { pollTimer = null; if (alive()) await load(); else stop(); }, delay);
    }
    async function load(n) {
      const mine = ++loadSeq;
      const res = await UI.get(base + (n ? `?n=${enc(n)}` : ''));
      if (!live || mine !== loadSeq) return;
      if (res.ok) { store.data = { state: res.data.state || null, proposals: res.data.proposals || null }; loadError = null; }
      else loadError = res.detail || res.error || 'no answer';
      drawView();
      schedule();
    }

    /* the start form */
    const specInput = el('input'); specInput.type = 'text'; specInput.maxLength = 120; specInput.placeholder = 'e.g. sol:high'; specInput.value = store.spec || '';
    specInput.setAttribute('aria-label', 'Model spec'); specInput.dataset.field = 'spec';
    const maxInput = el('input'); maxInput.type = 'number'; maxInput.min = '1'; maxInput.max = String(MAX_LIMIT); maxInput.placeholder = 'all';
    maxInput.setAttribute('aria-label', 'Most findings to read'); maxInput.dataset.field = 'max'; maxInput.value = store.max || '';
    const checkLine = el('p', '', 'triage-check muted'); checkLine.setAttribute('aria-live', 'polite');
    const startKey = el('button', 'Start triage', 'primary'); startKey.type = 'button'; startKey.dataset.op = 'start';
    const confirmHost = el('div', undefined, 'triage-confirm'), msgs = el('div', undefined, 'triage-msgs');
    const running = () => isActive(stateNow());
    function syncStart() {
      const max = parseMax(maxInput.value);
      startKey.disabled = !check.ok || Number.isNaN(max) || running() || starting;
      startKey.title = running() ? 'a triage is already running' : '';
      checkLine.textContent = Number.isNaN(max) ? `Most findings: a whole number from 1 to ${MAX_LIMIT}.` : check.text;
      checkLine.className = 'triage-check ' + (Number.isNaN(max) ? 'warn' : check.tone);
    }
    async function runCheck() {
      checkTimer = null;
      const spec = specInput.value.trim(), mine = ++checkSeq;
      if (!spec) { check = { ok: false, text: '', tone: 'muted' }; syncStart(); return; }
      const res = await UI.get('/api/specs/check?spec=' + enc(spec));
      if (!live || mine !== checkSeq) return;
      if (res.ok && res.data.ok === true) check = { ok: true, text: 'Runs as ' + resolvedText(res.data.resolved), tone: 'muted' };
      else check = { ok: false, text: res.ok ? str(res.data.error) || 'this spec does not resolve' : res.detail || res.error || 'could not check the spec', tone: 'warn' };
      syncStart();
    }
    function typed() {
      store.spec = specInput.value;
      check = { ok: false, text: specInput.value.trim() ? 'Checking the spec…' : '', tone: 'muted' };
      checkSeq += 1;
      timers().clearTimeout(checkTimer); checkTimer = null;
      syncStart();
      if (specInput.value.trim()) checkTimer = timers().setTimeout(runCheck, CHECK_MS);
    }
    async function start() {
      if (startKey.disabled) return;
      const spec = specInput.value.trim(), max = parseMax(maxInput.value);
      const open = Number.isFinite(ctx.openCount && ctx.openCount()) ? ctx.openCount() : null;
      const count = open == null ? null : max ? Math.min(open, max) : open;
      const what = count == null ? 'the open findings' : `${count} open finding${count === 1 ? '' : 's'}`;
      starting = true; syncStart(); msgs.replaceChildren();
      try {
        const yes = await UI.confirmInline(confirmHost, `Start a read-only triage of ${what} with ${spec}? It uses that model's quota.`, {});
        if (!live || !yes) return;
        const body = { spec };
        if (max) body.max_findings = max;
        const res = await UI.post(base, body);
        if (res && res.ok) store.data = { state: { state: 'running', spec, started: new Date().toISOString(), findings: count, triage: res.data.triage }, proposals: null };
        if (!live) return;
        if (!res || !res.ok) { UI.outcome(msgs, res); return; }
        drawView(); schedule();
        load(res.data.triage);
      } finally {
        starting = false;
        if (live) syncStart();
      }
    }
    function drawStart() {
      startHost.replaceChildren();
      if (!writes) {
        const cmds = el('div', undefined, 'triage-cmds');
        cmds.append(el('p', 'This page is read-only. Start and read a triage from a terminal:', 'muted'));
        for (const op of ['triage', 'proposals']) cmds.append(commandRow(cliFor(op, ctx.root, null, {})));
        startHost.append(cmds);
        return;
      }
      const row = el('div', undefined, 'triage-form');
      const l1 = el('label', undefined, 'fnd-field'), l2 = el('label', undefined, 'fnd-field');
      l1.append(el('span', 'Model', 'plate'), specInput); l2.append(el('span', 'Most findings', 'plate'), maxInput);
      row.append(l1, l2, startKey);
      startHost.append(row, checkLine, confirmHost, msgs);
      syncStart();
    }
    specInput.addEventListener('input', typed);
    maxInput.addEventListener('input', () => { store.max = maxInput.value; syncStart(); });
    startKey.addEventListener('click', () => { start(); });

    function commandRow(command) {
      const row = el('div', undefined, 'fnd-cmd'), code = el('code', command, 'mono'); code.tabIndex = 0;
      const b = el('button', 'Copy', 'fnd-copy'); b.type = 'button'; b.setAttribute('aria-label', 'Copy: ' + command);
      b.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); b.textContent = 'Copied'; } catch (_) { b.textContent = 'Copy failed: select it'; }
        timers().setTimeout(() => { b.textContent = 'Copy'; }, 2500);
      });
      row.append(code, b);
      return row;
    }

    /* the proposals */
    function findingRef(fp, rows) {
      const li = el('li', undefined, 'triage-fp'), row = rows.get(fp);
      const label = el(ctx.show && row ? 'button' : 'span', fp, 'mono triage-fp-id');
      if (label.tagName === 'BUTTON') {
        label.type = 'button'; label.title = 'show this finding in the table';
        label.addEventListener('click', () => ctx.show(fp));
      }
      li.append(label);
      if (row && row.summary) {
        const text = str(row.summary).replace(/^[-*\s]*(\[[^\]]*\]\s*)*/, '').slice(0, 200);
        li.append(el('span', ' ' + (text || row.summary), 'muted triage-fp-text'));
      }
      return li;
    }
    async function decide(card, p, n, op, input, parts) {
      if (card.dataset.busy === '1') return;
      let body;
      if (op === 'apply') {
        body = { hash: p.hash };
        const dir = input && input.value.trim();
        if (dir) body.plan_dir = dir;
        card.dataset.busy = '1'; setBusy(parts, true);
        const yes = await UI.confirmInline(parts.confirm, confirmFor(p), {});
        if (!live || !yes) { card.dataset.busy = '0'; if (live) setBusy(parts, false); return; }
      } else {
        body = {};
        const reason = input && input.value.trim();
        if (reason) body.reason = reason.slice(0, MAX_REASON);
        card.dataset.busy = '1'; setBusy(parts, true);
      }
      const res = await UI.post(`${base}/${enc(n)}/${enc(p.id)}/${op}`, body);
      card.dataset.busy = '0';
      if (res && res.ok && op === 'apply' && res.data && (res.data.next || res.data.plan_dir)) {
        store.notes[`${n}:${p.id}`] = { plan_dir: res.data.plan_dir || null, next: res.data.next || null };
      }
      if (!live) { if (res && res.ok && ctx.reload) ctx.reload(); return; }
      if (!res || !res.ok) { setBusy(parts, false); UI.outcome(parts.msg, res); return; }
      await load(n);
      if (ctx.reload) ctx.reload();
    }
    function setBusy(parts, on) {
      if (on) parts.card.setAttribute('aria-busy', 'true'); else parts.card.removeAttribute('aria-busy');
      for (const b of parts.buttons) b.disabled = on || (b.dataset.op === 'approve' && !parts.hash);
    }
    function proposalCard(p, n, rows) {
      const card = el('article', undefined, 'triage-card'); card.dataset.pid = str(p.id); card.dataset.kind = str(p.kind);
      const top = el('div', undefined, 'triage-card-top');
      top.append(el('span', str(p.kind) || '—', 'tag'), el('span', str(p.state) || '—', 'tag triage-state triage-state-' + str(p.state)), el('span', str(p.id), 'mono muted'));
      card.append(top, el('p', proposalSummary(p), 'triage-summary'));
      const fps = fpsOf(p);
      if (fps.length) { const ul = el('ul', undefined, 'triage-fps'); for (const fp of fps) ul.append(findingRef(fp, rows)); card.append(ul); }
      if (p.kind === 'duplicate' && p.why) card.append(el('p', 'Why: ' + str(p.why), 'triage-why'));
      if (p.kind === 'fixed') {
        if (p.evidence) card.append(el('p', 'Evidence: ' + str(p.evidence), 'triage-why'));
        if (p.commit) card.append(el('p', 'Commit: ' + str(p.commit), 'triage-why mono'));
      }
      if (p.kind === 'tag') {
        const chips = el('div', undefined, 'fnd-tags');
        for (const t of list(p.tags)) chips.append(el('span', str(t), 'fnd-tag'));
        card.append(chips);
      }
      if (p.kind === 'issue' || p.kind === 'plan') card.append(el('p', str(p.title), 'triage-title-line'));
      if (p.kind === 'issue') {
        const d = el('details', undefined, 'triage-details'); d.append(el('summary', 'Issue body'), el('pre', str(p.body), 'triage-pre'));
        card.append(d);
      }
      if (p.kind === 'plan') {
        if (p.request) { const d = el('details', undefined, 'triage-details'); d.append(el('summary', 'Request'), el('pre', str(p.request), 'triage-pre')); card.append(d); }
        const tasks = list(p.plan && p.plan.tasks);
        if (tasks.length) {
          const ul = el('ul', undefined, 'triage-tasks');
          for (const t of tasks) {
            const li = el('li');
            li.append(el('span', str(t && t.id), 'mono'), el('span', ' ' + str(t && t.difficulty || '—'), 'tag'), el('span', ' ' + str(t && t.title)));
            ul.append(li);
          }
          card.append(ul);
        }
      }
      if (p.hash) card.append(el('code', str(p.hash), 'mono triage-hash'));
      if (p.decided && typeof p.decided === 'object') {
        card.append(el('p', `${str(p.state)} ${str(p.decided.at)}${p.decided.actor ? ' by ' + str(p.decided.actor) : ''}${p.decided.reason ? ': ' + str(p.decided.reason) : ''}`, 'muted triage-decided'));
      }
      const note = store.notes[`${n}:${p.id}`];
      const planDir = p.plan_dir || (note && note.plan_dir);
      if (planDir) card.append(el('p', 'Plan dir: ' + str(planDir), 'triage-why mono'));
      if (note && note.next) { card.append(el('p', 'Next, to run it (nothing has run):', 'muted'), commandRow(note.next)); }
      const confirm = el('div', undefined, 'triage-confirm'), msg = el('div', undefined, 'triage-msgs');
      const parts = { card, confirm, msg, buttons: [], hash: p.hash };
      if (p.state === 'pending') {
        if (writes) {
          const actions = el('div', undefined, 'triage-actions');
          const approve = el('button', 'Approve', 'primary'); approve.type = 'button'; approve.dataset.op = 'approve'; approve.disabled = !p.hash;
          let dirInput = null;
          if (p.kind === 'plan') {
            dirInput = el('input'); dirInput.type = 'text'; dirInput.placeholder = 'plan dir (optional)'; dirInput.setAttribute('aria-label', 'Plan dir'); dirInput.dataset.field = 'plan-dir';
          }
          approve.addEventListener('click', () => { decide(card, p, n, 'apply', dirInput, parts); });
          const reject = el('button', 'Reject'); reject.type = 'button'; reject.dataset.op = 'reject';
          const reason = el('input'); reason.type = 'text'; reason.maxLength = MAX_REASON; reason.placeholder = 'Reason (optional)'; reason.setAttribute('aria-label', 'Reject reason'); reason.dataset.field = 'reason';
          reject.addEventListener('click', () => { decide(card, p, n, 'reject', reason, parts); });
          parts.buttons.push(approve, reject);
          actions.append(approve);
          if (dirInput) actions.append(dirInput);
          actions.append(reject, reason);
          card.append(actions);
        } else {
          card.append(el('p', 'Change it from a terminal:', 'muted'));
          card.append(commandRow(cliFor('apply', ctx.root, n, p)), commandRow(cliFor('reject', ctx.root, n, p)));
        }
      }
      card.append(confirm, msg);
      return card;
    }

    function statusBlock(st) {
      const wrap = el('div', undefined, 'triage-status');
      const tone = st.state === 'running' ? 'running' : st.state === 'done' ? 'pass' : 'fail';
      const head = el('p', undefined, 'triage-state-line');
      head.append(el('i', undefined, 'lamp st-' + tone), el('span', ' ' + str(st.state), 'triage-state-name'));
      const bits = [];
      if (st.spec) bits.push(str(st.spec));
      const time = st.state === 'running' ? elapsed(st.started) : elapsed(st.started, st.finished);
      if (time) bits.push(st.state === 'running' ? `running for ${time}` : `took ${time}`);
      if (Number.isFinite(st.findings)) bits.push(noun(st.findings));
      if (st.state === 'done' && Number.isFinite(st.proposals)) bits.push(`${st.proposals} proposal${st.proposals === 1 ? '' : 's'}`);
      head.append(el('span', bits.length ? ' · ' + bits.join(' · ') : '', 'muted'));
      wrap.append(head);
      if (st.state === 'failed') wrap.append(el('p', 'Failed: ' + (str(st.reason) || 'no reason given'), 'warn triage-reason'));
      return wrap;
    }
    function drawView() {
      const st = stateNow();
      badge.textContent = st ? str(st.state) : '';
      badge.className = 'triage-badge' + (st ? ' triage-badge-' + str(st.state) : '');
      syncStart();
      view.replaceChildren();
      if (loadError) view.append(el('p', `Could not read the triage (${loadError}).`, 'warn'));
      if (!store.data) { if (!loadError) view.append(el('p', 'Reading the triage…', 'muted')); return; }
      if (!st) { view.append(el('p', 'No triage has run for this project yet.', 'muted')); return; }
      view.append(statusBlock(st));
      if (!writes) {
        view.append(commandRow(cliFor('proposals', ctx.root, null, {})));
      }
      const proposals = list(store.data.proposals && store.data.proposals.proposals).filter(p => p && typeof p === 'object');
      if (st.state !== 'done' && !proposals.length) return;
      const n = st.triage || (store.data.proposals && store.data.proposals.triage) || null;
      if (!proposals.length) { if (st.state === 'done') view.append(el('p', 'The agent proposed nothing.', 'muted')); return; }
      const rows = rowsByFp();
      for (const kind of KINDS) {
        const items = proposals.filter(p => p.kind === kind);
        if (!items.length) continue;
        const sec = el('section', undefined, 'triage-group'); sec.dataset.kind = kind;
        const head = el('p', undefined, 'brief-list-head');
        head.append(el('span', KIND_TITLE[kind], 'plate'), el('span', String(items.length), 'module-count'));
        sec.append(head);
        for (const p of items) sec.append(proposalCard(p, n, rows));
        view.append(sec);
      }
    }

    /* mount */
    box.addEventListener('toggle', () => {
      store.open = !!box.open;
      if (box.open && live && !store.data) load();
    });
    drawStart();
    drawView();
    if (writes !== true && typeof ctx.writes !== 'boolean') {
      UI.controlsEnabled().then(on => { if (!live) return; writes = !!on; drawStart(); drawView(); });
    }
    if (store.data) { schedule(); if (box.open) load(); }
    else if (box.open) load();
    return { stop, refresh: () => load(), box };
  }

  return { render, proposalSummary, confirmFor, cliFor, isActive, pollDelay, elapsed, fpsOf, parseMax, resolvedText, shellQuote, POLL_MS, CHECK_MS, KINDS };
});
