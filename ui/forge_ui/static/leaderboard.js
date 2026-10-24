/* forge-ui model leaderboard: how each implementer and reviewer model did, from GET /api/models as aggregated by
   modelstats.js. All API values enter the DOM as text.
   window.ForgeLeaderboard = { render(host, data, ctx) }, where ctx = { runs, prefs, savePrefs(prefs), modelHref(alias), now }
   and prefs = { filter: { project, difficulty, kind, days, exploratoryOnly, minN }, open: [difficulty, ...] }. */
window.ForgeLeaderboard = (() => {
  'use strict';
  const MS = window.ForgeModelStats, RM = window.ForgeRunModel;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const DIFF_LABEL = { low: 'Low', medium: 'Medium', high: 'High', none: 'Difficulty not set' };
  const PERIODS = [[0, 'any time'], [7, 'last 7 days'], [30, 'last 30 days'], [90, 'last 90 days']];
  const MIN_NS = [1, 3, 5, 10, 20, 30, 50];

  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function button(text, cls, onClick, title) {
    const b = el('button', text, cls); b.type = 'button';
    if (title) b.title = title;
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  function svg(tag, attrs, cls) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (cls) n.setAttribute('class', cls);
    return n;
  }
  function ownDur(s) {
    s = num(s); if (s === null) return '—';
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
    return Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm';
  }
  const dur = s => RM && RM.fmtDur ? RM.fmtDur(s) : ownDur(s);
  const pct = p => num(p) === null ? '—' : Math.round(p * 100) + '%';
  const count = n => Number(n || 0).toLocaleString();
  const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many || one + 's'}`;
  const r2 = v => Math.round(v * 100) / 100;
  function compact(n) {
    n = num(n); if (n === null) return '—';
    const a = Math.abs(n), f = (v, s) => (Math.abs(v) >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + s;
    return a >= 999.5e6 ? f(n / 1e9, 'G') : a >= 999.5e3 ? f(n / 1e6, 'M') : a >= 1e3 ? f(n / 1e3, 'k') : String(Math.round(n));
  }
  function readPrefs(p) {
    p = p && typeof p === 'object' ? p : {};
    return { filter: MS.normFilter(p.filter), open: list(p.open).filter(d => MS.DIFFS.includes(d)) };
  }
  const nowOf = ctx => num(typeof ctx.now === 'function' ? ctx.now() : ctx.now) ?? Date.now() / 1000;

  /* ---------- cells ---------- */
  function numCell(v, title) {
    const td = el('td', v, 'lb-num' + (v === 0 ? ' lb-zero' : ''));
    if (title) td.title = title;
    return td;
  }
  function modelCell(alias, ctx, few) {
    const td = el('td', undefined, 'lb-model');
    const href = ctx.modelHref ? ctx.modelHref(alias) : null;
    const plate = window.ForgeIcons ? window.ForgeIcons.modelPlate(alias) : el('span', alias, 'mono');
    if (href) { const a = el('a', undefined, 'lb-alias'); a.href = href; a.title = 'show its runs in the cloud'; a.append(plate); td.append(a); }
    else td.append(plate);
    if (few) { const s = el('span', 'few', 'lb-few'); s.title = few; td.append(s); }
    return td;
  }
  // the 95% Wilson interval on a 0–100% track: the band is the interval, the tick the observed rate
  function ciBar(w) {
    const W = 100, H = 14;
    const s = svg('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, 'aria-hidden': 'true', focusable: 'false' }, 'lb-ci');
    s.append(svg('rect', { x: 0, y: 5, width: W, height: 4, rx: 2 }, 'lb-ci-track'), svg('rect', { x: W / 2 - .5, y: 3, width: 1, height: 8 }, 'lb-ci-grid'));
    if (num(w.lo) !== null) {
      const x0 = w.lo * W, x1 = w.hi * W;
      s.append(svg('rect', { x: r2(Math.min(x0, W - 2)), y: 2, width: r2(Math.max(2, x1 - x0)), height: 10, rx: 2 }, 'lb-ci-band'));
      s.append(svg('rect', { x: r2(clamp(w.p * W - 1, 0, W - 2)), y: 0, width: 2, height: H }, 'lb-ci-tick'));
    }
    return s;
  }
  function rateCell(w) {
    const td = el('td', undefined, 'lb-rate');
    const box = el('div', undefined, 'lb-rate-box'), txt = el('span', undefined, 'lb-rate-text');
    txt.append(el('strong', pct(w.p)), el('span', `${w.k}/${w.n}`, 'muted'));
    box.append(txt, ciBar(w)); td.append(box);
    td.title = w.n ? `${w.k} of ${w.n} first attempts passed (${pct(w.p)}); 95% Wilson interval ${pct(w.lo)}–${pct(w.hi)}` : 'No first attempt passed or failed';
    return td;
  }
  function tokenCell(t, total, unit) {
    const used = t.attempts ?? t.reviews ?? 0;
    if (!used) return numCell('—', 'No tokens recorded');
    const td = el('td', `${compact(t.input)} / ${compact(t.output)}`, 'lb-num');
    if (num(t.coverage) !== null && t.coverage < 1) td.append(el('span', `${pct(t.coverage)} of ${unit}s`, 'lb-sub'));
    td.title = `input ${count(t.input)} (cached ${count(t.cached)}) · output ${count(t.output)} · recorded by ${count(used)} of ${plural(total, unit)}`;
    return td;
  }

  /* ---------- implementers ---------- */
  const IMPL_COLS = [
    ['Model', '', 'The implementer (dwarf) model; its effort levels are counted together'],
    ['Tasks', 'lb-num', 'Tasks whose first attempt this model ran'],
    ['First-attempt pass', 'lb-num', 'First attempts that passed review, over those that passed or failed. Band: 95% Wilson interval; tick: the observed rate'],
    ['Censored', 'lb-num', 'First attempts that said nothing about the implementer (reviewer or forge trouble, no changes, interrupted): in no rate'],
    ['Retries', 'lb-num', 'Attempts this model ran after a task\'s first attempt'],
    ['Escalated away', 'lb-num', 'Times forge escalated a task away from this model'],
    ['Timeouts', 'lb-num', 'Attempts that timed out: the implementer, its reviewer or forge\'s own steps'],
    ['Gate accepts', 'lb-num', 'Attempts that passed although their reviewer said FAIL: the severity gate let them through'],
    ['Human accepts', 'lb-num', 'Tasks a human accepted although no attempt passed, counted for the model of the last attempt'],
    ['Median model time', 'lb-num', 'Median time in the model phase per attempt'],
    ['Tokens in / out', 'lb-num', 'Summed over the attempts that recorded tokens'],
  ];
  const colsOf = (spec, cells) => spec.map(([label, cls, title], i) => ({ ...cells[i], label, cls, title }));
  // a sortable, reorderable table (datatable.js); one set of preferences per kind of table, shared by its copies
  function table(id, label, columns, rows, ctx, rowClass) {
    const host = el('div', undefined, 'lb-dt');
    window.ForgeTable.render(host, { id, label, columns, rows, className: 'lb-table', rowClass,
      prefs: ctx.tablePrefs ? ctx.tablePrefs(id) : null, savePrefs: p => ctx.saveTablePrefs && ctx.saveTablePrefs(id, p) });
    return host;
  }
  function implementerTable(rows, ctx, minN, caption) {
    const tk = r => r.timeoutKinds;
    const columns = colsOf(IMPL_COLS, [
      { id: 'model', value: r => r.alias, cell: r => modelCell(r.alias, ctx, r.small ? `Fewer than ${plural(minN, 'first-attempt outcome')}: too few to rank` : '') },
      { id: 'tasks', value: r => r.tasks, cell: r => numCell(r.tasks) },
      { id: 'pass', value: r => r.first.n ? r.first.lo : null, cell: r => rateCell(r.first) },
      { id: 'censored', value: r => r.first.censored, cell: r => numCell(r.first.censored, Object.entries(r.first.reasons).map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ') || undefined) },
      { id: 'retries', value: r => r.retries, cell: r => numCell(r.retries) },
      { id: 'escalated', value: r => r.escalationsAway, cell: r => numCell(r.escalationsAway) },
      { id: 'timeouts', value: r => r.timeouts, cell: r => numCell(r.timeouts, r.timeouts ? `implementer ${tk(r).implementer} · reviewer ${tk(r).reviewer} · forge ${tk(r).forge}` : undefined) },
      { id: 'gate', value: r => r.gateAccepts, cell: r => numCell(r.gateAccepts) },
      { id: 'human', value: r => r.humanAccepts, cell: r => numCell(r.humanAccepts) },
      { id: 'model-time', value: r => num(r.medianModelS), cell: r => numCell(dur(r.medianModelS), `median wall time per attempt ${dur(r.medianDurationS)}`) },
      { id: 'tokens', value: r => (r.tokens.attempts ?? r.tokens.reviews) ? (r.tokens.input || 0) + (r.tokens.output || 0) : null, cell: r => tokenCell(r.tokens, r.attempts.total, 'attempt') },
    ]);
    columns[2].title += '. Sorts by the lower end of the interval, as the ranking does';
    return table('implementers', caption, columns, rows, ctx, r => r.small ? 'lb-small' : '');
  }

  /* ---------- reviewers ---------- */
  const SEV_TITLE = s => `Confirmed ${s} findings per review (mean over the reviews whose reply could be read)`;
  const REV_COLS = [
    ['Model', '', 'The reviewer (qa) model'],
    ['Primary', 'lb-num', 'Primary reviews of an attempt'],
    ['Shadow', 'lb-num', 'Shadow reviews run beside a primary review'],
    ['Final', 'lb-num', 'Final reviews of a whole run'],
    ['Verdicts', '', 'PASS and FAIL verdicts over every review'],
    ...MS.SEVERITIES.map(s => [s, 'lb-num', SEV_TITLE(s)]),
    ['Shadow agreement', 'lb-num', 'How often this model\'s shadow verdict matched the primary reviewer\'s (both PASS or FAIL)'],
    ['Did not hold', 'lb-num', 'Of the work this model PASSed that was integrated, the share later reverted or with under 80% of its lines kept (forge dataset reviewers, every run in the dataset: the filters do not apply). Advisory: routing never reads it'],
    ['Median time', 'lb-num', 'Median duration of a review'],
    ['Tokens in / out', 'lb-num', 'Summed over the reviews that recorded tokens'],
  ];
  function verdictCell(v) {
    const td = el('td', undefined, 'lb-verdicts'), box = el('div', undefined, 'lb-verdict-box');
    const total = v.PASS + v.FAIL + v.other, W = 80, H = 10;
    const s = svg('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, 'aria-hidden': 'true', focusable: 'false' }, 'lb-vbar');
    s.append(svg('rect', { x: 0, y: 0, width: W, height: H, rx: 2 }, 'lb-vbar-track'));
    let x = 0;
    for (const [k, st] of [['PASS', 'pass'], ['FAIL', 'fail'], ['other', 'none']]) {
      if (!v[k] || !total) continue;
      const w = v[k] / total * W;
      s.append(svg('rect', { x: r2(x), y: 0, width: r2(w), height: H }, 'lb-vbar-seg st-' + st)); x += w;
    }
    const txt = el('span', `${v.PASS} PASS · ${v.FAIL} FAIL${v.other ? ` · ${v.other} other` : ''}`, 'lb-verdict-text');
    box.append(s, txt); td.append(box);
    td.title = total ? `${pct(v.PASS / total)} PASS` : 'No verdicts';
    return td;
  }
  function findingCell(r, s) {
    const avg = r.findings[s];
    if (avg === null) return numCell('—', 'No review reply could be read');
    const td = numCell(avg === 0 ? 0 : avg.toFixed(avg < 10 ? 2 : 1), `${count(r.findingTotals[s])} confirmed ${s} over ${plural(r.findings.n, 'review')}`);
    if (s === 'P0' && avg > 0) td.classList.add('lb-p0');
    return td;
  }
  function agreementCell(a) {
    if (!a.n) return numCell('—', a.unknown ? `${a.unknown} shadow review${a.unknown === 1 ? '' : 's'} without two comparable verdicts` : 'No shadow reviews');
    const td = el('td', undefined, 'lb-num');
    td.append(el('strong', pct(a.p)), document.createTextNode(' '), el('span', `${a.k}/${a.n}`, 'muted'));
    td.title = `${a.k} of ${a.n} shadow verdicts matched the primary's; 95% Wilson interval ${pct(a.lo)}–${pct(a.hi)}${a.unknown ? ` · ${a.unknown} without two comparable verdicts` : ''}`;
    return td;
  }
  function heldCell(h) {
    const days = h.horizon ? ` ${h.horizon} days after integrate` : '';
    if (h.state === 'unavailable') return numCell('—', 'The run dataset could not be read (forge dataset reviewers)');
    if (h.state === 'none') return numCell('—', `No work this model PASSed has a survival outcome${days} yet (forge dataset outcomes records them)`);
    const detail = `${h.reverted} reverted, ${h.rewritten} rewritten, of ${h.n} PASSed and integrated${days}`;
    if (h.state === 'few') {
      const td = numCell('not enough data', `${detail}; a rate needs ${h.need || 'more'} outcomes`);
      td.classList.add('muted');
      return td;
    }
    const td = el('td', undefined, 'lb-num');
    td.append(el('strong', pct(h.p)), document.createTextNode(' '), el('span', `${h.k}/${h.n}`, 'muted'));
    td.title = `${detail}; 95% Wilson interval ${pct(h.lo)}–${pct(h.hi)}`;
    return td;
  }
  function reviewerTable(rows, ctx) {
    const held = r => MS.heldUp(ctx.reviewerOutcomes, r.alias);
    const columns = colsOf(REV_COLS, [
      { id: 'model', value: r => r.alias, cell: r => modelCell(r.alias, ctx, '') },
      { id: 'primary', value: r => r.reviews.primary, cell: r => numCell(r.reviews.primary) },
      { id: 'shadow', value: r => r.reviews.shadow, cell: r => numCell(r.reviews.shadow) },
      { id: 'final', value: r => r.reviews.final, cell: r => numCell(r.reviews.final) },
      { id: 'verdicts', value: r => { const v = r.verdicts, t = v.PASS + v.FAIL + v.other; return t ? v.PASS / t : null; }, cell: r => verdictCell(r.verdicts) },
      ...MS.SEVERITIES.map(sv => ({ id: 'sev-' + sv, value: r => r.findings[sv], cell: r => findingCell(r, sv) })),
      { id: 'agreement', value: r => r.agreement.n ? r.agreement.p : null, cell: r => agreementCell(r.agreement) },
      { id: 'held', value: r => { const h = held(r); return h.state === 'rate' ? h.p : null; }, cell: r => heldCell(held(r)) },
      { id: 'time', value: r => num(r.medianDurationS), cell: r => numCell(dur(r.medianDurationS)) },
      { id: 'tokens', value: r => (r.tokens.attempts ?? r.tokens.reviews) ? (r.tokens.input || 0) + (r.tokens.output || 0) : null, cell: r => tokenCell(r.tokens, r.reviews.total, 'review') },
    ]);
    return table('reviewers', 'Reviewers', columns, rows, ctx);
  }

  /* ---------- header, caveat and filters ---------- */
  function header(L, O, f, update, withNote) {
    const t = L.totals, frag = document.createDocumentFragment();
    const top = el('header', undefined, 'lb-head');
    top.append(el('h1', 'Models', 'module-title'), el('p', `${plural(t.runs, 'run')} · ${plural(t.tasks, 'task')} · ${plural(t.attempts, 'attempt')} · ${plural(t.reviews, 'review')} counted`, 'muted lb-summary'));
    const note = el('div', undefined, 'lb-caveat'); note.setAttribute('role', 'note');
    const p = el('p');
    p.append(el('strong', 'Observational, not causal. '), document.createTextNode('Forge routes each task to a model by its difficulty and by how models did before, so a model sent the hard tasks looks worse here than it is. Exploratory picks, the tasks where forge drew the model at random, are the fairest comparison.'));
    const l = el('label', undefined, 'filter check lb-explore'), c = el('input');
    c.type = 'checkbox'; c.checked = f.exploratoryOnly; c.dataset.key = 'exploratoryOnly';
    c.addEventListener('change', () => update({ exploratoryOnly: c.checked }));
    l.append(c, document.createTextNode(` Exploratory picks only (${plural(O.explored, 'task')})`));
    note.append(p, l);
    frag.append(top);
    if (withNote) frag.append(note);
    return frag;
  }
  function filters(O, L, f, update) {
    const bar = el('form', undefined, 'filters lb-filters'); bar.setAttribute('aria-label', 'Filter the tasks counted');
    bar.addEventListener('submit', e => e.preventDefault());
    const add = (label, key, entries, title, parse = v => v) => {
      const l = el('label', undefined, 'filter'); l.append(el('span', label));
      if (title) l.title = title;
      const s = el('select'); s.dataset.key = key;
      // a saved choice that is no longer offered stays visible, so an empty table has a visible reason
      if (!entries.some(([v]) => String(v) === String(f[key]))) entries.push([f[key], String(f[key])]);
      for (const [v, text, tip] of entries) { const o = el('option', text); o.value = String(v); if (tip) o.title = tip; if (String(v) === String(f[key])) o.selected = true; s.append(o); }
      s.addEventListener('change', () => update({ [key]: parse(s.value) }));
      l.append(s); bar.append(l);
    };
    add('Project', 'project', [['', 'any'], ...O.projects.map(p => [p.repo, p.name, p.repo])]);
    add('Difficulty', 'difficulty', [['', 'any'], ...O.difficulties.map(d => [d.value, `${d.label} (${d.tasks})`])]);
    add('Kind', 'kind', [['', 'any'], ...['plan', 'solo'].map(k => { const o = O.kinds.find(x => x.value === k); return [k, `${k} (${o ? o.tasks : 0})`]; })]);
    add('Period', 'days', PERIODS.map(([d, t]) => [d, t]), 'Tasks of runs created in this period', Number);
    add('Min n', 'minN', MIN_NS.map(n => [n, `${n}+`]), 'Grey the models with fewer first-attempt outcomes than this', Number);
    const reset = button('Reset', '', () => update(MS.emptyFilter()), 'Count every task');
    reset.dataset.key = 'reset';
    bar.append(reset, el('span', `${count(L.totals.tasks)} of ${plural(L.totals.allTasks, 'task')}`, 'muted lb-count'));
    return bar;
  }
  function emptyNote(title, lines, action) {
    const box = el('div', undefined, 'empty lb-empty');
    box.append(el('p', title, 'lb-empty-title'), ...lines.map(t => el('p', t)));
    if (action) box.append(action);
    return box;
  }

  /* ---------- the view ---------- */
  function render(host, data, ctx) {
    ctx = ctx || {};
    const prefs = readPrefs(ctx.prefs), f = prefs.filter, now = nowOf(ctx);
    const active = document.activeElement, focusKey = active && host.contains(active) && active.dataset ? active.dataset.key : null;
    const save = () => { if (ctx.savePrefs) ctx.savePrefs({ filter: { ...f }, open: prefs.open.slice() }); };
    const update = change => {
      const next = { filter: MS.normFilter({ ...f, ...change }), open: prefs.open.slice() };
      if (ctx.savePrefs) ctx.savePrefs(next);
      render(host, data, { ...ctx, prefs: next });
    };
    const O = MS.options(data, ctx.runs), L = MS.leaderboard(data, f, now, ctx.runs);
    const root = el('div', undefined, 'lb');
    const anything = list(data && data.tasks).some(t => t && list(t.attempts).length) || list(data && data.reviews).length > 0;
    if (!anything) {
      root.append(header(L, O, f, update, false), emptyNote('No attempts or reviews to count yet.', [
        'The leaderboard counts the attempts recorded in each task\'s attempts.tsv (a solo run\'s own attempts.tsv) and the reviews that have executions.',
        'Older runs may lack them, and a task that has not started has none.']));
      host.replaceChildren(root);
      return;
    }
    root.append(header(L, O, f, update, true), filters(O, L, f, update));
    if (L.caveats.length) { const ul = el('ul', undefined, 'lb-notes'); for (const c of L.caveats) ul.append(el('li', c)); root.append(ul); }
    if (!L.totals.tasks && !L.totals.reviews) {
      const reset = button('Reset the filters', '', () => update(MS.emptyFilter())); reset.dataset.key = 'reset-empty';
      root.append(emptyNote('No tasks or reviews match these filters.', [], reset));
    } else {
      const impl = el('section', undefined, 'lb-section');
      impl.append(el('h2', 'Implementers', 'plate'), el('p', 'Each task counts once, for the model that ran its first attempt; retries, escalations and accepts show what happened after. Ranked by the lower end of the 95% interval, so a high rate on few tasks does not jump the table.', 'lb-lead'));
      impl.append(L.implementers.length ? implementerTable(L.implementers, ctx, f.minN, 'Implementers') : el('p', 'No attempts with a recorded model match these filters.', 'muted'));
      root.append(impl);
      if (!f.difficulty && L.implementers.length) {
        const by = el('section', undefined, 'lb-section');
        by.append(el('h2', 'By difficulty', 'plate'), el('p', 'The same table for the tasks of one declared difficulty: the comparison routing distorts least.', 'lb-lead'));
        for (const d of MS.DIFFS) {
          const rows = L.byDifficulty[d];
          if (!rows.length) continue;
          const k = rows.reduce((s, r) => s + r.first.k, 0), n = rows.reduce((s, r) => s + r.first.n, 0), tasks = rows.reduce((s, r) => s + r.tasks, 0);
          const det = el('details', undefined, 'lb-diff'); det.open = prefs.open.includes(d);
          const sum = el('summary');
          sum.append(el('span', DIFF_LABEL[d], 'lb-diff-name'), el('span', `${plural(tasks, 'task')} · ${plural(rows.length, 'model')} · first-attempt pass ${n ? `${pct(k / n)} (${k}/${n})` : '—'}`, 'muted'));
          det.append(sum, implementerTable(rows, ctx, f.minN, `Implementers, ${DIFF_LABEL[d].toLowerCase()}`));
          det.addEventListener('toggle', () => {
            if (det.open === prefs.open.includes(d)) return;
            prefs.open = det.open ? prefs.open.concat(d) : prefs.open.filter(x => x !== d); save();
          });
          by.append(det);
        }
        root.append(by);
      }
      const rev = el('section', undefined, 'lb-section');
      rev.append(el('h2', 'Reviewers', 'plate'), el('p', 'Primary reviews of each attempt, shadow reviews run beside them and final reviews of a whole run. Findings are confirmed findings per review.', 'lb-lead'));
      rev.append(L.reviewers.length ? reviewerTable(L.reviewers, ctx) : el('p', 'No reviews with a recorded model match these filters.', 'muted'));
      root.append(rev);
    }
    host.replaceChildren(root);
    // keep the keyboard on the control that changed (a vanished one hands over to Reset)
    if (focusKey) {
      const keyed = [...root.querySelectorAll('[data-key]')], n = keyed.find(x => x.dataset.key === focusKey) || keyed.find(x => x.dataset.key === 'reset');
      if (n) n.focus({ preventScroll: true });
    }
  }

  return { render };
})();
