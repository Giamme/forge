/* forge-ui inspector: what a selected node is, which model owned it, what prompt launched it, how long it
   took and what its reviewer found. Defines window.ForgePanel.render(host, ctx). All API values enter the
   DOM as text. Large texts (prompts, reports) come from the lazy endpoints and are fetched once per node. */
window.ForgePanel = (() => {
  'use strict';
  const RM = window.ForgeRunModel;
  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const display = v => v === null || v === undefined || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
  const STATUSES = new Set(['merged', 'pass', 'running', 'pending', 'fail', 'error', 'infra', 'timeout', 'unknown', 'invalidated', 'blocked',
    'interrupted', 'nochanges', 'conflict', 'accepted', 'none']);
  const cache = new Map();                                  // path → {data, error, at}
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = display(text);
    if (className) node.className = className;
    return node;
  }
  function chip(value, cls) {
    const s = String(value || 'unknown').toLowerCase();
    return el('span', value || '—', 'chip st-' + (cls || (STATUSES.has(s) ? s : 'unknown')));
  }
  // a finding's severity: its own shape and ramp (style.css .sev-P0…P3), never a status lamp
  const plate = spec => window.ForgeIcons ? window.ForgeIcons.modelPlate(spec) : el('span', spec || '—', 'mono');
  const plates = specs => { const s = el('span', undefined, 'plates'); for (const x of specs) s.append(plate(x)); return specs.length ? s : undefined; };
  // a long path cut in the middle: its head gives way, its last part always shows; copying takes all of it
  function midPath(v) {
    const s = String(v || ''), i = s.lastIndexOf('/', s.length - 2);
    if (!s) return undefined;
    const out = el('span', undefined, 'mid-path'); out.title = s;
    out.append(el('span', i > 0 ? s.slice(0, i + 1) : '', 'mid-head'), el('span', i > 0 ? s.slice(i + 1) : s, 'mid-tail'));
    return out;
  }
  const sevChip = (text, sev) => el('span', text, 'chip sev-chip sev-' + (/^P[0-3]$/.test(sev) ? sev : 'none'));
  function time(v) {
    if (num(v) === null) return '—';
    const d = new Date(v * 1000); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
  }
  function fields(parent, entries) {
    const dl = el('dl', undefined, 'fields');
    for (const [label, value, mono] of entries) {
      if (value === undefined) continue;
      dl.append(el('dt', label));
      const dd = el('dd', undefined, mono ? 'mono' : '');
      dd.append(value instanceof Node ? value : document.createTextNode(display(value)));
      dl.append(dd);
    }
    parent.append(dl); return dl;
  }
  function section(parent, title) { const s = el('section', undefined, 'insp-section'); s.append(el('h3', title)); parent.append(s); return s; }
  function details(parent, title, value, key, { open = false, cls = '' } = {}) {
    const d = el('details', undefined, cls); d.dataset.keep = key; if (open) d.open = true;
    const pre = el('pre', value); pre.dataset.scroll = key + '-text';
    d.append(el('summary', title), pre); parent.append(d); return d;
  }
  function link(text, hash, onClick) {
    const a = el('a', text); a.href = hash;
    if (onClick) a.addEventListener('click', e => { e.preventDefault(); onClick(); });
    return a;
  }
  function muted(parent, text) { parent.append(el('p', text, 'muted')); }
  // a link into the diff viewer (ctx.diffHash is absent where there is no viewer)
  function diffLink(ctx, text, spec, cls = 'diff-link') { if (!ctx.diffHash) return null; const a = el('a', text, cls); a.href = ctx.diffHash(spec); return a; }
  // a lazy resource: returns cached data, or starts the fetch and re-renders when it lands
  function lazy(ctx, path, fresh) {
    const c = cache.get(path);
    if (c && (c.fresh === fresh || c.pending)) return c;
    const entry = { pending: true, fresh, data: c && c.data || null, error: null };
    cache.set(path, entry);
    ctx.fetchJSON(path).then(data => { entry.data = data; entry.error = null; }, err => { entry.error = err; })
      .finally(() => { entry.pending = false; ctx.rerender(); });
    return entry;
  }
  function loading(parent, entry, what) {
    if (entry.error) {
      parent.append(el('p', entry.error.status === 404 ? `${what} is not available from this server. If forge-ui was started before this version, run \`forge ui open\` again to restart it.` : `Could not load ${what}: ${entry.error.message}`, 'muted'));
      return true;
    }
    if (!entry.data) { parent.append(el('p', `Loading ${what}…`, 'muted')); return true; }
    return false;
  }

  /* ---------- findings: P0–P3, confirmed or plausible, with where and what ---------- */
  // FindingsSummary from a list of Findings (the lazy endpoints) or as given (the summaries)
  function countsOf(f) {
    if (!Array.isArray(f)) return f || null;
    const c = { P0: 0, P1: 0, P2: 0, P3: 0, unlabelled: 0, plausible: 0, confirmed: 0, worst: null };
    for (const x of f) {
      if (x.status === 'PLAUSIBLE') { c.plausible++; continue; }
      if (x.status === 'UNSTATED' || !x.severity) { c.unlabelled++; if (x.status === 'CONFIRMED') c.confirmed++; continue; }
      c.confirmed++; c[x.severity]++;
    }
    c.worst = ['P0', 'P1', 'P2', 'P3'].find(k => c[k]) || null;
    return c;
  }
  function findingCounts(parent, f) {
    f = countsOf(f);
    const row = el('div', undefined, 'chips');
    if (!f) { row.append(el('span', 'no findings parsed', 'muted')); parent.append(row); return; }
    let any = false;
    for (const sev of ['P0', 'P1', 'P2', 'P3']) if (f[sev]) { row.append(sevChip(`${sev} × ${f[sev]}`, sev)); any = true; }
    if (f.unlabelled) { row.append(chip(`unlabelled × ${f.unlabelled}`, 'unknown')); any = true; }
    if (f.plausible) { row.append(chip(`plausible × ${f.plausible}`, 'pending')); any = true; }
    if (!any) row.append(el('span', 'no confirmed findings', 'muted'));
    parent.append(row);
  }
  /* ---------- fail-first (opt-in --fail-first): did the change's new tests fail without it? ---------- */
  const FAIL_FIRST = { proven: ['fails without the change', 'pass'], passes_without_change: ['passes without the change: proves nothing', 'fail'],
    inconclusive: ['fails even with the change: no evidence', 'unknown'], error: ['could not run', 'unknown'] };
  function failFirst(parent, ff) {
    if (!ff) return;
    const box = el('div', undefined, 'review');
    const h = el('div', undefined, 'attempt-head');
    h.append(el('strong', 'fail-first evidence'));
    const weak = (ff.counts && ff.counts.passes_without_change) || 0;
    if (weak) h.append(chip(`${weak} pass without the change`, 'fail'));
    box.append(h);
    if (ff.status !== 'ran') { muted(box, ff.reason || `Fail-first did not run (${ff.status}).`); parent.append(box); return; }
    if (ff.command) box.append(el('p', ff.command, 'mono muted'));
    const ul = el('ul', undefined, 'findings');
    for (const t of list(ff.tests)) {
      const [label, cls] = FAIL_FIRST[t.result] || [t.result, 'unknown'];
      const li = el('li');
      li.append(el('span', t.path, 'mono'), document.createTextNode(' '), chip(label, cls));
      ul.append(li);
    }
    box.append(ul);
    parent.append(box);
  }
  function findingsList(parent, findings, key) {
    if (findings === null || findings === undefined) { muted(parent, 'Findings could not be parsed (forge-contract.py was not available to the server).'); return; }
    if (!findings.length) { muted(parent, 'The reviewer reported no findings in the expected `- [P1][CONFIRMED] file:line — …` form.'); return; }
    const order = f => (f.severity ? Number(f.severity.slice(1)) : 4) + (f.status === 'CONFIRMED' ? 0 : .5);
    const ul = el('ul', undefined, 'findings');
    findings.slice().sort((a, b) => order(a) - order(b)).forEach((f, i) => {
      const li = el('li', undefined, 'finding' + (f.status === 'CONFIRMED' ? '' : ' plausible'));
      const head = el('div', undefined, 'finding-head');
      head.append(sevChip(f.severity || 'P?', f.severity), el('span', f.status, 'tag'));
      if (f.location) head.append(el('code', f.location));
      li.append(head, el('p', f.summary || ''));
      if (f.text && f.text !== f.summary) { const d = el('details'); d.dataset.keep = `${key}-f${i}`; d.append(el('summary', 'Full finding'), el('pre', f.text)); li.append(d); }
      ul.append(li);
    });
    parent.append(ul);
  }
  function gateLine(parent, gate) {
    if (!gate) return;
    fields(parent, [['Blocking threshold', gate.threshold], ['Gate decision', gate.decision === 'accept' ? chip('accept', 'pass') : chip(gate.decision || '—', 'unknown')],
      ['Worst confirmed', gate.worst], ['Confirmed / unlabelled / tolerated', `${display(gate.n_confirmed)} / ${display(gate.n_unlabelled)} / ${display(gate.n_below)}`]]);
  }

  /* ---------- timings ---------- */
  function phaseBar(parent, phases, total) {
    const entries = Object.entries(phases || {}).filter(([, v]) => num(v) !== null && v > 0);
    const sum = entries.reduce((a, [, v]) => a + v, 0) || num(total) || 0;
    if (!entries.length || !sum) return;
    const bar = el('div', undefined, 'phase-bar'); bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', entries.map(([k, v]) => `${k} ${RM.fmtDur(v)}`).join(', '));
    const legend = el('div', undefined, 'phase-legend');
    for (const [k, v] of entries) {
      const seg = el('span', undefined, 'phase ph-' + k.replace(/[^a-z]/g, ''));
      seg.style.flexGrow = String(Math.max(.02, v / sum));   // CSSOM, allowed by the page CSP
      seg.title = `${k}: ${RM.fmtDur(v)}`;
      bar.append(seg);
      const item = el('span', undefined, 'phase-key'); item.append(el('i', undefined, 'ph-' + k.replace(/[^a-z]/g, '')), document.createTextNode(`${k} ${RM.fmtDur(v)}`));
      legend.append(item);
    }
    parent.append(bar, legend);
  }
  const tokens = t => t ? `${display(t.input)} in (${display(t.cached)} cached) · ${display(t.output)} out` : '—';
  function modelFields(m) {
    if (!m) return [['Model', '—']];
    return [['Spec', m.spec ? plate(m.spec) : undefined], ['Harness', m.harness, true], ['Model', m.model, true], ['Effort', m.effort], m.clamped ? ['Clamped', m.clamped] : ['Clamped', undefined]];
  }

  /* ---------- views ---------- */
  // the run's record: what it was asked, how it was set up, and what verification printed (the header names and counts it)
  function runRecord(host, ctx) {
    const run = ctx.run, solo = run.kind === 'solo';
    const s = section(host, 'Settings');
    fields(s, [['Repo', midPath(run.repo), true], ['Run ID', run.run_id, true], ['Path', midPath(run.path), true], ['Source', run.source],
      ['Decompose', run.decompose_level], ['Updated', time(run.updated)], ['Explore', run.settings && run.settings.explore], ['Shadow rate', run.settings && run.settings.shadow_rate],
      ['Shadow reviewers', run.settings && plates(list(run.settings.shadow_qa))], ['QA threshold', run.settings && run.settings.qa_threshold],
      ['Max parallel', run.settings && run.settings.max_parallel], ['Final reviewer', run.settings && run.settings.final_qa ? plate(run.settings.final_qa) : undefined],
      ['Timeout (seconds)', run.settings && run.settings.timeout], ['Verification', chip((run.verification && run.verification.status) || run.verification_status)],
      ['Command', run.verification && run.verification.command, true]]);
    details(host, 'Request', display(run.request), 'run-request');
    if (!solo) details(host, 'Waves', list(run.waves).map((ids, i) => `${i + 1}: ${list(ids).join(', ')}`).join('\n') || '—', 'waves');
    if (run.verification && run.verification.log_tail != null) details(host, 'Verification output', run.verification.log_tail, 'verification-output');
  }
  function runView(host, ctx) {
    const run = ctx.run, now = ctx.now;
    const tr = RM.timeRange(run, now);
    const s = section(host, 'Timeline');
    fields(s, [['Started', time(tr && tr.start)], ['Last activity', time(tr && tr.end)], ['Wall time', tr ? RM.fmtDur(tr.end - tr.start) : '—'],
      ['Concurrency limit', run.settings && run.settings.max_parallel != null ? run.settings.max_parallel : (run.kind === 'solo' ? '—' : '3 (default; not recorded)')]]);
    if (run.kind !== 'solo') {
      const cp = RM.criticalPath(run, { now });
      if (cp.ids.length) {
        const p = el('p', undefined, 'crit');
        p.append(document.createTextNode(cp.mode === 'exec' ? 'Critical path (what bounded the wall time): ' : 'Heaviest chain by difficulty: '));
        cp.ids.forEach((id, i) => { if (i) p.append(document.createTextNode(' → ')); p.append(link(id, ctx.hash({ kind: 'task', id }), () => ctx.onSelect({ kind: 'task', id }))); });
        s.append(p);
        for (const w of cp.waits) muted(s, `${w.to} waited ${RM.fmtDur(w.seconds)} after ${w.from} merged.`);
      }
      const c = RM.concurrency(run, { now });
      if (c.series.length) muted(s, `Peak concurrency: ${c.peak} at once (limit ${c.cap}).`);
    }
    const f = section(host, 'Finish');
    const F = RM.finishState(run);
    if (run.kind === 'solo') fields(f, [['Verdict', chip(list(run.tasks)[0] && list(run.tasks)[0].verdict)]]);
    else {
      fields(f, [['Integration', link(`${F.integration.merged}/${F.integration.total} merged`, ctx.hash({ kind: 'integration' }), () => ctx.onSelect({ kind: 'integration' }))],
        ['Verification', link(F.verification.status, ctx.hash({ kind: 'verification' }), () => ctx.onSelect({ kind: 'verification' }))],
        ['Final review', link(F.final.status === 'NONE' ? 'not requested' : F.final.status, ctx.hash({ kind: 'final' }), () => ctx.onSelect({ kind: 'final' }))]]);
      if (F.final.findings) findingCounts(f, F.final.findings);
    }
    if (list(run.deferrals).length) details(host, `Deferred tasks (${run.deferrals.length})`, run.deferrals.join('\n'), 'deferrals');
    runRecord(host, ctx);
    muted(host, 'Select a task, an attempt or a finish node on the map or in the table to see its prompts, models, timings and findings.');
  }

  function taskView(host, ctx, task) {
    const run = ctx.run, now = ctx.now, solo = run.kind === 'solo';
    const head = el('div', undefined, 'insp-head');
    head.append(el('h2', solo ? (task.title || 'Solo run') : `${task.id} · ${task.title || 'Untitled task'}`), el('div', solo ? 'solo task' : `task · wave ${display(task.wave)}`, 'insp-kind'));
    const chips = el('div', undefined, 'chips');
    chips.append(chip(task.status));
    if (task.verdict) chips.append(chip('review ' + task.verdict, task.verdict.toLowerCase()));
    if (task.explored) chips.append(el('span', '* explored', 'tag'));
    if (task.accepted_by) chips.append(chip('accepted over FAIL', 'nochanges'));
    head.append(chips);
    const dl = diffLink(ctx, solo ? 'View the diff →' : 'View its diff, findings pinned to their lines →', { kind: 'task', task: task.id, source: 'current' }); if (dl) head.append(dl);
    host.append(head);
    if (!solo) fields(host, [['Difficulty', task.difficulty], ['Depends on', depLinks(task.deps, ctx)], ['Files', list(task.files).join('\n') || '—', true], ['Branch', task.branch, true]]);

    // the models: who owned it, how it was routed, what it was escalated to
    const ms = section(host, 'Models');
    fields(ms, [['Dwarf', plate(task.dwarf)], ['Reviewer', plate(task.qa)], ['Attempt', task.attempt]]);
    const route = list(task.route).slice(-1)[0];
    if (route) fields(ms, [['Routed', `${display(route.chosen)} from ${list(route.candidates).join(', ') || '—'}${route.explored ? ` (exploratory, p=${display(route.propensity)})` : ''}`, true], ['Tier · ε', `${display(route.tier)} · ${display(route.epsilon)}`]]);
    for (const e of list(task.escalations)) muted(ms, `Escalated after attempt ${display(e.attempt)}: ${display(e.from)} → ${display(e.to)} at ${time(e.at)}`);

    // timings
    const tt = RM.taskTimes(task, run, now);
    const ts = section(host, 'Timings');
    fields(ts, [['First start', time(tt.first)], ['Last activity', time(tt.last)], ['Busy', RM.fmtDur(tt.busy)], ['Model time', RM.fmtDur(tt.model)],
      ['Overhead', RM.fmtDur(tt.overhead)], ['Queue wait', tt.wait === null ? '—' : RM.fmtDur(tt.wait)], ['Merged', time(task.merged_at)],
      ['Tokens', tt.tokens.input || tt.tokens.output ? `${tt.tokens.input} in (${tt.tokens.cached} cached) · ${tt.tokens.output} out` : '—']]);

    // attempts, each with its dwarf and its review
    const as = section(host, 'Attempts');
    const tp = RM.taskParts(task);
    if (!list(task.executions).length) muted(as, 'No executions recorded yet' + (task.status === 'PENDING' ? ': this task has not started.' : '.'));
    for (const z of tp.zones) {
      const box = el('div', undefined, 'attempt');
      const row = list(task.attempts).filter(a => a.attempt === z.attempt).pop();
      const h = el('div', undefined, 'attempt-head'); h.append(el('strong', z.name.toLowerCase())); if (row) h.append(chip(row.status), el('span', time(row.at), 'muted'));
      box.append(h);
      for (const pid of z.parts) {
        const p = tp.parts.find(x => x.id === pid); if (!p || p.planned) continue;
        const line = el('div', undefined, 'exec-line');
        const e = p.exec && list(task.executions).find(x => x.id === p.exec.id) || null;
        const label = p.label.toLowerCase();
        const target = p.exec && p.exec.id ? { kind: 'execution', task: task.id, id: task.id, exec: p.exec.id, shadow: p.kind === 'shadow' } : null;
        line.append(target ? link(label, ctx.hash(target), () => ctx.onSelect(target)) : el('span', label), plate(p.spec), chip(p.status));
        if (e) line.append(el('span', `${RM.fmtDur(e.duration_s ?? (e.state === 'running' && num(e.started) !== null ? now - e.started : null))}${e.state === 'running' && e.phase ? ' · ' + e.phase : ''}`, 'muted'));
        if (p.kind === 'shadow' && p.shadow) line.append(el('span', p.shadow.agreement === 'disagree' ? 'disagrees with the primary review' : p.shadow.agreement === 'agree' ? 'agrees' : '', p.shadow.agreement === 'disagree' ? 'warn' : 'muted'));
        const fs = (e && e.findings) || (p.shadow && p.shadow.findings);
        if (fs && fs.worst) line.append(sevChip(`${fs.worst}×${fs[fs.worst]}`, fs.worst));
        // what it said and ran, and the diff a review read
        const go = el('span', undefined, 'exec-links');
        if (target && ctx.convoHash) { const a = el('a', 'conversation', 'diff-link small'); a.href = ctx.convoHash(target); go.append(a); }
        if ((p.kind === 'qa' || p.kind === 'shadow') && p.exec && p.exec.id) { const a = diffLink(ctx, 'diff it read', { kind: 'task', task: task.id, source: p.exec.id }, 'diff-link small'); if (a) go.append(a); }
        if (go.childElementCount) line.append(go);
        box.append(line);
      }
      for (const l of tp.links.filter(l => l.kind === 'escalation' && z.parts.includes(l.to))) box.append(el('p', `escalated: ${l.label}`, 'muted'));
      as.append(box);
    }

    if (run.fractal && run.fractal.enabled) fractalSection(host, ctx, task);

    // reviews and their findings
    const rs = section(host, 'Review findings');
    gateLine(rs, task.gate);
    findingCounts(rs, task.findings);
    failFirst(rs, task.fail_first);
    const detail = lazy(ctx, `/api/runs/${encodeURIComponent(run.key)}/tasks/${encodeURIComponent(task.id)}`, run.updated);
    if (!loading(rs, detail, 'the full findings')) {
      const reviews = list(detail.data.reviews);
      if (!reviews.length) muted(rs, 'No review has run yet.');
      reviews.forEach((r, i) => {
        const box = el('div', undefined, 'review');
        const h = el('div', undefined, 'attempt-head');
        h.append(el('strong', `${r.kind} review · attempt ${display(r.attempt)}`), plate(r.spec), chip(r.verdict || r.state || 'unknown'));
        box.append(h);
        if (r.state === 'running') muted(box, 'Still reviewing; findings appear when it finishes.');
        else findingsList(box, r.findings, `task-${task.id}-r${i}`);
        rs.append(box);
      });
    }
    // Clef: what was predicted, against what happened
    const clef = task.clef || {};
    const preds = [];
    if (clef.first_pass) preds.push(['First attempt', clef.first_pass]);
    for (const [a, p] of Object.entries(clef.retry_pass || {})) preds.push([`Retry · attempt ${a}`, p]);
    if (preds.length) {
      const cs = section(host, 'Clef predictions');
      for (const [label, p] of preds) {
        const n = Number(String(label).match(/\d+/) || 1), outcome = list(task.attempts).filter(a => a.attempt === n).pop();
        fields(cs, [[label, `p(pass) = ${display(p.p)} → ${outcome ? outcome.status : 'not finished'}`], ['Assignment', p.assignment ? plates([p.assignment.dwarf, p.assignment.qa]) : '—']]);
      }
    }
    // the prompts and the final messages
    const ps = section(host, 'Prompts and output');
    if (!detail.data) details(ps, 'Requirements (prompt.md)', task.prompt, `task-${task.id}-prompt`);
    else {
      const t = detail.data.texts || {};
      for (const [title, key] of [['Requirements (prompt.md)', 'prompt'], ['Intended approach', 'approach'], ['Retry findings', 'retry_findings'],
        ['Dwarf prompt as assembled (dwarf.input)', 'dwarf_input'], ['Review prompt as assembled (qa.input)', 'qa_input'], ['Known issues', 'known_issues'], ['Accepted by', 'accepted_by']]) {
        if (t[key] != null) details(ps, title, t[key], `task-${task.id}-${key}`);
      }
    }
    if (task.dwarf_tail != null) details(ps, 'Dwarf final message', task.dwarf_tail, `task-${task.id}-dwarf_tail`);
    if (task.qa_tail != null) details(ps, 'Reviewer final message', task.qa_tail, `task-${task.id}-qa_tail`);
  }

  function execView(host, ctx, task, sel) {
    const run = ctx.run, now = ctx.now;
    const brief = list(task && task.executions).find(e => e.id === sel.exec) || list(run.timeline && run.timeline.verifications).find(e => e.id === sel.exec) || null;
    const entry = lazy(ctx, `/api/runs/${encodeURIComponent(run.key)}/executions/${encodeURIComponent(sel.exec)}`, brief && brief.state === 'done' ? 'done' : run.updated);
    const e = entry.data || brief;
    const head = el('div', undefined, 'insp-head');
    const role = e ? e.role : String(sel.exec).split('-')[0];
    head.append(el('h2', `${task ? task.id + ' · ' : ''}${role === 'qa' ? 'reviewer' : role === 'dwarf' ? 'dwarf' : role}`), el('div', `${sel.shadow ? 'shadow ' : ''}${role === 'qa' ? 'review' : role} · attempt ${display(e && e.attempt)}`, 'insp-kind'));
    const chips = el('div', undefined, 'chips');
    if (e) { chips.append(chip(RM.execStatus(e))); if (e.verdict) chips.append(chip('verdict ' + e.verdict, e.verdict.toLowerCase())); }
    head.append(chips);
    if (task) head.append(link('← task ' + task.id, ctx.hash({ kind: 'task', id: task.id }), () => ctx.onSelect({ kind: 'task', id: task.id })));
    if ((role === 'qa' || role === 'dwarf') && ctx.convoHash) { const a = el('a', 'Its conversation: what it said and ran →', 'diff-link'); a.href = ctx.convoHash(sel); head.append(a); }
    if (task && (role === 'qa' || role === 'dwarf')) { const a = diffLink(ctx, role === 'qa' ? 'The diff this review read →' : 'The diff its review read →', { kind: 'task', task: task.id, source: sel.exec }); if (a) head.append(a); }
    host.append(head);
    if (!e) { loading(host, entry, 'this execution'); return; }
    const ms = section(host, 'Model');
    fields(ms, modelFields(e.model));
    const ts = section(host, 'Timings');
    const running = e.state === 'running';
    fields(ts, [['Started', time(e.started)], ['Ended', running ? 'running' : time(e.ended)], ['Duration', RM.fmtDur(e.duration_s ?? (running && num(e.started) !== null ? now - e.started : null))],
      ['Phase', running ? `${display(e.phase)}${num(e.phase_started) !== null ? ' for ' + RM.fmtDur(now - e.phase_started) : ''}` : e.phase], ['Tokens', tokens(e.tokens)],
      ['Prompt size', e.prompt_bytes != null ? `${e.prompt_bytes} bytes` : '—'], ['Exit', e.exit_code], ['Infra', e.infra_class], ['Timed out', e.timed_out ? 'yes' : undefined]]);
    phaseBar(ts, e.phases, e.duration_s);
    if (role === 'qa') {
      const rs = section(host, 'Findings');
      if (running) muted(rs, 'Still reviewing; findings appear when it finishes.');
      else {
        findingCounts(rs, entry.data ? entry.data.findings : e.findings);
        if (entry.data) findingsList(rs, entry.data.findings, `exec-${sel.exec}`); else loading(rs, entry, 'the findings');
      }
    }
    const ps = section(host, 'Prompt and output');
    if (loading(ps, entry, 'the prompt')) return;
    const d = entry.data;
    details(ps, 'Prompt that launched it', d.prompt, `exec-${sel.exec}-prompt`, { open: false });
    details(ps, 'Final message', d.last, `exec-${sel.exec}-last`);
    if (d.resolved) details(ps, 'Resolved dispatch settings', Object.entries(d.resolved).map(([k, v]) => `${k}=${v}`).join('\n'), `exec-${sel.exec}-resolved`);
    if (list(d.warnings).length) details(ps, `Reader warnings (${d.warnings.length})`, d.warnings.join('\n'), `exec-${sel.exec}-warn`);
  }

  /* ---------- Fractal: the child tasks a task was split into ---------- */
  const money = v => num(v) === null ? '—' : '$' + (v < 1 ? v.toFixed(3) : v.toFixed(2));
  const fractalPath = (run, task) => `/api/runs/${encodeURIComponent(run.key)}/tasks/${encodeURIComponent(task)}/fractal`;
  function fractalSection(host, ctx, task) {
    const run = ctx.run, fs = section(host, 'Fractal tree');
    if (!run.fractal.available) { muted(fs, `Fractal ran this run, but its state cannot be read: ${run.fractal.reason || 'unavailable'}.`); return; }
    const entry = lazy(ctx, fractalPath(run, task.id), run.updated);
    if (loading(fs, entry, 'the Fractal tree')) return;
    const F = entry.data;
    if (ctx.onFractal) ctx.onFractal(task.id, F.available ? F : null);
    if (!F.available) { muted(fs, F.reason || 'No Fractal state for this task.'); return; }
    const st = F.state || {}, out = F.outcome || {};
    const chips = el('div', undefined, 'chips');
    if (st.status) chips.append(chip(st.status === 'active' ? 'RUNNING' : st.status, st.status === 'active' ? 'running' : undefined));
    if (out.acceptance || st.acceptance) chips.append(chip('acceptance ' + (out.acceptance || st.acceptance), String(out.acceptance || '').toLowerCase() === 'pass' ? 'pass' : 'none'));
    if (out.verification) chips.append(chip('verification ' + out.verification, out.verification === 'passed' ? 'pass' : 'none'));
    fs.append(chips);
    const nodes = list(F.nodes), cost = nodes.reduce((a, n) => a + (num(n.cost) || 0), 0);
    muted(fs, `${nodes.length} node${nodes.length === 1 ? '' : 's'} · ${RM.fmtDur(st.elapsed)} · ${money(cost)} so far · click a node for its goal, decision and output`);
    const T = RM.fractalTree(nodes), ul = el('ul', undefined, 'fractal-tree');
    for (const n of nodes) {
      const li = el('li', undefined, 'ft-node ft-d' + Math.min(6, (T.pos[n.id] || {}).depth || 0) + (n.reserved ? ' reserved' : ''));
      li.append(el('i', undefined, 'led st-' + String(n.ui_status || 'unknown').toLowerCase()));
      const target = { kind: 'fractal', task: task.id, node: n.id };
      li.append(link(n.id, ctx.hash(target), () => ctx.onSelect(target)));
      const bits = [n.reserved ? 'reserved' : n.status, n.difficulty, n.phase ? `${n.phase}${n.iteration ? ' #' + n.iteration : ''}` : null, num(n.cost) !== null ? money(n.cost) : null].filter(Boolean);
      if (n.model) li.append(plate(n.model));
      li.append(el('span', bits.join(' · '), 'muted'));
      const dec = n.decomposition;
      if (dec && dec.decision) li.append(el('span', dec.decision === 'split' ? `split into ${list(dec.children).length}` : dec.decision, 'tag'));
      if (n.has_diff) { const a = diffLink(ctx, 'diff', { kind: 'fractal', task: task.id, node: n.id }, 'diff-link small'); if (a) li.append(a); }
      if (n.goal) li.title = n.goal;
      ul.append(li);
    }
    fs.append(ul);
    if (list(F.events).length) details(fs, `Recent events (${F.events.length})`, F.events.map(e => `${time(e.at)}  ${e.kind}${e.node ? ' · ' + e.node : ''}${e.decision ? ' · ' + e.decision : ''}${e.status ? ' · ' + e.status : ''}${e.reason ? ' — ' + e.reason : ''}`).join('\n'), `fractal-${task.id}-events`);
    if (list(F.warnings).length) details(fs, `Reader warnings (${F.warnings.length})`, F.warnings.join('\n'), `fractal-${task.id}-warn`);
  }
  function fractalNodeView(host, ctx, sel) {
    const run = ctx.run, path = `${fractalPath(run, sel.task)}/nodes/${encodeURIComponent(sel.node)}`;
    const entry = lazy(ctx, path, run.updated), n = entry.data;
    const head = el('div', undefined, 'insp-head');
    head.append(el('h2', sel.node), el('div', `Fractal node · task ${sel.task}`, 'insp-kind'));
    const back = { kind: 'task', id: sel.task };
    if (n) { const chips = el('div', undefined, 'chips'); chips.append(chip(n.reserved ? 'reserved' : n.ui_status || n.status, n.reserved ? 'none' : undefined)); if (n.decomposition && n.decomposition.decision) chips.append(el('span', n.decomposition.decision, 'tag')); head.append(chips); }
    head.append(link('← task ' + sel.task, ctx.hash(back), () => ctx.onSelect(back)));
    if (n && n.has_diff) { const a = diffLink(ctx, 'Its candidate diff →', { kind: 'fractal', task: sel.task, node: sel.node }); if (a) head.append(a); }
    host.append(head);
    if (loading(host, entry, 'this node')) return;
    if (n.goal) host.append(el('p', n.goal));
    fields(host, [['Parent', n.parent ? link(n.parent, ctx.hash({ kind: 'fractal', task: sel.task, node: n.parent }), () => ctx.onSelect({ kind: 'fractal', task: sel.task, node: n.parent })) : '—'],
      ['Difficulty', n.difficulty], ['Model', n.model ? plate(n.model) : '—'], ['Resolved', n.resolved_model ? plate(n.resolved_model) : '—'], ['Paths', list(n.paths).join('\n') || '—', true], ['Depends on', list(n.deps).join(', ') || '—', true],
      ['Phase', n.phase ? `${n.phase}${n.iteration ? ' · iteration ' + n.iteration : ''}` : '—'], ['Started', time(n.started_at)], ['Ended', time(n.ended_at)],
      ['Cost', `${money(n.cost)}${n.unknown_cost_steps ? ` (+${n.unknown_cost_steps} steps of unknown cost)` : ''}`], ['Tokens', n.tokens ? `${display(n.tokens.input_tokens)} in (${display(n.tokens.cached_input_tokens)} cached) · ${display(n.tokens.output_tokens)} out` : '—']]);
    if (n.error) host.append(el('p', n.error, 'warn'));
    const dec = n.decomposition;
    if (dec) {
      const ds = section(host, 'Decomposition');
      fields(ds, [['Decision', dec.decision], ['Phase', dec.phase], ['Attempts', dec.attempts]]);
      if (dec.reason) ds.append(el('p', dec.reason));
      const adm = list(dec.admission);
      if (adm.length) {
        const ul = el('ul', undefined, 'plain');
        for (const a of adm) { const li = el('li'); li.append(link(a.name, ctx.hash({ kind: 'fractal', task: sel.task, node: a.name }), () => ctx.onSelect({ kind: 'fractal', task: sel.task, node: a.name })), document.createTextNode(` · ${a.difficulty || '—'} · `), plate(a.model), document.createTextNode(` — ${a.goal || ''}`)); ul.append(li); }
        ds.append(el('p', 'Children it admitted:', 'muted'), ul);
      }
      if (dec.error) ds.append(el('p', dec.error, 'warn'));
    }
    if (list(n.decomposition_history).length > 1) details(host, `Decomposition history (${n.decomposition_history.length})`, n.decomposition_history.map(h => `${h.phase || '—'} · ${h.decision || '—'}${h.reason ? ' — ' + h.reason : ''}`).join('\n'), `fnode-${sel.task}-${sel.node}-hist`);
    const ps = section(host, 'Output');
    if (n.last != null) details(ps, 'Last message', n.last, `fnode-${sel.task}-${sel.node}-last`);
    if (n.planning_last != null) details(ps, 'Planner\'s last message', n.planning_last, `fnode-${sel.task}-${sel.node}-plan`);
    if (n.last == null && n.planning_last == null) muted(ps, 'No output yet.');
  }

  function finalView(host, ctx, sel) {
    const run = ctx.run, reviews = list(run.final_reviews);
    const head = el('div', undefined, 'insp-head');
    head.append(el('h2', 'Final review'), el('div', 'finish · whole-run review', 'insp-kind'));
    host.append(head);
    const pick = sel.name ? reviews.find(r => r.name === sel.name) : reviews[0];
    if (!pick) {
      head.append(chip(run.settings && run.settings.final_qa ? 'pending' : 'not requested', 'none'));
      muted(host, 'No whole-run review has been run for this plan. Per-task reviews only see one slice; the final review reads the combined diff with every task\'s requirements.');
      host.append(el('pre', `bash <forge>/scripts/forge-parallel.sh review ${run.path}${run.settings && run.settings.final_qa ? '' : ' --qa <spec>'}`, 'cmd'));
      return;
    }
    const chips = el('div', undefined, 'chips');
    chips.append(chip(pick.state === 'running' ? 'RUNNING' : pick.verdict || pick.state || 'unknown'));
    if (pick.published) chips.append(el('span', 'published', 'tag'));
    chips.append(el('span', pick.source === 'integrate' ? 'during integrate' : 'forge review', 'tag'));
    head.append(chips);
    fields(host, [['Reviewer', plate(pick.spec)], ['Threshold', pick.threshold], ['Reviewed', pick.base || pick.tip ? `${String(pick.base || '?').slice(0, 10)}..${String(pick.tip || '?').slice(0, 10)}` : '—', true],
      ['Started', time(pick.started ?? pick.at)], ['Ended', time(pick.ended)], ['Duration', RM.fmtDur(pick.duration_s)]]);
    if (pick.execution) { const s = { kind: 'execution', task: null, exec: pick.execution }; host.append(link('Model, timings and exact prompt →', ctx.hash(s), () => ctx.onSelect(s))); }
    { const a = diffLink(ctx, 'The combined diff it read, findings pinned →', { kind: 'final', name: pick.name }); if (a) { host.append(el('br')); host.append(a); } }
    const fs = section(host, 'Findings');
    if (pick.state === 'running') muted(fs, 'The reviewer is still reading the combined diff; its findings appear here when it finishes.');
    gateLine(fs, pick.gate);
    if (pick.state !== 'running') findingCounts(fs, pick.findings);
    const entry = lazy(ctx, `/api/runs/${encodeURIComponent(run.key)}/final-reviews/${encodeURIComponent(pick.name)}`, pick.state === 'done' ? 'done:' + pick.verdict : run.updated);
    if (!loading(fs, entry, 'the review')) {
      const d = entry.data;
      if (pick.state !== 'running') findingsList(fs, d.findings, `final-${pick.name}`);
      const ps = section(host, 'Report and prompt');
      details(ps, 'Reviewer report (qa.last)', d.qa_last, `final-${pick.name}-last`);
      details(ps, 'Prompt (qa.input)', d.qa_input, `final-${pick.name}-input`);
      if (d.tasks_summary) details(ps, 'Tasks it was told about', d.tasks_summary, `final-${pick.name}-tasks`);
      if (d.known_issues) details(ps, 'Known issues', d.known_issues, `final-${pick.name}-known`);
    }
    if (reviews.length > 1) {
      const hs = section(host, `History (${reviews.length})`);
      const ul = el('ul', undefined, 'plain');
      for (const r of reviews) {
        const li = el('li');
        li.append(link(`${time(r.started ?? r.at)} · ${r.verdict || r.state}`, ctx.hash({ kind: 'final', name: r.name }), () => ctx.onSelect({ kind: 'final', name: r.name })));
        if (r.findings && r.findings.worst) li.append(document.createTextNode(' '), sevChip(`${r.findings.worst}×${r.findings[r.findings.worst]}`, r.findings.worst));
        if (r.name === pick.name) li.className = 'current';
        ul.append(li);
      }
      hs.append(ul);
    }
  }

  function verificationView(host, ctx) {
    const run = ctx.run, v = run.verification;
    const head = el('div', undefined, 'insp-head');
    head.append(el('h2', 'Verification'), el('div', 'finish', 'insp-kind'), chip(v && v.status || 'pending'));
    host.append(head);
    if (!v) { muted(host, 'Verification has not run: it runs after every task is merged into the integration branch.'); return; }
    fields(host, [['Command', v.command, true], ['Commit', v.commit, true]]);
    if (list(v.runs).length) {
      const ul = el('ul', undefined, 'plain');
      for (const r of v.runs) ul.append(el('li', `#${display(r.n)} ${display(r.kind)} · exit ${display(r.exit)} · ${RM.fmtDur(r.duration_s)}${r.tree_changed ? ' · changed the tree' : ''}`));
      section(host, 'Runs').append(ul);
    }
    for (const x of list(run.timeline && run.timeline.verifications)) { const s = { kind: 'execution', task: null, exec: x.id }; host.append(el('p'), link(`Timings of the verification started ${time(x.started)} →`, ctx.hash(s), () => ctx.onSelect(s))); }
    if (v.log_tail != null) details(host, 'Output', v.log_tail, 'verification-output');
  }
  function integrationView(host, ctx) {
    const run = ctx.run, ig = run.integration || {};
    const F = RM.finishState(run);
    const head = el('div', undefined, 'insp-head');
    head.append(el('h2', 'Integration'), el('div', 'finish', 'insp-kind'), chip(F.integration.status));
    host.append(head);
    fields(host, [['Merged', `${F.integration.merged} of ${F.integration.total} tasks`], ['Base', ig.base, true], ['Accepted at', ig.accepted, true]]);
    if (list(ig.merged).length) {
      const ul = el('ul', undefined, 'plain');
      for (const m of ig.merged) { const li = el('li'); li.append(link(m.id, ctx.hash({ kind: 'task', id: m.id }), () => ctx.onSelect({ kind: 'task', id: m.id })), document.createTextNode(' · ' + time(m.at))); ul.append(li); }
      section(host, 'Merge order').append(ul);
    }
    if (list(ig.integrations).length) {
      const ul = el('ul', undefined, 'plain');
      for (const i of ig.integrations) ul.append(el('li', `${i.name} · verification ${display(i.verification_status)}${i.final_review ? ' · with final review' : ''}`));
      section(host, 'Integrate attempts').append(ul);
    }
  }
  function depLinks(deps, ctx) {
    const span = el('span', undefined, 'mono');
    list(deps).forEach((id, i) => { if (i) span.append(document.createTextNode(', ')); span.append(link(id, ctx.hash({ kind: 'task', id }), () => ctx.onSelect({ kind: 'task', id }))); });
    if (!span.hasChildNodes()) span.textContent = '—';
    return span;
  }

  function render(host, ctx) {
    host.replaceChildren();
    const run = ctx.run, sel = ctx.sel;
    if (!run) return;
    const close = sel && !(run.kind === 'solo' && sel.kind === 'task') ? link('', ctx.hash(null), () => ctx.onSelect(null)) : null;
    if (close) {
      close.className = 'insp-close button'; close.textContent = '';
      if (window.ForgeIcons) close.append(window.ForgeIcons.icon('close'));
      close.append(document.createTextNode('Close')); close.title = 'Back to the run (Esc on the stage)';
      host.append(close);
    }
    const tid = sel && (sel.kind === 'task' ? sel.id : sel.kind === 'execution' ? sel.task : null);
    const task = tid ? list(run.tasks).find(t => t.id === tid) : run.kind === 'solo' && sel && sel.kind === 'execution' ? list(run.tasks)[0] : null;
    if (!sel) return runView(host, ctx);
    if (sel.kind === 'task') {
      if (!task) { muted(host, `Task ${sel.id} is not in this run.`); return; }
      taskView(host, ctx, task);
      if (run.kind === 'solo') runRecord(host, ctx);      // a solo run is its one task: its record goes below it
      return;
    }
    if (sel.kind === 'execution') return execView(host, ctx, task, sel);
    if (sel.kind === 'final') return finalView(host, ctx, sel);
    if (sel.kind === 'verification') return verificationView(host, ctx);
    if (sel.kind === 'integration') return integrationView(host, ctx);
    if (sel.kind === 'fractal') return fractalNodeView(host, ctx, sel);
    return runView(host, ctx);
  }
  return { render, cache };
})();
