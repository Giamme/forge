/* forge-ui brief questions (#135): a task's questions in the inspector, and the run's open questions.
   window.ForgeBrief, and a CommonJS module under node:test. The pure helpers (openQuestions, orderQuestions,
   startLine) are tested in ui/tests/js/briefview.test.js. Question text, answers and the late-answer notice
   are untrusted: they only ever enter the page as text.
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
  const notices = new Map();                                 // run\ntask\nqid → the late answer's stdout

  function el(tag, text, className) {
    const node = root.document.createElement(tag);
    if (text !== undefined) node.textContent = text === null || text === undefined ? '' : String(text);
    if (className) node.className = className;
    return node;
  }

  function noticeKey(run, task, question) {
    return String(run && run.key) + '\n' + String(task && task.id) + '\n' + String(question && question.id);
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
    const ui = root.ForgeUI;
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
    if (!parked && typeof question.answer === 'string' && question.answer) li.append(el('p', 'Answer: ' + question.answer, 'brief-got'));
    if (parked && typeof question.scope === 'string' && question.scope) li.append(el('p', 'scope: ' + question.scope, 'brief-scope muted'));
    if (!parked) {
      const note = notices.get(noticeKey(ctx && ctx.run, task, question));
      const box = answerBox(ctx, task, question);
      if (note) li.append(noticeNode(note));
      li.append(box);
    }
    return li;
  }

  // Drop the answer boxes once the page knows it cannot write. The promise is cached, so a redraw settles at once.
  function settleControls(section) {
    const ui = root.ForgeUI;
    const drop = () => {
      if (!section.isConnected) return;
      for (const box of section.querySelectorAll('.brief-box')) box.remove();
      section.dataset.controls = 'off';
    };
    if (!ui || !ui.controlsEnabled) { drop(); return; }
    ui.controlsEnabled().then(on => {
      if (!section.isConnected) return;
      if (!on) drop();
      else section.dataset.controls = 'on';
    });
  }

  // The inspector section. Nothing when this task has no brief. Parked questions stay collapsed, with no answer box.
  function taskSection(host, ctx, task) {
    const brief = task && task.brief;
    if (!host || !brief || typeof brief !== 'object') return null;
    const section = el('section', undefined, 'insp-section brief-section');
    section.append(el('h3', 'Questions'));
    const questions = list(brief.questions).filter(item => item && typeof item === 'object');
    const parked = list(brief.parked).filter(item => item && typeof item === 'object');
    if (!questions.length && !parked.length) section.append(el('p', 'No questions.', 'muted'));
    if (questions.length) {
      const ul = el('ul', undefined, 'brief-questions');
      for (const question of questions) ul.append(questionCard(question, ctx, task, false));
      section.append(ul);
    }
    if (parked.length) {
      const details = el('details', undefined, 'brief-parked');
      details.dataset.keep = 'brief-parked:' + task.id;
      const ul = el('ul', undefined, 'brief-questions');
      for (const question of parked) ul.append(questionCard(question, ctx, task, true));
      details.append(el('summary', 'Parked (wider scope)'), ul);
      section.append(details);
    }
    host.append(section);
    settleControls(section);
    return section;
  }

  // The run's open questions. `href(taskId)` is the link that opens that task's inspector.
  function renderList(host, spec) {
    spec = spec || {};
    if (!host) return;
    host.replaceChildren();
    const run = spec.run;
    const open = orderQuestions(openQuestions(run), spec.projections);
    if (!open.length) return;
    const head = el('p', undefined, 'brief-list-head');
    head.append(el('span', 'Questions', 'plate'), el('span', String(open.length), 'module-count'));
    const ul = el('ul', undefined, 'brief-list');
    ul.dataset.scroll = 'brief-list';
    for (const item of open) {
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
      ul.append(li);
    }
    host.append(head, ul);
  }

  return { openQuestions, orderQuestions, startLine, renderList, taskSection };
});
