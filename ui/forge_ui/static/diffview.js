/* forge-ui diff viewer: a task's diff (current, previous, committed, or exactly what one reviewer read), a final
   review's combined diff or a Fractal node's candidate, with each review finding shown under the line it points
   at. Defines window.ForgeDiff.render(host, ctx). All API values enter the DOM as text.
   ctx = { run, spec: {kind: 'task'|'final'|'fractal', task, source, file, name, node}, fetchJSON(path),
           rerender(), href(spec) → hash, closeHref }. */
window.ForgeDiff = (() => {
  'use strict';
  const list = v => Array.isArray(v) ? v : [];
  const SEV = { P0: 'fail', P1: 'conflict', P2: 'timeout', P3: 'unknown' };
  const RANK = { P0: 0, P1: 1, P2: 2, P3: 3 };
  const BUDGET = 4000;                                       // lines drawn before the rest of the files wait for a click
  const cache = new Map();                                   // url → {data, error, pending, fresh}
  const opened = new Set();                                  // files shown past the budget, by url + path
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  const chip = (text, st) => el('span', text, 'chip st-' + st);
  const enc = encodeURIComponent;
  const TASK_FILES = new Set(['current', 'prev', 'committed', 'existing']);
  function urlFor(run, spec) {
    const base = `/api/runs/${enc(run.key)}`;
    if (spec.kind === 'final') return `${base}/final-reviews/${enc(spec.name)}/diff`;
    if (spec.kind === 'fractal') return `${base}/tasks/${enc(spec.task)}/fractal/nodes/${enc(spec.node)}/diff`;
    const src = spec.source || 'current';
    if (TASK_FILES.has(src)) return `${base}/tasks/${enc(spec.task)}/diff${src === 'current' ? '' : '/' + src}`;
    return `${base}/executions/${enc(src)}/diff`;
  }
  function load(ctx, url) {
    const c = cache.get(url), fresh = ctx.run.updated;
    if (c && (c.fresh === fresh || c.pending)) return c;
    const entry = { pending: true, fresh, data: c && c.data || null, error: null };
    cache.set(url, entry);
    ctx.fetchJSON(url).then(d => { entry.data = d; entry.error = null; entry.at = performance.now(); }, e => { entry.error = e; }).finally(() => { entry.pending = false; ctx.rerender(); });
    return entry;
  }
  const pathOf = f => f.new || f.old || '';
  const worst = fs => fs.reduce((w, f) => (w === null || RANK[f.severity] < RANK[w] ? f.severity : w), null);
  // what a finding says after its "- [P1][CONFIRMED] path:line —" head
  function said(f) {
    const s = String(f.summary || f.text || '').split('\n')[0].replace(/^\s*-\s*\[P\d\]\s*(\[[A-Z]+\]\s*)?/, '');
    const i = s.indexOf(' — '); return i >= 0 && i < 200 ? s.slice(i + 3) : s;
  }

  /* ---------- one finding, as a row under its line or in a list ---------- */
  function findingBox(f, where) {
    const box = el('div', undefined, 'diff-finding sev-' + (f.severity || 'P3'));
    const head = el('div', undefined, 'diff-finding-head');
    head.append(chip(f.severity || '?', SEV[f.severity] || 'unknown'));
    if (f.status) head.append(el('span', f.status.toLowerCase(), 'tag'));
    const r = f.review || {};
    head.append(el('span', `${r.kind || 'review'}${r.attempt != null ? ' · attempt ' + r.attempt : ''}${r.spec ? ' · ' + r.spec : ''}`, 'muted'));
    if (where) head.append(el('span', where, 'mono muted'));
    box.append(head, el('p', said(f)));
    const more = String(f.text || '').trim();
    if (more && more !== String(f.summary || '').trim()) { const d = el('details'); d.append(el('summary', 'Details'), el('pre', more)); box.append(d); }
    return box;
  }

  /* ---------- a file: its header, hunks with line numbers, and pinned findings ---------- */
  function fileBlock(ctx, D, f, fi, pins, state) {
    const sec = el('section', undefined, 'diff-file'); sec.id = 'diff-file-' + fi; sec.dataset.path = pathOf(f);
    const head = el('div', undefined, 'diff-file-head');
    const st = { modified: 'running', added: 'merged', deleted: 'fail', renamed: 'nochanges', copied: 'nochanges' }[f.status] || 'unknown';
    head.append(chip(f.status, st));
    const name = el('a', f.status === 'renamed' || f.status === 'copied' ? `${f.old} → ${f.new}` : pathOf(f), 'mono diff-path');
    name.href = ctx.href({ ...ctx.spec, file: pathOf(f) });
    head.append(name, el('span', `+${f.additions} −${f.deletions}`, 'diff-stat'));
    if (f.binary) head.append(el('span', 'binary', 'tag'));
    if (f.truncated) head.append(el('span', 'cut short', 'tag'));
    sec.append(head);
    const fileLevel = pins.filter(p => p.pin.hunk === null || p.pin.hunk === undefined);
    if (fileLevel.length) {
      const fl = el('div', undefined, 'diff-file-findings');
      for (const p of fileLevel) fl.append(findingBox(p.f, p.f.off_diff === 'line outside the changed hunks' ? `line ${p.pin.line}, outside the changed lines` : 'this file'));
      sec.append(fl);
    }
    if (f.binary) { sec.append(el('p', 'A binary file: no lines to show.', 'muted diff-note')); return sec; }
    const lines = f.hunks.reduce((a, h) => a + h.lines.length, 0), key = D.__url + '\n' + pathOf(f);
    if (state.drawn + lines > BUDGET && !opened.has(key) && state.drawn > 0) {
      const b = el('button', `Show this file (${lines} lines)`, 'diff-more'); b.type = 'button';
      b.addEventListener('click', () => { opened.add(key); ctx.rerender(); });
      sec.append(b); return sec;
    }
    state.drawn += lines;
    const at = new Map();                                      // "hunk:index" → findings
    for (const p of pins) if (p.pin.hunk != null) { const k = p.pin.hunk + ':' + (p.pin.index ?? 'end'); if (!at.has(k)) at.set(k, []); at.get(k).push(p); }
    const table = el('table', undefined, 'diff-table'); table.setAttribute('role', 'presentation');
    const body = el('tbody');
    f.hunks.forEach((h, hi) => {
      const hr = el('tr', undefined, 'diff-hunk');
      const hc = el('td', `@@ -${h.old_start},${h.old_lines} +${h.new_start},${h.new_lines} @@${h.section ? ' ' + h.section : ''}`); hc.colSpan = 4; hr.append(hc); body.append(hr);
      let o = h.old_start, n = h.new_start;
      h.lines.forEach((ln, li) => {
        const m = ln[0], tr = el('tr', undefined, 'diff-line ' + (m === '+' ? 'add' : m === '-' ? 'del' : m === '\\' ? 'meta' : 'ctx'));
        const on = m === '+' || m === '\\' ? '' : o++, nn = m === '-' || m === '\\' ? '' : n++;
        tr.append(el('td', on, 'ln'), el('td', nn, 'ln'), el('td', m === '\\' ? '' : m, 'mk'), el('td', m === '\\' ? ln.slice(1).trim() : ln.slice(1), 'code'));
        body.append(tr);
        const here = at.get(hi + ':' + li);
        if (here) for (const p of here) body.append(pinRow(p, p.pin.line));
      });
      const tail = at.get(hi + ':end');
      if (tail) for (const p of tail) body.append(pinRow(p, p.pin.line));
    });
    table.append(body);
    const scroll = el('div', undefined, 'diff-scroll'); scroll.append(table); sec.append(scroll);
    return sec;
  }
  function pinRow(p, line) {
    const tr = el('tr', undefined, 'diff-pin'), td = el('td'); td.colSpan = 4;
    const box = findingBox(p.f, `line ${line}`); box.id = 'diff-finding-' + p.i; box.tabIndex = -1; td.append(box); tr.append(td);
    return tr;
  }

  /* ---------- the viewer ---------- */
  function render(host, ctx) {
    const run = ctx.run, spec = ctx.spec, url = urlFor(run, spec), entry = load(ctx, url);
    // a poll that brought nothing new leaves thousands of rows alone
    const sig = [url, entry.at || 0, !!entry.error, spec.file, opened.size].join('|');
    if (host.diffSig === sig) return;
    host.diffSig = sig;
    host.replaceChildren();
    const D = entry.data;
    const top = el('div', undefined, 'diff-top');
    const what = spec.kind === 'final' ? 'Final review' : spec.kind === 'fractal' ? `${spec.task} · Fractal node ${spec.node}` : spec.task === 'solo' && run.kind === 'solo' ? 'Solo run' : `Task ${spec.task}`;
    const title = el('h2'); title.append(el('span', 'Diff · ', 'muted'), document.createTextNode(what)); top.append(title);
    const close = el('a', 'Close ✕', 'diff-close'); close.href = ctx.closeHref; top.append(close);
    host.append(top);
    if (!D) {
      host.append(el('p', entry.error ? `Could not load this diff (${entry.error.status === 404 ? 'not found' : entry.error.message}).` : 'Loading the diff…', entry.error ? 'warn' : 'muted'));
      return;
    }
    D.__url = url;
    // which diff: the files of the task and the diffs each reviewer read
    const sources = list(D.sources);
    if (sources.length > 1) {
      const bar = el('div', undefined, 'diff-sources'); bar.setAttribute('role', 'group'); bar.setAttribute('aria-label', 'Which diff');
      const cur = spec.kind === 'final' ? spec.name : spec.kind === 'fractal' ? 'candidate' : spec.source || 'current';
      for (const s of sources) {
        const a = el('a', s.label || s.id, 'diff-source'); a.setAttribute('aria-current', String(s.id === cur));
        a.href = spec.kind === 'final' ? ctx.href({ kind: 'final', name: s.id }) : ctx.href({ ...spec, source: s.id, file: null });
        if (s.verdict) a.append(document.createTextNode(' '), chip(s.verdict, String(s.verdict).toLowerCase()));
        a.title = `${s.bytes != null ? s.bytes + ' bytes' : ''}${s.kind ? ' · ' + s.kind : ''}`;
        bar.append(a);
      }
      host.append(bar);
    }
    const src = D.source || {};
    const meta = el('p', undefined, 'diff-meta muted');
    const files = list(D.files), adds = files.reduce((a, f) => a + (f.additions || 0), 0), dels = files.reduce((a, f) => a + (f.deletions || 0), 0);
    const finds = list(D.findings).map((f, i) => ({ f, i, pin: f.pin || null }));
    const pinned = finds.filter(p => p.pin && p.pin.file != null && p.pin.hunk != null && !p.f.off_diff);
    meta.textContent = [src.file, src.attempt != null ? `attempt ${src.attempt}` : null, D.available ? `${files.length} file${files.length === 1 ? '' : 's'} · +${adds} −${dels}` : null,
      finds.length ? `${finds.length} finding${finds.length === 1 ? '' : 's'}, ${pinned.length} on a changed line` : null].filter(Boolean).join(' · ');
    host.append(meta);
    if (D.truncated) host.append(el('p', 'This diff was cut short (over 8 MiB, 300 files or 20,000 lines): what is shown is its beginning.', 'warn'));
    if (!D.available) { host.append(el('p', `No diff to show: ${D.reason || 'not available'}.`, 'muted diff-note')); }
    const byFile = new Map();
    for (const p of finds) if (p.pin && p.pin.file != null && (p.f.off_diff === null || p.f.off_diff === 'line outside the changed hunks')) { if (!byFile.has(p.pin.file)) byFile.set(p.pin.file, []); byFile.get(p.pin.file).push(p); }
    const loose = finds.filter(p => !(p.pin && p.pin.file != null && (p.f.off_diff === null || p.f.off_diff === 'line outside the changed hunks')));

    const body = el('div', undefined, 'diff-body');
    // the file list, with what changed and how many findings point into each
    const nav = el('nav', undefined, 'diff-files'); nav.setAttribute('aria-label', 'Files in this diff');
    const ul = el('ul');
    files.forEach((f, fi) => {
      const li = el('li'), a = el('a', undefined, 'diff-file-link'); a.href = ctx.href({ ...spec, file: pathOf(f) }); a.dataset.fi = String(fi);
      const p = pathOf(f), cut = p.lastIndexOf('/');
      a.append(el('span', cut >= 0 ? p.slice(0, cut + 1) : '', 'diff-dir'), el('span', cut >= 0 ? p.slice(cut + 1) : p, 'diff-base'));
      const stat = el('span', undefined, 'diff-stat'); stat.append(el('span', `+${f.additions}`, 'diff-add'), document.createTextNode(' '), el('span', `−${f.deletions}`, 'diff-del'));
      a.append(stat);
      const ps = byFile.get(fi) || [];
      if (ps.length) { const w = worst(ps.map(x => x.f)); a.append(chip(`${ps.length}`, SEV[w] || 'unknown')); a.title = `${ps.length} finding${ps.length === 1 ? '' : 's'}, worst ${w}`; }
      if (spec.file && spec.file === p) a.setAttribute('aria-current', 'true');
      li.append(a); ul.append(li);
    });
    nav.append(ul);
    if (loose.length) { const a = el('a', `Not on a changed line (${loose.length})`, 'diff-file-link diff-loose-link'); a.href = '#diff-loose'; a.addEventListener('click', e => { e.preventDefault(); host.querySelector('#diff-loose')?.scrollIntoView({ block: 'start' }); }); nav.append(a); }
    const main = el('div', undefined, 'diff-main');
    const state = { drawn: 0 };
    files.forEach((f, fi) => main.append(fileBlock(ctx, D, f, fi, byFile.get(fi) || [], state)));
    if (loose.length) {
      const ls = el('section', undefined, 'diff-loose'); ls.id = 'diff-loose';
      ls.append(el('h3', 'Findings not on a changed line of this diff'));
      for (const p of loose) { const b = findingBox(p.f, `${p.f.location || 'no location'} · ${p.f.off_diff || 'not located'}`); b.id = 'diff-finding-' + p.i; b.tabIndex = -1; ls.append(b); }
      main.append(ls);
    }
    if (list(D.warnings).length) { const d = el('details', undefined, 'diff-warnings'); d.append(el('summary', `Reader warnings (${D.warnings.length})`), el('pre', D.warnings.join('\n'))); main.append(d); }
    if (files.length || loose.length) body.append(nav); else body.classList.add('no-files');
    body.append(main); host.append(body);

    // keys: n/p findings, j/k files; the hash keeps the file a link points at
    const order = [...main.querySelectorAll('.diff-finding[id]')];
    let fIdx = -1;
    const fileEls = [...main.querySelectorAll('.diff-file')];
    const visibleFile = () => { let i = 0; fileEls.forEach((s, k) => { if (s.getBoundingClientRect().top <= 90) i = k; }); return i; };
    host.tabIndex = -1; host.setAttribute('aria-label', 'Diff: n and p move between findings, j and k between files');
    host.onkeydown = e => {
      if (e.altKey || e.ctrlKey || e.metaKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if ((e.key === 'n' || e.key === 'p') && order.length) {
        fIdx = (fIdx + (e.key === 'n' ? 1 : -1) + order.length) % order.length;
        order[fIdx].scrollIntoView({ block: 'center' }); order[fIdx].classList.add('flash'); setTimeout(() => order[fIdx] && order[fIdx].classList.remove('flash'), 900); e.preventDefault();
      } else if ((e.key === 'j' || e.key === 'k') && fileEls.length) {
        const i = Math.max(0, Math.min(fileEls.length - 1, visibleFile() + (e.key === 'j' ? 1 : -1)));
        fileEls[i].scrollIntoView({ block: 'start' }); e.preventDefault();
      }
    };
    if (spec.file) {
      const target = fileEls.find(s => s.dataset.path === spec.file);
      if (target && host.dataset.scrolledTo !== url + '\n' + spec.file) { host.dataset.scrolledTo = url + '\n' + spec.file; requestAnimationFrame(() => target.scrollIntoView({ block: 'start' })); }
    }
  }
  return { render, urlFor, cache };
})();
