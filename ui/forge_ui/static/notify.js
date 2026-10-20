/* forge-ui notifications: a run that finishes or fails, a task that fails, a final review's verdict.
   Seen on every poll of the run list (RM.statusEvents); shown in the bell's log, as an unread count in the
   tab title and a dot on its icon, and, once allowed from the bell, as browser notifications while this tab
   is in the background. Several open tabs notify once. All values enter the DOM as text.
   window.ForgeNotify = { init(ctx), observe(runs), open(), background() }, ctx = { go(href) }. */
window.ForgeNotify = (() => {
  'use strict';
  const RM = window.ForgeRunModel;
  const PREFS = 'forge-ui-notify', LOG = 'forge-ui-notify-log', SEEN = 'forge-ui-notify-seen';
  const LOG_CAP = 100, SEEN_TTL = 600;
  const KINDS = [['run', 'Runs that finish or fail'], ['task', 'Tasks that fail'], ['review', 'Final review verdicts']];
  const kindOf = e => e.kind === 'task-failed' ? 'task' : e.kind === 'review' ? 'review' : 'run';
  const get = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k) || 'null'); return v === null ? d : v; } catch (_) { return d; } };
  const set = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode: this tab only */ } };
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function words(e) {
    const where = [e.repo, e.run_id].filter(Boolean).join(' · ');
    if (e.kind === 'run-done') return { title: 'Run finished', body: e.goal, foot: where, bad: false };
    if (e.kind === 'run-failed') return { title: 'Run ended with failures', body: e.goal, foot: where, bad: true };
    if (e.kind === 'task-failed') return { title: `Task ${e.task} ${String(e.status).toLowerCase()}`, body: e.title ? `${e.title} — ${e.goal}` : e.goal, foot: where, bad: true };
    return { title: `Final review ${e.verdict === 'PASS' ? 'passed' : 'failed'}`, body: e.goal, foot: where, bad: e.verdict !== 'PASS' };
  }
  const supported = () => typeof Notification === 'function';

  let ctx = {}, prev = null, prefs = null, bell = null, badge = null, pop = null, toast = null, toastTimer = 0, icon = null, baseTitle = document.title || 'forge-ui';
  const log = () => { const v = get(LOG, []); return Array.isArray(v) ? v : []; };
  const unread = () => log().filter(x => !x.read);

  /* ---------- the bell and its popover ---------- */
  function init(c) {
    ctx = c || {};
    prefs = Object.assign({ kinds: { run: true, task: true, review: true }, browser: false }, get(PREFS, {}));
    bell = document.getElementById('bell'); badge = bell && bell.querySelector('.bell-count');
    icon = document.querySelector('link[rel=icon]');
    pop = el('div', undefined, 'notify-pop'); pop.id = 'notify-pop'; pop.hidden = true; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', 'Notifications');
    toast = el('div', undefined, 'notify-toast'); toast.hidden = true; toast.setAttribute('role', 'status');
    document.body.append(pop, toast);
    if (bell) {
      bell.setAttribute('aria-controls', 'notify-pop'); bell.setAttribute('aria-expanded', 'false');
      bell.addEventListener('click', e => { e.stopPropagation(); if (pop.hidden) open(); else close(); });
    }
    document.addEventListener('click', e => { if (!pop.hidden && !pop.contains(e.target) && e.target !== bell) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !pop.hidden) { close(); bell && bell.focus(); } });
    // another tab read, cleared or logged something: follow it
    window.addEventListener('storage', e => { if (e.key === LOG || e.key === PREFS) { if (e.key === PREFS) prefs = Object.assign(prefs, get(PREFS, {})); sync(); if (!pop.hidden) renderPop(); } });
    sync();
  }
  function open() {
    renderPop(); pop.hidden = false; bell && bell.setAttribute('aria-expanded', 'true');
    const r = bell && bell.getBoundingClientRect();
    if (r) { pop.style.top = `${Math.round(r.bottom + 8)}px`; pop.style.right = `${Math.max(8, Math.round(window.innerWidth - r.right))}px`; }
    const first = pop.querySelector('button, input, a'); if (first) first.focus({ preventScroll: true });
    markRead();
  }
  function close() { pop.hidden = true; bell && bell.setAttribute('aria-expanded', 'false'); }
  function savePrefs() { set(PREFS, prefs); }
  function renderPop() {
    pop.replaceChildren();
    const head = el('div', undefined, 'notify-head'); head.append(el('h2', 'Notifications'));
    const clear = el('button', 'Clear', 'linkish'); clear.type = 'button';
    clear.addEventListener('click', () => { set(LOG, []); sync(); renderPop(); });
    head.append(clear); pop.append(head);
    const kinds = el('fieldset', undefined, 'notify-kinds'); kinds.append(el('legend', 'Tell me about'));
    for (const [k, label] of KINDS) {
      const l = el('label'), c = el('input'); c.type = 'checkbox'; c.checked = !!prefs.kinds[k];
      c.addEventListener('change', () => { prefs.kinds[k] = c.checked; savePrefs(); });
      l.append(c, document.createTextNode(' ' + label)); kinds.append(l);
    }
    pop.append(kinds);
    const br = el('div', undefined, 'notify-browser');
    if (!supported()) br.append(el('p', 'This browser has no notifications: changes show here and in the tab title.', 'muted'));
    else if (Notification.permission === 'denied') br.append(el('p', 'Browser notifications are blocked for this page in the browser\'s site settings.', 'muted'));
    else if (prefs.browser && Notification.permission === 'granted') {
      br.append(el('p', 'Browser notifications are on while this tab is in the background; the run list is then checked every 30 seconds.', 'muted'));
      const off = el('button', 'Turn off', ''); off.type = 'button'; off.addEventListener('click', () => { prefs.browser = false; savePrefs(); renderPop(); }); br.append(off);
    } else {
      const on = el('button', 'Enable browser notifications', 'primary'); on.type = 'button';
      on.addEventListener('click', async () => {
        let p = Notification.permission;
        try { if (p === 'default') p = await Notification.requestPermission(); } catch (_) { p = Notification.permission; }
        prefs.browser = p === 'granted'; savePrefs(); renderPop();
      });
      br.append(on, el('p', 'Only while this tab is open; nothing leaves this machine.', 'muted'));
    }
    pop.append(br);
    const items = log();
    const ul = el('ul', undefined, 'notify-log');
    if (!items.length) ul.append(el('li', 'Nothing yet. Runs are watched while this page is open.', 'muted'));
    for (const x of items) {
      const li = el('li', undefined, 'notify-item' + (x.read ? '' : ' unread') + (x.bad ? ' bad' : ''));
      const a = el('a', x.title); a.href = x.href; a.addEventListener('click', close);
      li.append(a, el('span', x.body, 'notify-body'), el('span', `${x.foot ? x.foot + ' · ' : ''}${new Date(x.at * 1000).toLocaleString()}`, 'muted notify-when'));
      ul.append(li);
    }
    pop.append(ul);
  }
  function markRead() { const items = log(); if (items.some(x => !x.read)) { set(LOG, items.map(x => ({ ...x, read: true }))); sync(); } }

  /* ---------- the unread count: in the bell, the title and the tab's icon ---------- */
  function sync() {
    const u = unread(), n = u.length;
    if (badge) { badge.textContent = n ? String(n > 99 ? '99+' : n) : ''; badge.hidden = !n; }
    if (bell) bell.setAttribute('aria-label', n ? `Notifications, ${n} unread` : 'Notifications');
    document.title = (n ? `(${n}) ` : '') + baseTitle;
    favicon(n ? (u.some(x => x.bad) ? '#d23b45' : '#2f7de1') : null);
  }
  let iconImg = null;
  function favicon(dot) {
    if (!icon) return;
    if (!dot) { icon.href = 'favicon.svg'; return; }
    const draw = () => {
      const c = document.createElement('canvas'); c.width = c.height = 64;
      const x = c.getContext('2d'); if (!x) return;
      if (iconImg && iconImg.complete && iconImg.naturalWidth) x.drawImage(iconImg, 0, 0, 64, 64);
      x.beginPath(); x.arc(48, 16, 14, 0, Math.PI * 2); x.fillStyle = dot; x.fill(); x.lineWidth = 4; x.strokeStyle = '#ffffff'; x.stroke();
      try { icon.href = c.toDataURL('image/png'); } catch (_) { /* keep the plain icon */ }
    };
    if (!iconImg) { iconImg = new Image(); iconImg.onload = draw; iconImg.src = 'favicon.svg'; }
    draw();
  }

  /* ---------- each poll: what changed, told once across tabs ---------- */
  function claim(ids) {
    const now = Date.now() / 1000, seen = get(SEEN, {}), fresh = [];
    for (const [k, t] of Object.entries(seen)) if (now - t > SEEN_TTL) delete seen[k];
    for (const id of ids) if (!seen[id]) { seen[id] = now; fresh.push(id); }
    set(SEEN, seen);
    return new Set(fresh);
  }
  function deliver(events) {
    const run = () => {
      const mine = claim(events.map(e => e.id));
      const now = Date.now() / 1000, add = [];
      for (const e of events) {
        if (!mine.has(e.id)) continue;
        const w = words(e);
        add.push({ id: e.id, kind: e.kind, href: e.href, title: w.title, body: w.body, foot: w.foot, bad: w.bad, at: now, read: false });
        if (prefs.browser && supported() && Notification.permission === 'granted' && (document.hidden || !document.hasFocus())) {
          try {
            const n = new Notification(w.title, { body: `${w.body}\n${w.foot}`, tag: e.id, icon: 'favicon.svg' });
            n.onclick = () => { window.focus(); if (ctx.go) ctx.go(e.href); n.close(); };
          } catch (_) { /* some platforms only allow notifications from a service worker */ }
        }
      }
      if (!add.length) return;
      set(LOG, add.reverse().concat(log()).slice(0, LOG_CAP));
      sync();
      if (!pop.hidden) renderPop();
      if (!document.hidden) showToast(add[0], add.length);
      const live = document.getElementById('live'); if (live) live.textContent = add.map(x => `${x.title}: ${x.body}`).slice(0, 3).join('; ');
    };
    // one tab at a time decides what is new, so two tabs do not both notify
    if (navigator.locks && navigator.locks.request) navigator.locks.request('forge-ui-notify', run).catch(run); else run();
  }
  function showToast(x, n) {
    clearTimeout(toastTimer);
    const a = el('a', x.title); a.href = x.href;
    toast.replaceChildren(a, el('span', x.body, 'notify-body'));
    if (n > 1) toast.append(el('span', `and ${n - 1} more in the bell`, 'muted'));
    toast.classList.toggle('bad', !!x.bad); toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 7000);
  }
  function observe(runs) {
    if (!prefs) return [];
    const next = Array.isArray(runs) ? runs.map(r => ({ key: r.key, run_id: r.run_id, repo: r.repo, goal: r.goal, updated: r.updated, counts: r.counts, final_review: r.final_review, task_statuses: r.task_statuses })) : [];
    const events = RM.statusEvents(prev, next, { now: Date.now() / 1000 }).filter(e => prefs.kinds[kindOf(e)]);
    prev = next;
    if (events.length) deliver(events);
    return events;
  }
  // poll the run list while the tab is hidden: only when there is someone to tell
  const background = () => !!(prefs && prefs.browser && supported() && Notification.permission === 'granted');
  return { init, observe, open, background };
})();
