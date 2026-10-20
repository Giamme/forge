/* forge-ui model leaderboard: pure aggregation of GET /api/models facts (no DOM, no I/O). A plain script in the
   page (window.ForgeModelStats) and a CommonJS module under node:test.
   Outcomes are the server's labels, which follow scripts/forge_dataset/export.py `_outcome`: a censored attempt
   (reviewer or forge trouble, no changes, interrupted, ...) says nothing about the implementer and counts in no
   rate. First-attempt rates follow scripts/forge_dataset/routing.py: each task's first attempt (`attempts[0]`),
   n = pass + fail, with routing.py's `wilson` interval. */
(function (root, factory) {
  'use strict';
  const RM = typeof module === 'object' && module.exports ? require('./runmodel.js') : root.ForgeRunModel;
  const api = factory(RM);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeModelStats = api;
})(typeof self !== 'undefined' ? self : this, function (RM) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const DIFFS = ['low', 'medium', 'high', 'none'];
  const KINDS = ['plan', 'solo'];
  const OUTCOMES = ['pass', 'fail', 'censored'];
  const REVIEW_KINDS = ['primary', 'shadow', 'final'];
  const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
  const DAY = 86400;
  const DEFAULT_MIN_N = 5;
  const diffOf = t => { const d = String(t && t.difficulty || '').toLowerCase(); return DIFFS.includes(d) ? d : 'none'; };
  const aliasOf = x => (x && x.alias) || RM.alias(x && x.spec) || null;
  const taskKey = (run, task) => `${run}\u0000${task}`;

  // routing.py `wilson`, the same arithmetic in the same order, so the bounds match it to the last bit
  function wilson(k, n, z = 1.96) {
    if (!n) return { p: null, lo: null, hi: null };
    const p = k / n;
    const centre = (p + z * z / (2 * n)) / (1 + z * z / n);
    const half = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n);
    return { p, lo: Math.max(0, centre - half), hi: Math.min(1, centre + half) };
  }
  // statistics.median: the mean of the middle two for an even count; nulls are ignored
  function median(xs) {
    const v = list(xs).filter(x => num(x) !== null).sort((a, b) => a - b);
    if (!v.length) return null;
    const m = v.length >> 1;
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
  }

  /* ---------- filters ---------- */
  function emptyFilter() { return { project: '', difficulty: '', kind: '', days: 0, exploratoryOnly: false, minN: DEFAULT_MIN_N }; }
  function normFilter(f) {
    f = obj(f) || {};
    return {
      project: typeof f.project === 'string' ? f.project : '',
      difficulty: DIFFS.includes(f.difficulty) ? f.difficulty : '',
      kind: KINDS.includes(f.kind) ? f.kind : '',
      days: num(f.days) !== null && f.days > 0 ? Math.floor(f.days) : 0,
      exploratoryOnly: f.exploratoryOnly === true,
      minN: num(f.minN) !== null && f.minN >= 0 ? Math.floor(f.minN) : DEFAULT_MIN_N,
    };
  }
  // filters that hide tasks (minN only greys rows)
  function activeCount(f) {
    f = normFilter(f);
    return [f.project, f.difficulty, f.kind, f.days, f.exploratoryOnly].filter(Boolean).length;
  }
  function inPeriod(created, f, now) {
    return !f.days || num(now) === null || (num(created) !== null && created >= now - f.days * DAY);
  }
  // a run's own facts (repo, kind, created): from its task rows, else from the run summaries
  function runIndex(data, runs) {
    const by = new Map();
    for (const t of list(data.tasks)) if (obj(t) && t.run && !by.has(t.run)) by.set(t.run, { repo: t.repo || null, kind: t.kind || null, created: num(t.created) });
    for (const r of list(runs)) if (obj(r) && r.key && !by.has(r.key)) by.set(r.key, { repo: r.repo || null, kind: r.kind || null, created: num(r.created) });
    return by;
  }
  function taskMatch(t, f, now) {
    if (f.project && t.repo !== f.project) return false;
    if (f.difficulty && diffOf(t) !== f.difficulty) return false;
    if (f.kind && t.kind !== f.kind) return false;
    if (!inPeriod(t.created, f, now)) return false;
    if (f.exploratoryOnly && t.explored !== true) return false;
    return true;
  }
  // a task's review goes with its task; a final review belongs to a whole run, so it has no difficulty and was never explored
  function reviewMatch(v, f, now, tasks, runs) {
    if (v.task !== null && v.task !== undefined) {
      const t = tasks.get(taskKey(v.run, v.task));
      if (t) return taskMatch(t, f, now);
    }
    if (f.difficulty || f.exploratoryOnly) return false;
    const r = runs.get(v.run) || {};
    if (f.project && r.repo !== f.project) return false;
    if (f.kind && r.kind !== f.kind) return false;
    return inPeriod(r.created, f, now);
  }

  /* ---------- the choices the filters offer ---------- */
  function projectNames(repos) {
    const out = [...repos].map(([repo, tasks]) => ({ repo, name: RM.basename(repo), tasks }));
    // two repos with the same folder name: add the parent folder (as cloudmodel.js projects())
    const names = new Map(); for (const p of out) names.set(p.name, (names.get(p.name) || 0) + 1);
    for (const p of out) if (names.get(p.name) > 1) { const parts = String(p.repo).split(/[\\/]/).filter(Boolean); p.name = `${p.name} (${parts[parts.length - 2] || '/'})`; }
    return out.sort((a, b) => a.name.localeCompare(b.name) || a.repo.localeCompare(b.repo));
  }
  function options(data, runs) {
    data = obj(data) || {};
    const tasks = list(data.tasks).filter(obj);
    const repos = new Map(), diff = {}, kinds = {}, aliases = new Set();
    let first = null, last = null, explored = 0;
    for (const t of tasks) {
      if (t.repo) repos.set(t.repo, (repos.get(t.repo) || 0) + 1);
      const d = diffOf(t); diff[d] = (diff[d] || 0) + 1;
      if (t.kind) kinds[t.kind] = (kinds[t.kind] || 0) + 1;
      const c = num(t.created);
      if (c !== null) { first = first === null ? c : Math.min(first, c); last = last === null ? c : Math.max(last, c); }
      if (t.explored === true) explored++;
      for (const a of list(t.attempts)) { const x = obj(a) && aliasOf(a); if (x) aliases.add(x); }
    }
    // a run with reviews but no task rows still names its project
    const idx = runIndex(data, runs);
    for (const v of list(data.reviews)) {
      if (!obj(v)) continue;
      const x = aliasOf(v); if (x) aliases.add(x);
      const r = idx.get(v.run); if (r && r.repo && !repos.has(r.repo)) repos.set(r.repo, 0);
    }
    return {
      projects: projectNames(repos),
      difficulties: DIFFS.filter(d => diff[d]).map(d => ({ value: d, label: d === 'none' ? 'not set' : d, tasks: diff[d] })),
      kinds: KINDS.filter(k => kinds[k]).map(k => ({ value: k, tasks: kinds[k] })),
      range: { first, last },
      explored,
      aliases: [...aliases].sort(),
    };
  }

  /* ---------- implementers: one row per alias over the attempts of the matching tasks ---------- */
  function implementerRows(tasks, minN) {
    const by = new Map();
    let unattributed = 0;
    const row = alias => {
      if (!by.has(alias)) by.set(alias, { alias, tasks: 0, first: { k: 0, n: 0, censored: 0, reasons: {} }, attempts: { pass: 0, fail: 0, censored: 0, total: 0 },
        retries: 0, escalationsAway: 0, timeouts: 0, timeoutKinds: { implementer: 0, reviewer: 0, forge: 0 }, gateAccepts: 0, humanAccepts: 0,
        model: [], duration: [], tokens: { input: 0, cached: 0, output: 0, attempts: 0, coverage: null } });
      return by.get(alias);
    };
    for (const t of tasks) {
      const atts = list(t.attempts).filter(obj).slice().sort((a, b) => (num(a.n) ?? 0) - (num(b.n) ?? 0));
      atts.forEach((a, i) => {
        const alias = aliasOf(a);
        if (!alias) { unattributed++; return; }
        const r = row(alias), o = OUTCOMES.includes(a.outcome) ? a.outcome : 'censored', reason = String(a.reason || '');
        r.attempts[o]++; r.attempts.total++;
        if (i === 0) {
          r.tasks++;
          if (o === 'censored') { r.first.censored++; r.first.reasons[reason || 'unknown'] = (r.first.reasons[reason || 'unknown'] || 0) + 1; }
          else { r.first.n++; if (o === 'pass') r.first.k++; }
        } else r.retries++;
        if (a.timed_out === true || reason.endsWith('_timeout')) {
          r.timeouts++;
          r.timeoutKinds[a.timed_out === true ? 'implementer' : reason === 'reviewer_timeout' ? 'reviewer' : reason === 'forge_timeout' ? 'forge' : 'implementer']++;
        }
        if (a.gate === true) r.gateAccepts++;
        r.model.push(a.model_s); r.duration.push(a.duration_s);
        const tk = obj(a.tokens);
        if (tk) { r.tokens.attempts++; for (const k of ['input', 'cached', 'output']) r.tokens[k] += num(tk[k]) || 0; }
      });
      // A human accepted the task although no attempt passed: the work accepted is the last attempt's.
      if (t.accepted === true && atts.length && !atts.some(a => a.outcome === 'pass')) {
        const alias = aliasOf(atts[atts.length - 1]); if (alias) row(alias).humanAccepts++;
      }
      for (const e of list(t.escalations)) { const from = obj(e) && RM.alias(e.from); if (from) row(from).escalationsAway++; }
    }
    const rows = [...by.values()].map(r => {
      const { model, duration, ...rest } = r;
      return { ...rest, first: { ...r.first, ...wilson(r.first.k, r.first.n) }, small: r.first.n < minN,
        medianModelS: median(model), medianDurationS: median(duration),
        tokens: { ...r.tokens, coverage: r.attempts.total ? r.tokens.attempts / r.attempts.total : null } };
    });
    rows.sort((a, b) => (b.first.lo ?? -1) - (a.first.lo ?? -1) || b.first.n - a.first.n || a.alias.localeCompare(b.alias));
    return { rows, unattributed };
  }

  /* ---------- reviewers: one row per alias over the matching reviews ---------- */
  function reviewerRows(reviews) {
    const by = new Map();
    let unattributed = 0;
    for (const v of reviews) {
      const alias = aliasOf(v);
      if (!alias) { unattributed++; continue; }
      if (!by.has(alias)) by.set(alias, { alias, reviews: { primary: 0, shadow: 0, final: 0, total: 0 }, verdicts: { PASS: 0, FAIL: 0, other: 0 },
        sums: { P0: 0, P1: 0, P2: 0, P3: 0, unlabelled: 0, plausible: 0 }, withFindings: 0, agreement: { k: 0, n: 0, unknown: 0 },
        duration: [], tokens: { input: 0, cached: 0, output: 0, reviews: 0, coverage: null } });
      const r = by.get(alias);
      if (REVIEW_KINDS.includes(v.kind)) r.reviews[v.kind]++;
      r.reviews.total++;
      r.verdicts[v.verdict === 'PASS' || v.verdict === 'FAIL' ? v.verdict : 'other']++;
      const f = obj(v.findings);
      if (f) { r.withFindings++; for (const k of Object.keys(r.sums)) r.sums[k] += num(f[k]) || 0; }
      // agree/disagree need both verdicts; `unknown` (or none) says nothing either way
      if (v.kind === 'shadow') { if (v.agreement === 'agree' || v.agreement === 'disagree') { r.agreement.n++; if (v.agreement === 'agree') r.agreement.k++; } else r.agreement.unknown++; }
      r.duration.push(v.duration_s);
      const tk = obj(v.tokens);
      if (tk) { r.tokens.reviews++; for (const k of ['input', 'cached', 'output']) r.tokens[k] += num(tk[k]) || 0; }
    }
    const rows = [...by.values()].map(r => {
      const { sums, withFindings, duration, ...rest } = r;
      const findings = { n: withFindings };
      for (const k of Object.keys(sums)) findings[k] = withFindings ? sums[k] / withFindings : null;
      return { ...rest, findings, findingTotals: sums, agreement: { ...r.agreement, ...wilson(r.agreement.k, r.agreement.n) },
        medianDurationS: median(duration), tokens: { ...r.tokens, coverage: r.reviews.total ? r.tokens.reviews / r.reviews.total : null } };
    });
    rows.sort((a, b) => b.reviews.total - a.reviews.total || a.alias.localeCompare(b.alias));
    return { rows, unattributed };
  }

  /* ---------- the leaderboard ---------- */
  function leaderboard(data, filter, now, runs) {
    data = obj(data) || {};
    const f = normFilter(filter);
    const allTasks = list(data.tasks).filter(t => obj(t) && t.run), allReviews = list(data.reviews).filter(v => obj(v) && v.run);
    const byTask = new Map(allTasks.map(t => [taskKey(t.run, t.task), t])), idx = runIndex(data, runs);
    const tasks = allTasks.filter(t => taskMatch(t, f, now));
    const reviews = allReviews.filter(v => reviewMatch(v, f, now, byTask, idx));
    const imp = implementerRows(tasks, f.minN), rev = reviewerRows(reviews);
    const byDifficulty = {};
    for (const d of DIFFS) byDifficulty[d] = implementerRows(tasks.filter(t => diffOf(t) === d), f.minN).rows;

    const attempts = tasks.reduce((s, t) => s + list(t.attempts).filter(obj).length, 0);
    const first = { k: 0, n: 0, censored: 0 };
    for (const r of imp.rows) { first.k += r.first.k; first.n += r.first.n; first.censored += r.first.censored; }
    const withTokens = imp.rows.reduce((s, r) => s + r.tokens.attempts, 0);
    const finalsAll = allReviews.filter(v => v.task === null || v.task === undefined).length;
    const totals = {
      runs: new Set([...tasks.map(t => t.run), ...reviews.map(v => v.run)]).size, tasks: tasks.length,
      tasksRun: tasks.filter(t => list(t.attempts).some(obj)).length, attempts, first: { ...first, ...wilson(first.k, first.n) },
      reviews: reviews.length, unattributedAttempts: imp.unattributed, unattributedReviews: rev.unattributed,
      allTasks: allTasks.length, allReviews: allReviews.length, servedRuns: num(data.runs), truncated: data.truncated === true,
      warnings: list(data.warnings).length,
    };

    const caveats = [];
    const s = (n, one, many) => `${n} ${n === 1 ? one : many || one + 's'}`;
    if (totals.truncated) caveats.push('Only the newest runs were read: the server stops at 50,000 task rows.');
    if (totals.warnings) caveats.push(`${s(totals.warnings, 'unreadable entry', 'unreadable entries')} in the runs' files ${totals.warnings === 1 ? 'was' : 'were'} skipped.`);
    if (imp.unattributed) caveats.push(`${s(imp.unattributed, 'attempt')} with no recorded model ${imp.unattributed === 1 ? 'is' : 'are'} left out.`);
    if (rev.unattributed) caveats.push(`${s(rev.unattributed, 'review')} with no recorded model ${rev.unattributed === 1 ? 'is' : 'are'} left out.`);
    if ((f.difficulty || f.exploratoryOnly) && finalsAll) caveats.push('Final reviews cover a whole run, so the difficulty and exploratory filters leave them out.');
    if (attempts && withTokens < attempts - imp.unattributed) caveats.push(`Token counts cover ${withTokens} of ${attempts - imp.unattributed} attempts: older runs did not record them.`);
    if (imp.rows.some(r => r.small)) caveats.push(`Models with fewer than ${s(f.minN, 'first-attempt outcome')} are greyed: their interval is too wide to rank them.`);
    if (first.censored) caveats.push(`${s(first.censored, 'first attempt')} ended without a verdict on the implementer (reviewer or forge trouble, no changes, interrupted) and ${first.censored === 1 ? 'counts' : 'count'} in no rate.`);

    return { implementers: imp.rows, reviewers: rev.rows, byDifficulty, totals, caveats, filter: f };
  }

  return { DIFFS, KINDS, SEVERITIES, DEFAULT_MIN_N, wilson, median, emptyFilter, normFilter, activeCount, taskMatch, options, leaderboard };
});
