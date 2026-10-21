/* forge-ui conversation: what one dwarf or reviewer said and did, read from its agent CLI's log (transcripts.py), shown
   in the stage's place like a diff. Its messages, its thinking folded, each command with its exit and output, each edit
   with its patch and a link to the diff, other tool calls, the final answer and the tokens; a search, the errors one key
   apart, and while the agent runs a poll every few seconds that follows the end. Defines window.ForgeConvo.render(host, ctx).
   All API values enter the DOM as text.
   ctx = { run, spec: {task, exec}, brief (the Execution, when the run lists it), live (false in an export), fetchJSON(path),
           rerender(), closeHref, diffHash(spec) → hash } */
window.ForgeConvo = (() => {
  'use strict';
  const list = v => Array.isArray(v) ? v : [];
  const POLL = 2500;                                         // ms between reads of a log being written
  const FOLD = 14;                                           // lines of an output shown before it folds
  const cache = new Map();                                   // url → {data, error, pending, at, timer}
  const views = new Map();                                   // url → {query, show, follow, open: Set, err}
  const enc = encodeURIComponent;
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  const icon = name => window.ForgeIcons ? window.ForgeIcons.icon(name) : el('span');
  const chip = (text, st) => el('span', text, 'chip st-' + st);
  const num = v => Number.isFinite(Number(v)) && v !== null ? Number(v).toLocaleString() : '—';
  const ICON = { prompt: 'flag', message: 'chat', reasoning: 'thought', command: 'terminal', edit: 'pencil', tool: 'tool', result: 'check', error: 'cross', text: 'keyboard', gap: 'chevron-down' };
  // which kinds each filter key shows
  const SHOW = [['all', 'All', null], ['said', 'Said', ['prompt', 'message', 'reasoning', 'result', 'error']], ['ran', 'Ran', ['command', 'tool', 'error']],
    ['changed', 'Changed', ['edit']]];
  const urlFor = (run, spec) => `/api/runs/${enc(run.key)}/executions/${enc(spec.exec)}/transcript`;

  function read(ctx, url, entry) {
    entry.pending = true;
    ctx.fetchJSON(url).then(d => { entry.data = d; entry.error = null; }, e => { entry.error = e; })
      .finally(() => { entry.at = performance.now(); entry.pending = false; ctx.rerender(); });
  }
  function load(ctx, url) {
    let entry = cache.get(url);
    if (!entry) {
      entry = { pending: false, data: null, error: null, at: 0, timer: null };
      cache.set(url, entry);
      for (const old of cache.keys()) if (cache.size > 12 && old !== url) cache.delete(old);   // the conversations opened last
      read(ctx, url, entry);
    }
    // a finished log never changes; one still being written is read again when the view comes back to it, and a
    // read that failed is tried again after a while
    else if (ctx.live && !entry.pending && !entry.timer && performance.now() - entry.at > (entry.error ? 10 * POLL : POLL) &&
             (entry.error || (entry.data && entry.data.state === 'running'))) read(ctx, url, entry);
    return entry;
  }

  /* ---------- the frame: title, what was read, the tools; built once per conversation ---------- */
  function frame(host, ctx, url, v) {
    host.replaceChildren();
    const f = { url, drawn: [], nodes: [] };
    const top = el('div', undefined, 'diff-top convo-top');
    f.title = el('h2'); f.chips = el('div', undefined, 'chips');
    const close = el('a', undefined, 'diff-close button'); close.href = ctx.closeHref; close.append(icon('close'), document.createTextNode('Close'));
    top.append(f.title, f.chips, close);
    f.meta = el('p', undefined, 'diff-meta convo-meta muted');
    const tools = el('div', undefined, 'convo-tools'); tools.setAttribute('role', 'toolbar'); tools.setAttribute('aria-label', 'Conversation');
    const find = el('label', undefined, 'convo-find'); find.append(icon('search'));
    f.search = el('input'); f.search.type = 'search'; f.search.placeholder = 'Search what it said and ran'; f.search.setAttribute('aria-label', 'Search the conversation');
    f.search.value = v.query;
    f.search.addEventListener('input', () => { v.query = f.search.value; filter(f, v); });
    f.search.addEventListener('keydown', e => { if (e.key === 'Escape' && f.search.value) { e.stopPropagation(); f.search.value = ''; v.query = ''; filter(f, v); } });
    find.append(f.search);
    f.count = el('span', undefined, 'convo-count muted'); f.count.setAttribute('aria-live', 'polite');
    f.show = el('div', undefined, 'seg'); f.show.setAttribute('role', 'group'); f.show.setAttribute('aria-label', 'Show');
    for (const [key, label] of SHOW) {
      const b = el('button', label); b.type = 'button'; b.dataset.show = key;
      b.addEventListener('click', () => { v.show = key; filter(f, v); });
      f.show.append(b);
    }
    f.errs = el('span', undefined, 'convo-errs');
    f.err = el('button', 'Next error', 'convo-err'); f.err.type = 'button'; f.err.title = 'Next error (E)'; f.err.setAttribute('aria-keyshortcuts', 'E');
    f.err.append(el('kbd', 'E'));
    f.err.addEventListener('click', () => nextError(f, v));
    f.follow = el('button', 'Follow', 'convo-follow latch'); f.follow.type = 'button'; f.follow.title = 'Keep the newest event in view while it runs';
    f.follow.addEventListener('click', () => { v.follow = !v.follow; f.follow.setAttribute('aria-pressed', String(v.follow)); if (v.follow) end(f); });
    tools.append(find, f.count, f.show, f.errs, f.err, f.follow);
    f.status = el('div', undefined, 'convo-status');
    f.list = el('ol', undefined, 'convo-list'); f.list.setAttribute('aria-label', 'Events');
    host.append(top, f.meta, tools, f.status, f.list);
    host.tabIndex = -1; host.setAttribute('aria-label', 'Conversation: / searches, E goes to the next error');
    host.onkeydown = e => {
      if (e.altKey || e.ctrlKey || e.metaKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key === '/') { e.preventDefault(); f.search.focus(); f.search.select(); }
      else if (e.key === 'e' || e.key === 'E') { e.preventDefault(); nextError(f, v); }
    };
    // a fold opened or closed stays so through a rebuild
    f.list.addEventListener('toggle', e => { const d = e.target; if (d.dataset && d.dataset.fold) { if (d.open) v.open.add(d.dataset.fold); else v.open.delete(d.dataset.fold); } }, true);
    return f;
  }

  function header(f, ctx, D) {
    const spec = ctx.spec, run = ctx.run, b = ctx.brief || {};
    const owner = D && D.owner || {}, role = (D && D.role) || b.role || String(spec.exec).split('-')[0];
    const where = owner.kind === 'final_review' ? 'Final review' : spec.task === 'solo' || run.kind === 'solo' ? 'Solo run' :
      owner.kind === 'shadow' ? `Task ${String(owner.id).split('/')[0]} · shadow` : spec.task ? `Task ${spec.task}` : 'Run';
    const attempt = D && D.attempt != null ? D.attempt : b.attempt;
    f.title.replaceChildren(el('span', 'Conversation · ', 'muted'), document.createTextNode(`${where} · ${role === 'qa' ? 'reviewer' : role}${attempt != null ? ' · attempt ' + attempt : ''}`));
    const chips = [];
    const s = (D && D.spec) || (b.model && b.model.spec);
    if (s && window.ForgeIcons) chips.push(window.ForgeIcons.modelPlate(s));
    // one lamp: running, else a review's verdict, else how it ended
    const state = (D && D.state) || b.state;
    if (state === 'running') chips.push(chip('running', 'running'));
    else if (b.verdict) chips.push(chip('verdict ' + b.verdict, String(b.verdict).toLowerCase()));
    else if (b.id && window.ForgeRunModel) { const st = window.ForgeRunModel.execStatus(b); chips.push(chip(st, String(st).toLowerCase())); }
    f.chips.replaceChildren(...chips);
    if (!D) { f.meta.replaceChildren(); return; }
    // where it was read from, then the numbers in a describer window
    const t = D.tokens, from = { log: 'read from its log', session: "read from claude's session file", none: 'no log' }[D.source] || '';
    const n = list(D.events).filter(e => e.kind !== 'gap').length;
    const desc = el('span', undefined, 'describer');
    if (t) desc.append(el('span', `${num(t.input)} in`), ...(t.cached ? [el('span', `${num(t.cached)} cached`, 'dim')] : []), el('span', `${num(t.output)} out`));
    if (D.cost != null) desc.append(el('span', '$' + Number(D.cost).toFixed(D.cost < 1 ? 3 : 2)));
    desc.append(el('span', `${n} event${n === 1 ? '' : 's'}`));
    if (D.session) desc.append(el('span', `session ${D.session}`, 'dim'));
    f.meta.replaceChildren(el('span', [D.harness, from].filter(Boolean).join(' · ')), desc);
  }

  /* ---------- one event ---------- */
  const lines = s => String(s || '').split('\n').length;
  function out(parent, text, key, v, { title = 'Output', open = false, cls = '' } = {}) {
    if (text == null || text === '') return;
    const pre = el('pre', text, 'ev-pre ' + cls);
    if (lines(text) <= FOLD && String(text).length < 1600) { parent.append(pre); return; }
    const d = el('details', undefined, 'ev-fold'); d.dataset.fold = key;
    d.open = open || v.open.has(key);
    d.append(el('summary', `${title} · ${lines(text).toLocaleString()} lines`), pre); parent.append(d);
  }
  function patch(parent, text, key, v) {
    const pre = el('pre', undefined, 'ev-pre ev-patch');
    for (const line of String(text).split('\n')) pre.append(el('span', line, line[0] === '+' ? 'add' : line[0] === '-' ? 'del' : null));
    if (lines(text) <= FOLD) { parent.append(pre); return; }
    const d = el('details', undefined, 'ev-fold'); d.dataset.fold = key; d.open = v.open.has(key);
    d.append(el('summary', `Patch · ${lines(text).toLocaleString()} lines`), pre); parent.append(d);
  }
  // a path in the worktree the agent ran in (or the run's repo) as the diff names it
  function rel(D, path, run) {
    for (const r of [D.repo, run && run.repo]) {
      const root = String(r || '').replace(/\/+$/, '');
      if (root && path.startsWith(root + '/')) return path.slice(root.length + 1);
    }
    return path;
  }
  function eventNode(ctx, D, ev, i, v, t0) {
    const kind = ICON[ev.kind] ? ev.kind : 'text';
    const li = el('li', undefined, `ev ev--${kind}${ev.error ? ' is-error' : ''}${ev.running ? ' is-running' : ''}`);
    li.id = 'ev-' + i; li.tabIndex = -1; li.dataset.kind = kind;
    const gut = el('div', undefined, 'ev-gut'); gut.setAttribute('aria-hidden', 'true'); gut.append(icon(ICON[kind]));
    const body = el('div', undefined, 'ev-body');
    const head = el('div', undefined, 'ev-head');
    const when = Number.isFinite(ev.t) && Number.isFinite(t0) ? el('span', '+' + clock(ev.t - t0), 'ev-time') : null;
    const key = i + ':';
    if (kind === 'gap') { li.append(el('p', `… ${num(ev.count)} events left out: the log is too long to show whole …`, 'muted ev-gap')); return li; }
    if (kind === 'prompt' || kind === 'reasoning') {
      const d = el('details', undefined, 'ev-fold'); d.dataset.fold = key + 'x'; d.open = v.open.has(key + 'x');
      const sum = el('summary'); sum.append(el('span', kind === 'prompt' ? 'The task as it got it' : 'Thinking', 'ev-label'), el('span', String(ev.text || '').split('\n').find(s => s.trim()) || '', 'ev-peek'));
      if (when) sum.append(when);
      d.append(sum, el('div', ev.text, 'ev-text' + (kind === 'reasoning' ? ' ev-thought' : '')));
      body.append(d);
    } else if (kind === 'message') {
      head.append(el('span', D.role === 'qa' ? 'Reviewer' : 'Dwarf', 'ev-label')); if (when) head.append(when);
      body.append(head, el('div', ev.text, 'ev-text'));
    } else if (kind === 'command') {
      const st = ev.running ? 'running' : ev.error ? 'fail' : ev.exit === 0 || ev.error === false ? 'pass' : ev.exit != null ? 'fail' : 'none';
      const line = el('span', undefined, 'ev-cmdline'); line.append(el('i', undefined, 'lamp st-' + st), el('code', ev.command || '(no command)', 'ev-cmd'));
      head.append(line);
      if (ev.exit != null) head.append(el('span', 'exit ' + ev.exit, 'tag' + (ev.exit ? ' tag-bad' : '')));
      else if (ev.running) head.append(el('span', 'running', 'tag'));
      if (when) head.append(when);
      body.append(head); out(body, ev.output, key + 'o', v, { open: !!ev.error });
    } else if (kind === 'edit') {
      head.append(el('span', 'Edited', 'ev-label'));
      if (ev.tool && ev.tool.toLowerCase() !== 'edit') head.append(el('span', ev.tool, 'mono muted ev-toolname'));
      for (const p of list(ev.files)) {
        const r = rel(D, String(p), ctx.run);
        if (ctx.diffHash && ctx.spec.task) { const a = el('a', r, 'mono ev-file'); a.href = ctx.diffHash({ kind: 'task', task: ctx.spec.task, source: ctx.spec.exec, file: r }); a.title = 'This file in the diff its review read'; head.append(a); }
        else head.append(el('span', r, 'mono ev-file'));
      }
      if (list(ev.change).length) head.append(el('span', [...new Set(ev.change)].join(', '), 'tag'));
      if (ev.error) head.append(el('span', 'failed', 'tag tag-bad'));
      if (when) head.append(when);
      body.append(head);
      if (ev.patch) patch(body, ev.patch, key + 'p', v); else out(body, ev.input, key + 'i', v, { title: 'Input' });
      out(body, ev.output, key + 'o', v, { open: !!ev.error });
    } else if (kind === 'tool') {
      head.append(el('span', 'Tool', 'ev-label'), el('span', ev.tool || '?', 'mono ev-toolname'));
      if (ev.target) head.append(el('span', rel(D, String(ev.target), ctx.run), 'mono ev-file'));
      if (ev.error) head.append(el('span', 'failed', 'tag tag-bad'));
      if (when) head.append(when);
      body.append(head); out(body, ev.input, key + 'i', v, { title: 'Input' }); out(body, ev.output, key + 'o', v, { open: !!ev.error });
    } else if (kind === 'result') {
      head.append(el('span', ev.error ? 'Ended with an error' : 'Final answer', 'ev-label'));
      const t = ev.tokens; if (t) head.append(el('span', `${num(t.input)} in · ${num(t.output)} out`, 'muted ev-tokens'));
      if (when) head.append(when);
      body.append(head);
      if (!ev.same) body.append(el('div', ev.text, 'ev-text'));
    } else if (kind === 'error') {
      head.append(el('span', 'Error', 'ev-label')); if (when) head.append(when);
      body.append(head, el('div', ev.text, 'ev-text warn'));
    } else {
      head.append(el('span', ev.stream === 'stderr' ? 'stderr' : 'Log', 'ev-label')); if (when) head.append(when);
      body.append(head); out(body, ev.text, key + 't', v, { title: 'Lines' });
    }
    li.append(gut, body);
    li.dataset.find = [ev.text, ev.command, ev.output, ev.input, ev.patch, ev.tool, ev.target, list(ev.files).join(' ')].filter(Boolean).join('\n').toLowerCase();
    return li;
  }
  function clock(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60; return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); }

  /* ---------- the list: rebuilt from the first event that changed, so a live log only appends ---------- */
  function events(f, ctx, D, v) {
    const evs = list(D.events), sigs = evs.map(e => JSON.stringify(e));
    let i = 0;
    while (i < f.drawn.length && i < sigs.length && f.drawn[i] === sigs[i]) i++;
    const grew = sigs.length > f.drawn.length || i < f.drawn.length;
    for (const n of f.nodes.splice(i)) n.remove();
    const t0 = evs.map(e => e.t).find(Number.isFinite);
    for (let k = i; k < evs.length; k++) { const n = eventNode(ctx, D, evs[k], k, v, t0); f.nodes.push(n); f.list.append(n); }
    // a final answer that repeats the agent's last message marks that message instead
    const r = evs.findIndex(e => e.kind === 'result' && e.same);
    let m = r - 1; while (m >= 0 && evs[m].kind !== 'message') m--;
    const said = m >= 0 && f.nodes[m];
    if (said && !said.querySelector('.ev-final')) said.querySelector('.ev-head').append(el('span', 'final answer', 'tag ev-final'));
    f.drawn = sigs;
    return grew && i < sigs.length;
  }
  function filter(f, v) {
    const q = v.query.trim().toLowerCase(), kinds = (SHOW.find(s => s[0] === v.show) || SHOW[0])[2];
    let shown = 0;
    for (const n of f.nodes) {
      const kind = n.dataset.kind;
      const on = (!kinds || kinds.includes(kind)) && (!q || (n.dataset.find || '').includes(q));
      n.hidden = !on; if (on && kind !== 'gap') shown++;
    }
    for (const b of f.show.children) b.setAttribute('aria-pressed', String(b.dataset.show === v.show));
    const total = f.nodes.filter(n => n.dataset.kind !== 'gap').length;
    f.count.textContent = q || kinds ? `${shown} of ${total}` : '';
    const errs = f.nodes.filter(n => n.classList.contains('is-error')).length;
    f.errs.replaceChildren(errs ? chip(`${errs} error${errs === 1 ? '' : 's'}`, 'fail') : el('span', 'no errors', 'muted'));
    f.err.hidden = !errs;
  }
  function nextError(f, v) {
    const errs = f.nodes.filter(n => n.classList.contains('is-error'));
    if (!errs.length) return;
    v.err = (v.err == null ? -1 : v.err) + 1;
    if (v.err >= errs.length) v.err = 0;
    const n = errs[v.err];
    if (n.hidden) { v.query = ''; v.show = 'all'; f.search.value = ''; filter(f, v); }
    for (const d of n.querySelectorAll('details')) d.open = true;
    n.scrollIntoView({ block: 'center' }); n.focus({ preventScroll: true });
    n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash');
  }
  function end(f) { const last = f.nodes[f.nodes.length - 1]; if (last) last.scrollIntoView({ block: 'end' }); }

  /* ---------- the view ---------- */
  function render(host, ctx) {
    const url = urlFor(ctx.run, ctx.spec), entry = load(ctx, url);
    let v = views.get(url);
    if (!v) { v = { query: '', show: 'all', follow: true, open: new Set(), err: null }; views.set(url, v); }
    let f = host.convoFrame;
    const fresh = !f || f.url !== url;
    if (fresh) { f = frame(host, ctx, url, v); host.convoFrame = f; }
    const D = entry.data;
    header(f, ctx, D);
    if (!D) {
      f.status.replaceChildren(el('p', entry.error ? (entry.error.snapshot ? 'This conversation is not in this export: it was made without prompts and texts.' :
        `Could not load this conversation (${entry.error.status === 404 ? 'not found' : entry.error.message}).`) : 'Reading the log…', entry.error ? 'warn' : 'muted'));
      return;
    }
    const running = D.state === 'running' && ctx.live !== false;
    f.follow.hidden = !running; f.follow.setAttribute('aria-pressed', String(v.follow));
    const status = [];
    const ws = list(D.warnings);
    if (ws.length) status.push(el('p', ws[0], 'warn'));
    if (ws.length > 1) { const d = el('details', undefined, 'diff-warnings'); d.append(el('summary', `${ws.length - 1} more warning${ws.length === 2 ? '' : 's'}`), el('pre', ws.slice(1).join('\n'))); status.push(d); }
    if (D.format === 'text' && list(D.events).length) status.push(el('p', `${D.harness || 'This harness'} left plain text, not a stream of events: its log is shown as it is.`, 'muted'));
    if (!list(D.events).length) status.push(el('p', running ? 'Nothing logged yet: the agent has just started.' : D.source === 'none' ? 'No log was kept for this execution.' : 'Its log holds no conversation.', 'muted convo-empty'));
    f.status.replaceChildren(...status);
    const grew = events(f, ctx, D, v);
    filter(f, v);
    if (running && v.follow && (grew || fresh)) requestAnimationFrame(() => end(f));
    // a log still being written: read it again shortly, while this view shows it
    if (running && !entry.timer) {
      entry.timer = setTimeout(() => {
        entry.timer = null;
        if (host.convoFrame === f && host.isConnected && !host.hidden && !entry.pending) read(ctx, url, entry);
      }, POLL);
    }
  }
  return { render, urlFor, cache };
})();
