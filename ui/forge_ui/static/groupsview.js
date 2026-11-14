/* forge-ui groups (#140, #136): the run page's groups block and the group page.
   window.ForgeGroups, and a CommonJS module under node:test.
   Run data only ever enters the page as text. The group page reads
   GET /api/runs/<key>/groups/<gid> and draws it; it never runs a command.
   The pure helpers are tested in ui/tests/js/groupsview.test.js.
   summary(run, ctx) is the run-page block, or null when the run has no groups.
   ctx for the block: { groupHref(id), taskHref(id) }.
   ctx for the page: { key, gid, run, plan, back, taskHref(id), transcriptHref(execution), fetchJSON(path) }. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeGroups = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  // REVIEWING is a review in flight: the same amber lamp as a running task. The other words match task lamps.
  const GROUP_LAMP = {
    PASS: 'pass', FAIL: 'fail', NEEDS_DECISION: 'needs_decision', REVIEWING: 'running', WAITING: 'waiting',
    UNKNOWN: 'unknown', CONFLICT: 'conflict', INFRA: 'infra', TIMEOUT: 'timeout', ERROR: 'error',
    INVALIDATED: 'invalidated', INTERRUPTED: 'interrupted',
  };

  function env() {
    return (typeof globalThis !== 'undefined' && globalThis) || root;
  }
  function doc() {
    const g = env();
    if (g && g.document && g.document.createElement) return g.document;
    if (root && root.document && root.document.createElement) return root.document;
    return null;
  }
  function el(tag, text, className) {
    const node = doc().createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function runModel() {
    if (root && root.ForgeRunModel) return root.ForgeRunModel;
    const g = env();
    return (g && g.ForgeRunModel) || null;
  }
  function icons() {
    if (root && root.ForgeIcons) return root.ForgeIcons;
    const g = env();
    return (g && g.ForgeIcons) || null;
  }
  // A shell word, copied from the inspector: a path with a space is quoted, a safe path is left as it is.
  function shellWord(s) {
    const text = String(s);
    return /^[\w@%+=:,./-]+$/.test(text) ? text : "'" + text.replace(/'/g, "'\\''") + "'";
  }

  function scopeText(review) {
    return review === 'group' ? 'one review' : 'per-task review';
  }
  function lampOf(status) {
    if (status && GROUP_LAMP[status]) return GROUP_LAMP[status];
    const word = String(status || '').toLowerCase();
    return /^[a-z0-9_]+$/.test(word) ? word : 'unknown';
  }
  function statusChip(status) {
    return el('span', status || '—', 'chip st-' + lampOf(status));
  }
  function reviewerNode(qa) {
    if (!qa) return el('span', '—', 'muted');
    const plate = icons();
    if (plate && plate.modelPlate) return plate.modelPlate(qa);
    return el('span', String(qa), 'mono');
  }
  function taskById(run, id) {
    return list(run && run.tasks).find(task => task && task.id === id) || null;
  }
  function memberStatus(run, id) {
    const task = taskById(run, id);
    if (!task) return 'unknown';
    const model = runModel();
    if (model && model.taskStatus) return String(model.taskStatus(task)).toLowerCase();
    return String(task.status || 'pending').toLowerCase();
  }
  function memberChip(id, run, href) {
    const node = href ? el('a', String(id), 'chip st-' + memberStatus(run, id)) : el('span', String(id), 'chip st-' + memberStatus(run, id));
    if (href) node.href = href;
    return node;
  }
  function membersRow(ids, run, hrefFor) {
    const row = el('div', undefined, 'group-members');
    const members = list(ids);
    if (!members.length) { row.append(el('span', 'no members', 'muted')); return row; }
    for (const id of members) row.append(memberChip(id, run, hrefFor ? hrefFor(id) : null));
    return row;
  }
  function implicatedText(ids) {
    const names = list(ids);
    return names.length ? 'implicated ' + names.join(', ') : 'implicated none';
  }
  function rowTitle(group, href) {
    const text = group.title || group.id || '';
    if (group.review === 'group' && href) {
      const link = el('a', text, 'group-title');
      link.href = href;
      return link;
    }
    return el('span', text, 'group-title');
  }

  // The run page's groups block. Nothing when the run has no groups, so that page stays as it was.
  function summary(run, ctx) {
    const groups = run && run.groups;
    if (!Array.isArray(groups) || !groups.length) return null;
    ctx = ctx || {};
    const block = el('div', undefined, 'groups-block');
    const head = el('div', undefined, 'module-head');
    head.append(el('h2', 'Groups', 'module-title'), el('span', String(groups.length), 'module-count'));
    const rows = el('div', undefined, 'group-rows');
    for (const group of groups) {
      if (!group || typeof group !== 'object') continue;
      rows.append(groupRow(group, run, ctx));
    }
    block.append(head, rows);
    return block;
  }

  function groupRow(group, run, ctx) {
    const row = el('article', undefined, 'group-row');
    row.append(rowTitle(group, ctx.groupHref ? ctx.groupHref(group.id) : null));
    row.append(el('p', group.goal ? String(group.goal) : '(no goal)', 'group-goal'));
    const meta = el('div', undefined, 'group-meta');
    meta.append(el('span', scopeText(group.review), 'group-scope'), reviewerNode(group.qa));
    row.append(meta);
    row.append(membersRow(group.members, run, ctx.taskHref));
    if (group.review === 'group') {
      const state = el('p', undefined, 'group-state');
      if (group.status) state.append(statusChip(group.status));
      else state.append(el('span', 'no status', 'muted'));
      state.append(el('span', group.round == null ? 'round —' : 'round ' + group.round, 'group-round'));
      state.append(el('span', implicatedText(group.implicated), 'group-implicated'));
      row.append(state);
    }
    return row;
  }

  // `forge group retry <plan> <gid> --tasks …`. Copyable text: the page does not run it.
  function decisionCommand(group, plan) {
    const path = plan ? shellWord(plan) : '<plan>';
    const gid = shellWord(group && group.id ? group.id : '');
    const members = list(group && group.members).map(id => String(id)).filter(Boolean);
    const tasks = members.length ? shellWord(members.join(',')) : '…';
    return 'forge group retry ' + path + ' ' + gid + ' --tasks ' + tasks;
  }

  // The attribution table. A finding is highlighted when it is flagged ambiguous or listed in attribution.ambiguous.
  function findingsTable(attribution) {
    const findings = attribution && Array.isArray(attribution.findings) ? attribution.findings : null;
    if (!findings || !findings.length) return el('p', 'No findings.', 'muted');
    const ambiguous = new Set(list(attribution.ambiguous));
    const table = el('table', undefined, 'group-findings');
    const headRow = el('tr');
    for (const label of ['Severity', 'Status', 'File', 'Tasks', 'Blocking', 'Ambiguous']) headRow.append(el('th', label));
    const head = el('thead');
    head.append(headRow);
    const body = el('tbody');
    for (const finding of findings) {
      if (!finding || typeof finding !== 'object') continue;
      const flagged = finding.ambiguous === true || ambiguous.has(finding.n);
      const tr = el('tr', undefined, flagged ? 'group-ambiguous' : '');
      const file = finding.file ? (finding.line == null ? String(finding.file) : String(finding.file) + ':' + finding.line) : '—';
      const tasks = list(finding.tasks);
      const cells = [
        [finding.severity || '—', ''],
        [finding.status || '—', ''],
        [file, 'mono'],
        [tasks.length ? tasks.join(', ') : '—', ''],
        [finding.blocking ? 'yes' : 'no', finding.blocking ? 'group-blocking' : ''],
        [flagged ? 'yes' : 'no', ''],
      ];
      for (const [text, className] of cells) tr.append(el('td', text, className || undefined));
      body.append(tr);
    }
    table.append(head, body);
    return table;
  }

  function decisionSection(group, plan) {
    if (!group || group.status !== 'NEEDS_DECISION') return null;
    const section = el('section', undefined, 'group-section group-decision');
    section.append(el('h2', 'Decision', 'plate'));
    section.append(el('p', group.decision ? String(group.decision) : 'No decision recorded.', group.decision ? '' : 'muted'));
    section.append(el('pre', decisionCommand(group, plan), 'group-cmd'));
    return section;
  }

  function roundsSection(group) {
    const section = el('section', undefined, 'group-section group-rounds');
    section.append(el('h2', 'Rounds', 'plate'));
    const rounds = list(group.rounds);
    if (!rounds.length) { section.append(el('p', 'No rounds.', 'muted')); return section; }
    const byRound = new Map(list(group.members_by_round).filter(bucket => bucket && typeof bucket === 'object').map(bucket => [bucket.round, list(bucket.members)]));
    for (const round of rounds) {
      if (!round || typeof round !== 'object') continue;
      const article = el('article', undefined, 'group-round-row');
      article.append(el('h3', 'Round ' + round.round));
      const meta = el('p', undefined, 'group-meta');
      if (round.status) meta.append(statusChip(round.status));
      meta.append(reviewerNode(round.reviewer));
      meta.append(el('span', implicatedText(round.implicated), 'group-implicated'));
      if (round.epoch != null && Number.isFinite(Number(round.epoch))) {
        const when = new Date(Number(round.epoch) * 1000);
        if (!Number.isNaN(when.getTime())) meta.append(el('span', when.toLocaleString(), 'muted'));
      }
      article.append(meta);
      const members = byRound.get(round.round) || [];
      if (members.length) {
        const line = el('p', undefined, 'group-round-members mono');
        line.textContent = members.map(member => {
          if (!member || typeof member !== 'object') return '';
          const attempt = member.attempt == null ? '' : ' attempt ' + member.attempt;
          const commit = member.commit ? ' ' + member.commit : '';
          return (member.task || '—') + commit + attempt;
        }).filter(Boolean).join(', ');
        article.append(line);
      }
      section.append(article);
    }
    return section;
  }

  function textSection(title, value, empty, className) {
    const section = el('section', undefined, 'group-section ' + className);
    section.append(el('h2', title, 'plate'));
    if (value == null || value === '') section.append(el('p', empty, 'muted'));
    else section.append(el('pre', String(value)));
    return section;
  }

  function transcriptSection(group, ctx) {
    const section = el('section', undefined, 'group-section group-transcript');
    section.append(el('h2', 'Review transcript', 'plate'));
    const execution = group && group.execution;
    const href = execution && execution.id && ctx.transcriptHref ? ctx.transcriptHref(execution) : null;
    if (!href) { section.append(el('p', 'No review transcript.', 'muted')); return section; }
    const link = el('a', 'Review transcript', 'diff-link');
    link.href = href;
    section.append(link);
    return section;
  }

  function pageHeader(group, ctx) {
    const head = el('header', undefined, 'group-head');
    const back = el('a', 'Back to the run', 'group-back');
    back.href = ctx.back || '#/';
    const title = el('h1', group.title || group.id || '', 'group-title');
    head.append(back, title, el('p', group.goal ? String(group.goal) : '(no goal)', 'group-goal'));
    const meta = el('div', undefined, 'group-meta');
    meta.append(el('span', scopeText(group.review), 'group-scope'), reviewerNode(group.qa));
    if (group.review === 'group') {
      if (group.status) meta.append(statusChip(group.status));
      else meta.append(el('span', 'no status', 'muted'));
      meta.append(el('span', group.round == null ? 'round —' : 'round ' + group.round, 'group-round'));
      meta.append(el('span', implicatedText(group.implicated), 'group-implicated'));
    }
    head.append(meta, membersRow(group.members, ctx.run, ctx.taskHref));
    return head;
  }

  // The group page, from one group payload. Synchronous, so a test can read it without a fetch.
  function pageView(group, ctx) {
    ctx = ctx || {};
    const page = el('div', undefined, 'group-page');
    if (!group || typeof group !== 'object') {
      page.append(el('p', 'This group is not in the run.', 'empty'));
      return page;
    }
    page.append(pageHeader(group, ctx));
    const decision = decisionSection(group, ctx.plan);
    if (decision) page.append(decision);
    const findings = el('section', undefined, 'group-section group-findings-section');
    findings.append(el('h2', 'Findings', 'plate'));
    // attribution.json is kept across rounds (the next prompt quotes it): a round that left no review has
    // none of its own, so the table is an earlier round's and says so.
    const attribution = group.attribution;
    if (attribution && Number.isInteger(attribution.round) && Number.isInteger(group.round) && attribution.round !== group.round) {
      findings.append(el('p', 'From round ' + attribution.round + ': round ' + group.round + ' has no attributed findings.', 'muted group-stale'));
    }
    findings.append(findingsTable(attribution));
    page.append(findings, roundsSection(group));
    page.append(textSection('Known issues', group.known_issues, 'None.', 'group-known'));
    page.append(textSection('Review', group.qa_last, 'No review yet.', 'group-review'));
    page.append(transcriptSection(group, ctx));
    return page;
  }

  // A task's `group <id>` chip. Null when the task is in no group.
  function taskChip(gid, href) {
    if (!gid) return null;
    const link = el('a', 'group ' + gid, 'group-chip');
    if (href) link.href = href;
    return link;
  }

  // Fetch the group and draw it. A poll that still has the same run skips the fetch.
  // A newer navigation (host._groupsSeq) drops a response that arrived late.
  function render(host, ctx) {
    ctx = ctx || {};
    if (!ctx.run) {
      host.replaceChildren(el('p', 'Loading the group…', 'empty'));
      return;
    }
    const token = String(ctx.key) + '\n' + String(ctx.gid) + '\n' + String(ctx.run.updated);
    if (host._groupsToken === token && host.childElementCount) return;
    const seq = (host._groupsSeq = (host._groupsSeq || 0) + 1);
    const same = host.dataset && host.dataset.group === String(ctx.gid) && host.dataset.run === String(ctx.key) && host._groupsData;
    if (!same) host.replaceChildren(el('p', 'Loading the group…', 'empty'));
    const path = '/api/runs/' + encodeURIComponent(ctx.key) + '/groups/' + encodeURIComponent(ctx.gid);
    Promise.resolve().then(() => ctx.fetchJSON(path)).then(data => {
      if (host.isConnected === false || host._groupsSeq !== seq) return;
      host._groupsToken = token;
      host._groupsData = data;
      if (host.dataset) { host.dataset.group = String(ctx.gid); host.dataset.run = String(ctx.key); }
      host.replaceChildren(pageView(data, ctx));
    }, error => {
      if (host.isConnected === false || host._groupsSeq !== seq) return;
      host._groupsToken = '';
      const missing = error && error.status === 404;
      host.replaceChildren(el('p', missing ? 'This group is not in the run.' : 'Could not load this group.', 'empty'));
    });
  }

  return { summary, findingsTable, decisionCommand, taskChip, pageView, render, scopeText };
});
