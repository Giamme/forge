/* forge-ui quick jump: Ctrl/⌘-K or "/" opens one search over every run, task, project, model and command.
   Enter opens the pick, Shift+Enter opens a run (or a task's run) in the cloud. Recent picks come first
   when nothing is typed. All values enter the DOM as text.
   window.ForgeSearch = { init(ctx) }, where ctx = { runs(), commands(), go(href), cloudFilter(filter) }. */
window.ForgeSearch = (() => {
  'use strict';
  const RM = window.ForgeRunModel, CM = window.ForgeCloudModel;
  const RECENT = 'forge-ui-recent', RECENT_CAP = 30;
  const GROUP = { command: 'Commands', run: 'Runs', task: 'Tasks', project: 'Projects', model: 'Models' };
  const PER = { run: 8, task: 8, project: 5, model: 5, command: 6 };
  const STATE_ST = { live: 'running', failed: 'fail', done: 'merged', pending: 'pending' };
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  const loadRecent = () => { try { const v = JSON.parse(localStorage.getItem(RECENT) || '{}'); return v && typeof v === 'object' ? v : {}; } catch (_) { return {}; } };
  function saveRecent(id) {
    const r = loadRecent(); r[id] = Date.now() / 1000;
    const keep = Object.entries(r).sort((a, b) => b[1] - a[1]).slice(0, RECENT_CAP);
    try { localStorage.setItem(RECENT, JSON.stringify(Object.fromEntries(keep))); } catch (_) { /* not remembered */ }
  }
  const typing = t => t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');

  function init(ctx) {
    const dlg = el('dialog', undefined, 'jump'); dlg.setAttribute('aria-label', 'Jump to');
    const input = el('input', undefined, 'jump-input');
    input.type = 'search'; input.autocomplete = 'off'; input.spellcheck = false;
    input.placeholder = 'Jump to a run, task, project, model or command…';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-controls', 'jump-list'); input.setAttribute('aria-autocomplete', 'list');
    const listEl = el('ul', undefined, 'jump-list'); listEl.id = 'jump-list'; listEl.setAttribute('role', 'listbox'); listEl.setAttribute('aria-label', 'Results');
    const foot = el('p', 'Enter opens · Shift+Enter opens in the cloud · ↑ ↓ choose · Esc closes', 'jump-foot');
    dlg.append(input, listEl, foot); document.body.append(dlg);
    let items = [], active = 0;
    const cache = { ref: null, idx: [] };

    function index() {
      const runs = ctx.runs();
      if (runs !== cache.ref) {
        cache.ref = runs;
        const projects = CM ? CM.projects(runs).map(p => ({ id: 'project:' + p.id, kind: 'project', label: p.name, sub: `${p.count} run${p.count === 1 ? '' : 's'} · ${p.repo || ''}`,
          ids: [], words: [p.repo], t: Number.isFinite(p.last) ? p.last : 0, href: '#/cloud/projects/' + encodeURIComponent(p.id) })) : [];
        const used = new Map();
        for (const r of runs) for (const a of new Set((r.models || []).map(RM.alias).filter(Boolean))) { const u = used.get(a) || { n: 0, t: 0 }; u.n++; u.t = Math.max(u.t, r.updated || 0); used.set(a, u); }
        const models = [...used].map(([a, u]) => ({ id: 'model:' + a, kind: 'model', label: a, sub: `in ${u.n} run${u.n === 1 ? '' : 's'} · shows them in the cloud`, ids: [a], t: u.t,
          action: () => ctx.cloudFilter({ models: { aliases: [a], role: 'any' } }) }));
        cache.idx = RM.searchIndex(runs).concat(projects, models);
      }
      return cache.idx.concat(ctx.commands().map(c => ({ ...c, kind: 'command' })));
    }
    function labelWithMark(e) {
      const span = el('span', undefined, 'jump-label'), s = String(e.label || '');
      if (e.mark && e.mark[1] > e.mark[0]) span.append(document.createTextNode(s.slice(0, e.mark[0])), el('mark', s.slice(e.mark[0], e.mark[1])), document.createTextNode(s.slice(e.mark[1])));
      else span.textContent = s;
      return span;
    }
    function render() {
      const ranked = RM.searchRank(index(), input.value, { now: Date.now() / 1000, recent: loadRecent(), limit: 300 });
      const groups = new Map();
      for (const e of ranked) { const g = groups.get(e.kind) || []; if (g.length < (PER[e.kind] || 5)) g.push(e); groups.set(e.kind, g); }
      items = []; listEl.replaceChildren();
      for (const [kind, es] of groups) {
        if (!es.length) continue;
        const h = el('li', GROUP[kind] || kind, 'jump-group'); h.setAttribute('role', 'presentation'); listEl.append(h);
        for (const e of es) {
          const i = items.length, li = el('li', undefined, 'jump-item');
          li.id = 'jump-opt-' + i; li.setAttribute('role', 'option'); li.dataset.i = String(i);
          const dot = el('i', undefined, 'led st-' + (e.kind === 'run' ? STATE_ST[e.state] || 'unknown' : e.kind === 'task' ? String(e.status || 'pending').toLowerCase() : 'none'));
          const text = el('span', undefined, 'jump-text'); text.append(labelWithMark(e), el('span', e.sub || '', 'jump-sub'));
          li.append(dot, text);
          if (e.kind === 'run' || e.kind === 'task') li.title = 'Enter opens · Shift+Enter shows it in the cloud';
          listEl.append(li); items.push({ e, li });
        }
      }
      if (!items.length) { const li = el('li', input.value.trim() ? 'Nothing matches.' : 'No runs yet.', 'jump-empty'); li.setAttribute('role', 'presentation'); listEl.append(li); }
      setActive(Math.min(active, Math.max(0, items.length - 1)));
    }
    function setActive(i) {
      active = i;
      items.forEach((it, k) => it.li.setAttribute('aria-selected', String(k === i)));
      const cur = items[i];
      if (cur) { input.setAttribute('aria-activedescendant', cur.li.id); cur.li.scrollIntoView({ block: 'nearest' }); } else input.removeAttribute('aria-activedescendant');
    }
    function pick(e, inCloud) {
      saveRecent(e.id); close();
      if (inCloud && (e.kind === 'run' || e.kind === 'task')) ctx.go('#/cloud/runs/' + encodeURIComponent(e.key));
      else if (e.action) e.action();
      else if (e.href) ctx.go(e.href);
    }
    function open() {
      if (dlg.open) { input.select(); return; }
      input.value = ''; active = 0; render();
      dlg.showModal(); input.focus();
    }
    function close() { if (dlg.open) dlg.close(); }

    input.addEventListener('input', () => { active = 0; render(); });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (items.length) setActive((active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length); }
      else if (e.key === 'PageDown' || e.key === 'PageUp') { e.preventDefault(); if (items.length) setActive(Math.max(0, Math.min(items.length - 1, active + (e.key === 'PageDown' ? 8 : -8)))); }
      else if (e.key === 'Enter') { e.preventDefault(); const cur = items[active]; if (cur) pick(cur.e, e.shiftKey); }
    });
    listEl.addEventListener('mousemove', e => { const li = e.target.closest('[role=option]'); if (li && Number(li.dataset.i) !== active) setActive(Number(li.dataset.i)); });
    listEl.addEventListener('click', e => { const li = e.target.closest('[role=option]'); if (li) pick(items[Number(li.dataset.i)].e, e.shiftKey); });
    dlg.addEventListener('click', e => { if (e.target === dlg) close(); });       // the backdrop
    document.addEventListener('keydown', e => {
      if ((e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey) && !e.altKey) { e.preventDefault(); if (dlg.open) close(); else open(); }
      // a view that handles / itself (a conversation's search) keeps it
      else if (e.key === '/' && !e.defaultPrevented && !e.ctrlKey && !e.metaKey && !e.altKey && !dlg.open && !typing(e.target)) { e.preventDefault(); open(); }
    });
    for (const b of document.querySelectorAll('[data-jump]')) {
      b.addEventListener('click', open);
      const k = b.querySelector('kbd'); if (k) k.textContent = mac ? '⌘K' : 'Ctrl K';
    }
    return { open, close, get isOpen() { return dlg.open; } };
  }
  return { init };
})();
