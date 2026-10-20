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
  const SEV = { P0: 'fail', P1: 'conflict', P2: 'timeout', P3: 'unknown' };
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
  function findingCounts(parent, f) {
    const row = el('div', undefined, 'chips');
    if (!f) { row.append(el('span', 'no findings parsed', 'muted')); parent.append(row); return; }
    let any = false;
    for (const sev of ['P0', 'P1', 'P2', 'P3']) if (f[sev]) { row.append(chip(`${sev} × ${f[sev]}`, SEV[sev])); any = true; }
    if (f.unlabelled) { row.append(chip(`unlabelled × ${f.unlabelled}`, 'unknown')); any = true; }
    if (f.plausible) { row.append(chip(`plausible × ${f.plausible}`, 'pending')); any = true; }
    if (!any) row.append(el('span', 'no confirmed findings', 'muted'));
    parent.append(row);
  }
  function findingsList(parent, findings, key) {
    if (findings === null || findings === undefined) { muted(parent, 'Findings could not be parsed (forge-contract.py was not available to the server).'); return; }
    if (!findings.length) { muted(parent, 'The reviewer reported no findings in the expected `- [P1][CONFIRMED] file:line — …` form.'); return; }
    const order = f => (f.severity ? Number(f.severity.slice(1)) : 4) + (f.status === 'CONFIRMED' ? 0 : .5);
    const ul = el('ul', undefined, 'findings');
    findings.slice().sort((a, b) => order(a) - order(b)).forEach((f, i) => {
      const li = el('li', undefined, 'finding' + (f.status === 'CONFIRMED' ? '' : ' plausible'));
      const head = el('div', undefined, 'finding-head');
      head.append(chip(f.severity || 'P?', f.severity ? SEV[f.severity] : 'unknown'), el('span', f.status, 'tag'));
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
    return [['Spec', m.spec, true], ['Harness', m.harness, true], ['Model', m.model, true], ['Effort', m.effort], m.clamped ? ['Clamped', m.clamped] : ['Clamped', undefined]];
  }

  /* ---------- views ---------- */
  function runView(host, ctx) {
    const run = ctx.run, now = ctx.now;
    const head = el('div', undefined, 'insp-head');
    head.append(el('div', run.kind === 'solo' ? 'solo run' : 'run', 'insp-kind'), el('h2', String(run.goal || 'Untitled run').split('\n')[0]));
    const chips = el('div', undefined, 'chips');
    for (const [s, n] of Object.entries(run.counts || {})) { const c = chip(s); c.append(document.createTextNode(' ' + n)); chips.append(c); }
    head.append(chips); host.append(head);
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
    muted(host, 'Select a task, an attempt or a finish node on the map or in the table to see its prompts, models, timings and findings.');
  }

  function taskView(host, ctx, task) {
    const run = ctx.run, now = ctx.now, solo = run.kind === 'solo';
    const head = el('div', undefined, 'insp-head');
    head.append(el('div', solo ? 'solo task' : `task · wave ${display(task.wave)}`, 'insp-kind'), el('h2', solo ? (task.title || 'Solo run') : `${task.id} · ${task.title || 'Untitled task'}`));
    const chips = el('div', undefined, 'chips');
    chips.append(chip(task.status));
    if (task.verdict) chips.append(chip('review ' + task.verdict, task.verdict.toLowerCase()));
    if (task.explored) chips.append(el('span', '* explored', 'tag'));
    if (task.accepted_by) chips.append(chip('accepted over FAIL', 'nochanges'));
    head.append(chips); host.append(head);
    if (!solo) fields(host, [['Difficulty', task.difficulty], ['Depends on', depLinks(task.deps, ctx)], ['Files', list(task.files).join('\n') || '—', true], ['Branch', task.branch, true]]);

    // the models: who owned it, how it was routed, what it was escalated to
    const ms = section(host, 'Models');
    fields(ms, [['Dwarf', task.dwarf, true], ['Reviewer', task.qa, true], ['Attempt', task.attempt]]);
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
        const label = `${p.label.toLowerCase()} · ${p.spec || '—'}`;
        const target = p.exec && p.exec.id ? { kind: 'execution', task: task.id, id: task.id, exec: p.exec.id, shadow: p.kind === 'shadow' } : null;
        line.append(target ? link(label, ctx.hash(target), () => ctx.onSelect(target)) : el('span', label), chip(p.status));
        if (e) line.append(el('span', `${RM.fmtDur(e.duration_s ?? (e.state === 'running' ? now - e.started : null))}${e.state === 'running' && e.phase ? ' · ' + e.phase : ''}`, 'muted'));
        if (p.kind === 'shadow' && p.shadow) line.append(el('span', p.shadow.agreement === 'disagree' ? '⚑ disagrees with the primary review' : p.shadow.agreement === 'agree' ? 'agrees' : '', p.shadow.agreement === 'disagree' ? 'warn' : 'muted'));
        const fs = (e && e.findings) || (p.shadow && p.shadow.findings);
        if (fs && fs.worst) line.append(chip(`${fs.worst}×${fs[fs.worst]}`, SEV[fs.worst]));
        box.append(line);
      }
      for (const l of tp.links.filter(l => l.kind === 'escalation' && z.parts.includes(l.to))) box.append(el('p', `escalated: ${l.label}`, 'muted'));
      as.append(box);
    }

    // reviews and their findings
    const rs = section(host, 'Review findings');
    gateLine(rs, task.gate);
    findingCounts(rs, task.findings);
    const detail = lazy(ctx, `/api/runs/${encodeURIComponent(run.key)}/tasks/${encodeURIComponent(task.id)}`, run.updated);
    if (!loading(rs, detail, 'the full findings')) {
      const reviews = list(detail.data.reviews);
      if (!reviews.length) muted(rs, 'No review has run yet.');
      reviews.forEach((r, i) => {
        const box = el('div', undefined, 'review');
        const h = el('div', undefined, 'attempt-head');
        h.append(el('strong', `${r.kind} review · attempt ${display(r.attempt)}`), el('span', r.spec || '', 'mono'), chip(r.verdict || r.state || 'unknown'));
        box.append(h);
        findingsList(box, r.findings, `task-${task.id}-r${i}`);
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
        fields(cs, [[label, `p(pass) = ${display(p.p)} → ${outcome ? outcome.status : 'not finished'}`], ['Assignment', p.assignment ? `${display(p.assignment.dwarf)} / ${display(p.assignment.qa)}` : '—', true]]);
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
    head.append(el('div', `${sel.shadow ? 'shadow ' : ''}${role === 'qa' ? 'review' : role} · attempt ${display(e && e.attempt)}`, 'insp-kind'),
      el('h2', `${task ? task.id + ' · ' : ''}${role === 'qa' ? 'reviewer' : role === 'dwarf' ? 'dwarf' : role}`));
    const chips = el('div', undefined, 'chips');
    if (e) { chips.append(chip(RM.execStatus(e))); if (e.verdict) chips.append(chip('verdict ' + e.verdict, e.verdict.toLowerCase())); }
    head.append(chips);
    if (task) head.append(link('← task ' + task.id, ctx.hash({ kind: 'task', id: task.id }), () => ctx.onSelect({ kind: 'task', id: task.id })));
    host.append(head);
    if (!e) { loading(host, entry, 'this execution'); return; }
    const ms = section(host, 'Model');
    fields(ms, modelFields(e.model));
    const ts = section(host, 'Timings');
    const running = e.state === 'running';
    fields(ts, [['Started', time(e.started)], ['Ended', running ? 'running' : time(e.ended)], ['Duration', RM.fmtDur(e.duration_s ?? (running && num(e.started) !== null ? now - e.started : null))],
      ['Phase', running ? `${display(e.phase)} for ${RM.fmtDur(num(e.phase_started) !== null ? now - e.phase_started : null)}` : e.phase], ['Tokens', tokens(e.tokens)],
      ['Prompt size', e.prompt_bytes != null ? `${e.prompt_bytes} bytes` : '—'], ['Exit', e.exit_code], ['Infra', e.infra_class], ['Timed out', e.timed_out ? 'yes' : undefined]]);
    phaseBar(ts, e.phases, e.duration_s);
    if (role === 'qa') {
      const rs = section(host, 'Findings');
      findingCounts(rs, e.findings);
      if (entry.data) findingsList(rs, entry.data.findings, `exec-${sel.exec}`); else loading(rs, entry, 'the findings');
    }
    const ps = section(host, 'Prompt and output');
    if (loading(ps, entry, 'the prompt')) return;
    const d = entry.data;
    details(ps, 'Prompt that launched it', d.prompt, `exec-${sel.exec}-prompt`, { open: false });
    details(ps, 'Final message', d.last, `exec-${sel.exec}-last`);
    if (d.resolved) details(ps, 'Resolved dispatch settings', Object.entries(d.resolved).map(([k, v]) => `${k}=${v}`).join('\n'), `exec-${sel.exec}-resolved`);
    if (list(d.warnings).length) details(ps, `Reader warnings (${d.warnings.length})`, d.warnings.join('\n'), `exec-${sel.exec}-warn`);
  }

  function finalView(host, ctx, sel) {
    const run = ctx.run, reviews = list(run.final_reviews);
    const head = el('div', undefined, 'insp-head');
    head.append(el('div', 'finish · whole-run review', 'insp-kind'), el('h2', 'Final review'));
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
    fields(host, [['Reviewer', pick.spec, true], ['Threshold', pick.threshold], ['Reviewed', pick.base || pick.tip ? `${String(pick.base || '?').slice(0, 10)}..${String(pick.tip || '?').slice(0, 10)}` : '—', true],
      ['Started', time(pick.started ?? pick.at)], ['Ended', time(pick.ended)], ['Duration', RM.fmtDur(pick.duration_s)]]);
    if (pick.execution) { const s = { kind: 'execution', task: null, exec: pick.execution }; host.append(link('Model, timings and exact prompt →', ctx.hash(s), () => ctx.onSelect(s))); }
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
        if (r.findings && r.findings.worst) li.append(document.createTextNode(' '), chip(`${r.findings.worst}×${r.findings[r.findings.worst]}`, SEV[r.findings.worst]));
        if (r.name === pick.name) li.className = 'current';
        ul.append(li);
      }
      hs.append(ul);
    }
  }

  function verificationView(host, ctx) {
    const run = ctx.run, v = run.verification;
    const head = el('div', undefined, 'insp-head');
    head.append(el('div', 'finish', 'insp-kind'), el('h2', 'Verification'), chip(v && v.status || 'pending'));
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
    head.append(el('div', 'finish', 'insp-kind'), el('h2', 'Integration'), chip(F.integration.status));
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
    const close = sel ? link('Close ✕', ctx.hash(null), () => ctx.onSelect(null)) : null;
    if (close) { close.className = 'insp-close'; host.append(close); }
    const tid = sel && (sel.kind === 'task' ? sel.id : sel.kind === 'execution' ? sel.task : null);
    const task = tid ? list(run.tasks).find(t => t.id === tid) : run.kind === 'solo' && sel && sel.kind === 'execution' ? list(run.tasks)[0] : null;
    if (!sel) return runView(host, ctx);
    if (sel.kind === 'task') { if (!task) { muted(host, `Task ${sel.id} is not in this run.`); return; } return taskView(host, ctx, task); }
    if (sel.kind === 'execution') return execView(host, ctx, task, sel);
    if (sel.kind === 'final') return finalView(host, ctx, sel);
    if (sel.kind === 'verification') return verificationView(host, ctx);
    if (sel.kind === 'integration') return integrationView(host, ctx);
    return runView(host, ctx);
  }
  return { render, cache };
})();
