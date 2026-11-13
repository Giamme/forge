/* forge-ui brief questions (#135) and proposals (#136): a task's questions in the inspector, the run's
   open questions, and the proposals waiting on an answer. window.ForgeBrief, and a CommonJS module
   under node:test. The pure helpers (openQuestions, orderQuestions, startLine, groupQuestions,
   canPropose, affectedLabel) are tested in ui/tests/js/briefview.test.js. Question text, answers,
   reasons and diffs are untrusted: they only ever enter the page as text.
   Ordering is flagged (must answer) first, then by the projected start from projectSchedule. A task with no
   finite start is last. Wave numbers are a preview and are never a sort key. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeBrief = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  // A question with no group, drawn only when it shares the list with a group or with Plan.
  // An all-loose list stays flat, so this title is not a heading there.
  const LOOSE_TITLE = 'No group';
  const notices = new Map();                                 // run\ntask\nqid → the late answer's stdout
  const proposalNotices = new Map();                         // run\npid → the apply/reject/propose stdout

  function page() {
    if (root && root.document) return root;
    return globalThis;
  }

  function forgeUI() {
    if (root && root.ForgeUI) return root.ForgeUI;
    return globalThis.ForgeUI;
  }

  function el(tag, text, className) {
    const node = page().document.createElement(tag);
    if (text !== undefined) node.textContent = text === null || text === undefined ? '' : String(text);
    if (className) node.className = className;
    return node;
  }

  function noticeKey(run, task, question) {
    return String(run && run.key) + '\n' + String(task && task.id) + '\n' + String(question && question.id);
  }

  function proposalKey(runKey, pid) {
    return String(runKey) + '\n' + String(pid);
  }

  // Open task questions, in task order. Parked questions are wider than the task and are not listed here.
  function openQuestions(run) {
    const items = [];
    for (const task of list(run && run.tasks)) {
      const brief = task && task.brief;
      if (!brief || typeof brief !== 'object') continue;
      for (const question of list(brief.questions)) {
        if (!question || typeof question !== 'object' || question.state !== 'open') continue;
        items.push({ task, question });
      }
    }
    return items;
  }

  function projectionStart(projections, task) {
    if (!projections || !task) return null;
    const row = projections[task.id];
    if (!row || typeof row !== 'object') return null;
    return num(row.start);
  }

  // Flagged first, then the earliest finite projected start. No start (missing, or not a finite number) sorts
  // last inside its group. The original index breaks ties, so equal starts keep the order they were given.
  function orderQuestions(items, projections) {
    const rows = list(items).map((item, index) => ({ item, index }));
    rows.sort((a, b) => {
      const fa = a.item && a.item.question && a.item.question.must_answer === true ? 0 : 1;
      const fb = b.item && b.item.question && b.item.question.must_answer === true ? 0 : 1;
      if (fa !== fb) return fa - fb;
      const sa = projectionStart(projections, a.item && a.item.task);
      const sb = projectionStart(projections, b.item && b.item.task);
      if (sa === null && sb !== null) return 1;
      if (sa !== null && sb === null) return -1;
      if (sa !== null && sb !== null && sa !== sb) return sa - sb;
      return a.index - b.index;
    });
    return rows.map(row => row.item);
  }

  // A wait, rounded to a phrase. Under 45s is "about now"; otherwise the nearest minute, then hours.
  function aboutText(seconds) {
    const s = Math.max(0, num(seconds) === null ? 0 : seconds);
    if (s < 45) return 'about now';
    const minutes = Math.round(s / 60);
    if (minutes <= 1) return 'about 1 min';
    if (minutes < 60) return 'about ' + minutes + ' min';
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return rest ? 'about ' + hours + ' h ' + rest + ' min' : 'about ' + hours + ' h';
  }

  function namesPhrase(deps) {
    const names = list(deps).map(id => String(id));
    if (names.length <= 1) return names[0] || '';
    if (names.length === 2) return names[0] + ' and ' + names[1];
    return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  }

  // "starts after A and B merge, about 40 min". No finite start → null. No clock → the wait is about now.
  // A task that has started (any status but PENDING or BLOCKED) says so instead: its projected start is in
  // the past, and an answer now is a late one.
  function startLine(spec) {
    spec = spec || {};
    const status = typeof spec.status === 'string' ? spec.status : '';
    if (status === 'RUNNING') return 'running now';
    if (status && status !== 'PENDING' && status !== 'BLOCKED') return 'already started';
    const start = num(spec.start);
    if (start === null) return null;
    const clock = num(spec.now);
    const about = aboutText(clock === null ? 0 : start - clock);
    const deps = list(spec.deps);
    if (!deps.length) return about === 'about now' ? 'starts about now' : 'starts in ' + about;
    const verb = deps.length === 1 ? 'merges' : 'merge';
    return 'starts after ' + namesPhrase(deps) + ' ' + verb + ', ' + about;
  }

  // Deps that have not merged yet, including an id the run does not know. PASS is still ahead of the merge.
  function waitingDeps(task, tasks) {
    const byId = new Map(list(tasks).map(item => [item && item.id, item]));
    return list(task && task.deps).filter(id => {
      const dep = byId.get(id);
      return !dep || dep.status !== 'MERGED';
    });
  }

  // Where one question sits. A plan question is under "Plan". A wave question uses only its own group
  // (an empty one stays loose, not the task's group). Anything else uses the question's group, then the task's.
  function bucketOf(item) {
    const question = item && item.question;
    const task = item && item.task;
    const scope = question && question.scope;
    if (scope === 'plan') return { kind: 'plan' };
    if (scope === 'wave') {
      const id = question && typeof question.group === 'string' ? question.group : '';
      return id ? { kind: 'group', id } : { kind: 'loose' };
    }
    const fromQuestion = question && typeof question.group === 'string' && question.group ? question.group : '';
    const fromTask = task && typeof task.group === 'string' && task.group ? task.group : '';
    const id = fromQuestion || fromTask;
    return id ? { kind: 'group', id } : { kind: 'loose' };
  }

  // Sections in run.groups order, then unknown group ids in the order they appear, then Plan, then loose.
  // A group with no question in `items` is skipped. Order inside a section is the order of `items`.
  // Loose questions (no group of their own, and not plan-scoped) carry LOOSE_TITLE so a mixed list
  // does not file them under the previous group or under Plan.
  function groupQuestions(items, groups) {
    const catalog = new Map();
    for (const group of list(groups)) {
      if (group && typeof group.id === 'string' && group.id && !catalog.has(group.id)) catalog.set(group.id, group);
    }
    const byGroup = new Map();
    const unknown = [];
    const plan = [];
    const loose = [];
    for (const item of list(items)) {
      const where = bucketOf(item);
      if (where.kind === 'plan') { plan.push(item); continue; }
      if (where.kind === 'loose') { loose.push(item); continue; }
      if (!byGroup.has(where.id)) {
        byGroup.set(where.id, []);
        if (!catalog.has(where.id)) unknown.push(where.id);
      }
      byGroup.get(where.id).push(item);
    }
    const sections = [];
    for (const group of list(groups)) {
      if (!group || typeof group.id !== 'string' || !group.id) continue;
      const rows = byGroup.get(group.id);
      if (!rows || !rows.length) continue;
      const title = typeof group.title === 'string' && group.title ? group.title : group.id;
      const goal = typeof group.goal === 'string' ? group.goal : '';
      sections.push({ kind: 'group', id: group.id, title, goal, items: rows });
      byGroup.delete(group.id);
    }
    for (const id of unknown) {
      const rows = byGroup.get(id);
      if (rows && rows.length) sections.push({ kind: 'group', id, title: id, goal: '', items: rows });
    }
    if (plan.length) sections.push({ kind: 'plan', id: null, title: 'Plan', goal: '', items: plan });
    if (loose.length) sections.push({ kind: 'loose', id: null, title: LOOSE_TITLE, goal: '', items: loose });
    return sections;
  }

  // The propose button is only on an answered wave or plan question. A task question has no scope.
  function canPropose(question) {
    if (!question || typeof question !== 'object') return false;
    if (question.scope !== 'wave' && question.scope !== 'plan') return false;
    return typeof question.answer === 'string' && question.answer.length > 0;
  }

  // "affected by p3", or "flagged: already started" when the task had already started. Otherwise null.
  function affectedLabel(task) {
    const mark = task && task.affected;
    if (!mark || typeof mark !== 'object') return null;
    if (mark.flag_only === true) return 'flagged: already started';
    if (typeof mark.pid === 'string' && mark.pid) return 'affected by ' + mark.pid;
    return null;
  }

  function affectedBadge(task) {
    const label = affectedLabel(task);
    if (!label) return null;
    const node = el('span', label, 'tag brief-affected');
    const reason = task.affected && typeof task.affected.reason === 'string' ? task.affected.reason : '';
    if (reason) node.title = reason;
    return node;
  }

  function flags(question) {
    const row = el('div', undefined, 'brief-flags');
    if (question && question.must_answer === true) row.append(el('span', 'must answer', 'tag'));
    if (question && question.possibly_stale === true) row.append(el('span', 'possibly stale', 'tag'));
    return row.childElementCount ? row : null;
  }

  function noticeNode(text) {
    const pre = el('pre', text, 'brief-notice');
    pre.setAttribute('role', 'status');
    return pre;
  }

  function paintNotice(box, text) {
    const card = box && box.parentElement;
    if (!card) return;
    const node = noticeNode(text);
    const old = card.querySelector(':scope > .brief-notice');
    if (old) old.replaceWith(node);
    else card.insertBefore(node, box);
  }

  function clearNotice(box) {
    const old = box && box.parentElement && box.parentElement.querySelector(':scope > .brief-notice');
    if (old) old.remove();
  }

  function setBusy(node, on) {
    if (!node) return;
    if (on) {
      node.dataset.busy = '1';
      node.setAttribute('aria-busy', 'true');
    } else {
      delete node.dataset.busy;
      node.removeAttribute('aria-busy');
    }
    if (!node.querySelectorAll) return;
    for (const item of node.querySelectorAll('button, textarea, input')) item.disabled = !!on;
  }

  function outputOf(res) {
    const data = res && res.data;
    return data && typeof data.output === 'string' ? data.output : '';
  }

  async function postJson(path, body) {
    const ui = forgeUI();
    let res = { ok: false, error: 'unavailable', detail: 'this page cannot send' };
    try {
      if (ui && ui.post) res = await ui.post(path, body);
    } catch (_) {
      res = { ok: false, error: 'failed', detail: 'could not send' };
    }
    return res;
  }

  function answerBox(ctx, task, question) {
    const run = ctx.run || {};
    const box = el('div', undefined, 'brief-box');
    box.dataset.hold = 'brief:' + run.key + ':' + task.id + ':' + question.id;
    const field = el('textarea');
    field.setAttribute('aria-label', 'Answer');
    field.maxLength = 4096;
    field.rows = 3;
    field.autocomplete = 'off';
    const send = el('button', 'Send', 'brief-send');
    send.type = 'button';
    const msgs = el('div', undefined, 'brief-msgs');
    const mark = () => { box.dataset.editing = '1'; };
    field.addEventListener('focus', mark);
    field.addEventListener('input', mark);
    field.addEventListener('blur', () => {
      if (box.dataset.busy) return;
      if (!field.value.trim()) delete box.dataset.editing;
    });
    send.addEventListener('click', () => sendAnswer(box, field, send, msgs, ctx, task, question));
    box.append(field, send, msgs);
    return box;
  }

  async function sendAnswer(box, field, send, msgs, ctx, task, question) {
    if (box.dataset.busy) return;
    const answer = field.value.trim();
    const ui = forgeUI();
    if (!answer) {
      if (ui) ui.outcome(msgs, { ok: false, status: 400, error: 'invalid', detail: 'the answer is empty' });
      return;
    }
    box.dataset.editing = '1';
    box.dataset.busy = '1';
    box.setAttribute('aria-busy', 'true');
    send.disabled = true;
    const run = ctx.run || {};
    const path = '/api/runs/' + encodeURIComponent(run.key) + '/tasks/' + encodeURIComponent(task.id) + '/brief/' + encodeURIComponent(question.id);
    let res = { ok: false, error: 'unavailable', detail: 'this page cannot send' };
    try {
      if (ui && ui.post) res = await ui.post(path, { answer });
    } catch (_) {
      res = { ok: false, error: 'failed', detail: 'could not send the answer' };
    }
    if (!box.isConnected) return;
    delete box.dataset.busy;
    box.removeAttribute('aria-busy');
    send.disabled = false;
    const data = res && res.data;
    const output = data && typeof data.output === 'string' ? data.output : '';
    if (res && res.ok && data && data.exit === 0) {
      const key = noticeKey(run, task, question);
      if (output.includes('forge control add')) {
        notices.set(key, output);
        paintNotice(box, output);
      } else {
        notices.delete(key);
        clearNotice(box);
      }
      delete box.dataset.editing;                          // the redraw shows the recorded answer, not this draft
      if (ctx.refresh) ctx.refresh();
      return;
    }
    if (ui && ui.outcome) ui.outcome(msgs, res);
  }

  async function sendPropose(actions, ctx, task, question) {
    if (actions.dataset.busy) return;
    const run = (ctx && ctx.run) || {};
    setBusy(actions, true);
    const path = '/api/runs/' + encodeURIComponent(run.key) + '/proposals';
    const res = await postJson(path, { task: task.id, qid: question.id });
    if (!actions.isConnected) return;
    const output = outputOf(res);
    // Exit 0 stored a proposal. Exit 3 is also 409 when the run lock is busy, and when the CLI
    // refuses without storing anything (a wave question with no group, a plan question the scope
    // cannot act on, brief off): stdout is empty then. A refused proposal that was stored prints
    // its pid. Only that, and exit 0, are a write the page should redraw. Anything else is an error.
    const pid = /^p[0-9]{1,6}\b/.exec(String(output).trim());
    const wrote = !!(res && (res.ok || (res.status === 409 && pid)));
    if (pid) proposalNotices.set(proposalKey(run.key, pid[0]), output);
    if (wrote && ctx && ctx.refresh) { ctx.refresh(); return; }
    setBusy(actions, false);
    const ui = forgeUI();
    const msgs = actions.querySelector('.brief-msgs');
    if (!wrote && ui && ui.outcome && msgs) ui.outcome(msgs, res);
  }

  function proposeActions(ctx, task, question) {
    const actions = el('div', undefined, 'brief-actions');
    const send = el('button', 'Propose a change', 'brief-propose');
    send.type = 'button';
    const msgs = el('div', undefined, 'brief-msgs');
    send.addEventListener('click', () => sendPropose(actions, ctx, task, question));
    actions.append(send, msgs);
    return actions;
  }

  function questionCard(question, ctx, task, parked) {
    const li = el('li', undefined, 'brief-q');
    if (question.id) li.dataset.qid = String(question.id);
    li.append(el('p', typeof question.text === 'string' && question.text ? question.text : '—', 'brief-text'));
    if (typeof question.why === 'string' && question.why) li.append(el('p', question.why, 'brief-why muted'));
    const options = list(question.options).filter(item => typeof item === 'string' && item);
    if (options.length) li.append(el('p', options.join(' · '), 'brief-options'));
    if (typeof question.default === 'string' && question.default) li.append(el('p', 'if unanswered: ' + question.default, 'brief-default muted'));
    const marks = flags(question);
    if (marks) li.append(marks);
    if (typeof question.answer === 'string' && question.answer) li.append(el('p', 'Answer: ' + question.answer, 'brief-got'));
    if (parked && typeof question.scope === 'string' && question.scope) li.append(el('p', 'scope: ' + question.scope, 'brief-scope muted'));
    const note = notices.get(noticeKey(ctx && ctx.run, task, question));
    const box = answerBox(ctx, task, question);
    if (note) li.append(noticeNode(note));
    li.append(box);
    if (canPropose(question)) li.append(proposeActions(ctx, task, question));
    return li;
  }

  function sectionHead(section) {
    const title = section && typeof section.title === 'string' ? section.title : '';
    if (!title) return null;
    const block = el('div', undefined, 'brief-group');
    block.append(el('h4', title, 'brief-group-title'));
    if (section.goal) block.append(el('p', section.goal, 'brief-goal'));
    return block;
  }

  function questionList(items, ctx, task, parked) {
    const ul = el('ul', undefined, 'brief-questions');
    for (const item of items) ul.append(questionCard(item.question, ctx, task, parked));
    return ul;
  }

  function appendQuestions(parent, questions, ctx, task, parked) {
    const sections = groupQuestions(questions.map(question => ({ task, question })), (ctx.run || {}).groups);
    const flat = sections.length === 1 && sections[0].kind === 'loose';
    if (flat) { parent.append(questionList(sections[0].items, ctx, task, parked)); return; }
    for (const section of sections) {
      const heading = sectionHead(section);
      if (heading) parent.append(heading);
      parent.append(questionList(section.items, ctx, task, parked));
    }
  }

  // Drop the answer boxes and the write buttons once the page knows it cannot write.
  function settleControls(section) {
    const ui = forgeUI();
    const drop = () => {
      if (!section.isConnected) return;
      for (const box of section.querySelectorAll('.brief-box, .brief-actions')) box.remove();
      section.dataset.controls = 'off';
    };
    if (!ui || !ui.controlsEnabled) { drop(); return; }
    ui.controlsEnabled().then(on => {
      if (!section.isConnected) return;
      if (!on) drop();
      else section.dataset.controls = 'on';
    });
  }

  // The inspector section. Nothing when this task has no brief. Parked questions stay collapsed.
  // An unanswered parked question still has an answer box, so it can be answered and then proposed.
  function taskSection(host, ctx, task) {
    const brief = task && task.brief;
    if (!host || !brief || typeof brief !== 'object') return null;
    const section = el('section', undefined, 'insp-section brief-section');
    section.append(el('h3', 'Questions'));
    const questions = list(brief.questions).filter(item => item && typeof item === 'object');
    const parked = list(brief.parked).filter(item => item && typeof item === 'object');
    if (!questions.length && !parked.length) section.append(el('p', 'No questions.', 'muted'));
    if (questions.length) appendQuestions(section, questions, ctx, task, false);
    if (parked.length) {
      const details = el('details', undefined, 'brief-parked');
      details.dataset.keep = 'brief-parked:' + task.id;
      details.append(el('summary', 'Parked (wider scope)'));
      appendQuestions(details, parked, ctx, task, true);
      section.append(details);
    }
    host.append(section);
    settleControls(section);
    return section;
  }

  function openItem(item, spec) {
    const run = spec.run;
    const task = item.task, question = item.question;
    const li = el('li');
    const link = el('a', undefined, 'brief-open');
    link.href = typeof spec.href === 'function' ? spec.href(task.id) : '#';
    const top = el('span', undefined, 'brief-open-top');
    top.append(el('span', task.id, 'mono brief-open-task'), el('span', typeof question.text === 'string' && question.text ? question.text : '—', 'brief-open-text'));
    link.append(top);
    const marks = flags(question);
    if (marks) link.append(marks);
    const when = startLine({ deps: waitingDeps(task, run && run.tasks), start: projectionStart(spec.projections, task), now: spec.now, status: task.status });
    if (when) link.append(el('span', when, 'brief-when'));
    li.append(link);
    return li;
  }

  function drawDiff(proposal) {
    const box = el('div', undefined, 'brief-patch');
    const Diff = (typeof globalThis !== 'undefined' && globalThis.ForgeDiff) || (root && root.ForgeDiff);
    const text = typeof proposal.diff === 'string' ? proposal.diff : '';
    const opts = { truncated: proposal.truncated === true };
    if (Diff && typeof Diff.renderPatch === 'function') Diff.renderPatch(box, text, opts);
    else {
      box.append(el('pre', text, 'mono brief-diff'));
      if (opts.truncated) box.append(el('p', 'This diff was cut short.', 'warn'));
    }
    return box;
  }

  async function sendDecision(card, spec, proposal, kind, reasonField) {
    if (card.dataset.busy) return;
    const run = (spec && spec.run) || {};
    const base = '/api/runs/' + encodeURIComponent(run.key) + '/proposals/' + encodeURIComponent(proposal.pid) + '/';
    let path, body;
    if (kind === 'apply') {
      path = base + 'apply';
      body = { hash: proposal.hash };
    } else {
      path = base + 'reject';
      const reason = reasonField && typeof reasonField.value === 'string' ? reasonField.value.trim() : '';
      body = reason ? { reason } : {};
    }
    setBusy(card, true);
    const res = await postJson(path, body);
    if (!card.isConnected) return;
    const output = outputOf(res);
    if (output) proposalNotices.set(proposalKey(run.key, proposal.pid), output);
    if (res && res.ok && spec && spec.refresh) { spec.refresh(); return; }
    setBusy(card, false);
    const ui = forgeUI();
    const msgs = card.querySelector('.brief-msgs');
    if (!(res && res.ok) && ui && ui.outcome && msgs) ui.outcome(msgs, res);
  }

  function proposalActions(card, spec, proposal) {
    const actions = el('div', undefined, 'brief-actions');
    const msgs = el('div', undefined, 'brief-msgs');
    if (typeof proposal.hash === 'string' && proposal.hash) {
      const approve = el('button', 'Approve', 'brief-approve');
      approve.type = 'button';
      approve.addEventListener('click', () => sendDecision(card, spec, proposal, 'apply'));
      actions.append(approve);
    }
    const reject = el('button', 'Reject', 'brief-reject');
    reject.type = 'button';
    const reason = el('input');
    reason.type = 'text';
    reason.maxLength = 500;
    reason.setAttribute('aria-label', 'Reject reason');
    reason.placeholder = 'Reason (optional)';
    reject.addEventListener('click', () => sendDecision(card, spec, proposal, 'reject', reason));
    actions.append(reject, reason, msgs);
    return actions;
  }

  function proposalCard(proposal, spec) {
    const card = el('article', undefined, 'brief-proposal');
    if (proposal.pid) card.dataset.pid = String(proposal.pid);
    const top = el('div', undefined, 'brief-proposal-top');
    top.append(el('span', typeof proposal.state === 'string' && proposal.state ? proposal.state : '—', 'tag brief-state'));
    top.append(el('span', typeof proposal.scope === 'string' && proposal.scope ? proposal.scope : '—', 'tag'));
    const group = typeof proposal.group === 'string' && proposal.group ? proposal.group : '—';
    top.append(el('span', group, 'brief-proposal-group'));
    card.append(top);
    card.append(el('p', typeof proposal.summary === 'string' && proposal.summary ? proposal.summary : '—', 'brief-summary'));
    if (proposal.state === 'refused' && typeof proposal.reason === 'string' && proposal.reason) card.append(el('p', proposal.reason, 'brief-reason'));
    if (typeof proposal.hash === 'string' && proposal.hash) card.append(el('code', proposal.hash, 'mono brief-hash'));
    card.append(drawDiff(proposal));
    const run = spec && spec.run;
    const stored = proposalNotices.get(proposalKey(run && run.key, proposal.pid));
    if (stored) card.append(noticeNode(stored));
    if (proposal.state === 'proposed') card.append(proposalActions(card, spec, proposal));
    return card;
  }

  function proposalsBlock(proposals, spec) {
    const wrap = el('div', undefined, 'brief-proposals');
    const head = el('p', undefined, 'brief-list-head');
    head.append(el('span', 'Proposals', 'plate'), el('span', String(proposals.length), 'module-count'));
    wrap.append(head);
    for (const proposal of proposals) wrap.append(proposalCard(proposal, spec));
    return wrap;
  }

  // The run's open questions, grouped when a group or a plan section exists, and its proposals.
  // All-loose questions stay the flat list the run page had. Beside another section they sit under
  // "No group", not under the previous heading. `href(taskId)` opens that task's inspector.
  function renderList(host, spec) {
    spec = spec || {};
    if (!host) return;
    host.replaceChildren();
    if (host.dataset) delete host.dataset.proposals;
    const run = spec.run;
    const open = orderQuestions(openQuestions(run), spec.projections);
    const proposals = list(run && run.proposals).filter(item => item && typeof item === 'object');
    if (!open.length && !proposals.length) return;
    if (proposals.length && host.dataset) host.dataset.proposals = '1';
    if (open.length) {
      const head = el('p', undefined, 'brief-list-head');
      head.append(el('span', 'Questions', 'plate'), el('span', String(open.length), 'module-count'));
      const ul = el('ul', undefined, 'brief-list');
      ul.dataset.scroll = 'brief-list';
      const sections = groupQuestions(open, run && run.groups);
      const flat = sections.length === 1 && sections[0].kind === 'loose';
      if (flat) {
        for (const item of sections[0].items) ul.append(openItem(item, spec));
      } else {
        for (const section of sections) {
          const li = el('li');
          const heading = sectionHead(section);
          if (heading) li.append(heading);
          if (li.childElementCount) ul.append(li);
          for (const item of section.items) ul.append(openItem(item, spec));
        }
      }
      host.append(head, ul);
    }
    if (proposals.length) {
      const block = proposalsBlock(proposals, spec);
      host.append(block);
      settleControls(block);
    }
  }

  return {
    openQuestions, orderQuestions, startLine, renderList, taskSection,
    groupQuestions, canPropose, affectedLabel, affectedBadge,
  };
});
