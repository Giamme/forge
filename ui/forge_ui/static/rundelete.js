/* forge-ui delete-run dialog (window.ForgeRunDelete, and a CommonJS module under node:test). A "Delete run…" key in the
   run header opens a modal: it dry-runs `forge runs rm` on open and whenever an option changes, shows what would go
   (size, archive, steps, worktrees, branches), and deletes only after the run id has been typed exactly.
   The pure helpers (request body, option state, confirm gate, warnings, plates, response decision) are tested in
   ui/tests/js/rundelete.test.js; open(run, ctx) is thin DOM over them. Every value from the CLI enters the page as text.
   ctx = { onDeleted?() } is called after a full success (the page then shows the run list again).
   The write call is ForgeUI's: POST /api/runs/<key>/delete, see ui/docs/api.md, "Controls (write endpoints)". */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeRunDelete = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const object = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const text = v => typeof v === 'string' ? v : '';

  /* ---------- pure ---------- */
  // The POST body. Archive is on unless it is explicitly off; the rest are on only when asked for.
  function deleteBody(o) {
    const x = o || {};
    return { dry_run: !!x.dryRun, archive: x.archive !== false, worktrees: !!x.worktrees, branches: !!x.branches };
  }

  // The dialog's state: the three options and what has been typed. Never starts with anything typed.
  const OPTIONS = ['archive', 'worktrees', 'branches'];
  const initial = runId => ({ runId: text(runId), archive: true, worktrees: false, branches: false, typed: '' });
  // A changed option clears the typed confirmation (the dry run it answered is out of date); an unchanged one does not.
  function withOption(s, name, value) {
    if (!OPTIONS.includes(name) || s[name] === !!value) return s;
    return { ...s, [name]: !!value, typed: '' };
  }
  const withTyped = (s, typed) => ({ ...s, typed: text(typed) });
  // The options as the dry run / delete body is built from them.
  const bodyOf = (s, dryRun) => deleteBody({ dryRun, archive: s.archive, worktrees: s.worktrees, branches: s.branches });

  // The confirm gate: the run id, character for character (no trimming, no case folding).
  const confirmOk = (typed, runId) => typeof typed === 'string' && typeof runId === 'string' && runId !== '' && typed === runId;
  // Delete is armed by the exact id AND a dry run that answers for the options as they stand now.
  const canDelete = (s, preview, busy) => !!preview && !busy && confirmOk(s.typed, s.runId);

  const unmerged = result => list(object(result) && result.branches).filter(b => b && String(b.status).toUpperCase() === 'UNMERGED' && !b.checked_out);
  const dirtyRun = result => list(object(result) && result.worktrees).filter(w => w && w.kind !== 'foreign' && w.dirty);

  // Warnings for a dry-run result under the options as they stand: [{id, tone: 'warn' | 'note', text, names}].
  // 'warn' is what the delete would do; 'note' is a risk the options do not take.
  function warningsOf(result, opts) {
    const o = opts || {}, out = [];
    if (o.archive === false) {
      out.push({ id: 'no-archive', tone: 'warn', names: [],
        text: 'Without the archive the transcripts and run files are lost for good. The dataset events are still preserved first.' });
    }
    const bad = unmerged(result).map(b => text(b.name));
    if (bad.length) {
      out.push({ id: 'unmerged', tone: o.branches ? 'warn' : 'note', names: bad,
        text: o.branches ? 'Deleting these branches drops commits that are merged nowhere: ' + bad.join(', ')
          : 'These branches are not merged and are kept: ' + bad.join(', ') });
    }
    const dirty = dirtyRun(result).map(w => text(w.path));
    if (dirty.length) {
      out.push({ id: 'dirty', tone: o.worktrees ? 'warn' : 'note', names: dirty,
        text: o.worktrees ? 'These worktrees have uncommitted changes and will not be removed, so the run directory is kept too: ' + dirty.join(', ')
          : 'These worktrees have uncommitted changes and are never removed: ' + dirty.join(', ') });
    }
    return out;
  }

  // The plate of a worktree: run | dirty | foreign, and what happens to it.
  function worktreePlate(w) {
    if (w && w.kind === 'foreign') return { id: 'foreign', label: 'foreign', note: 'never touched' };
    if (w && w.dirty) return { id: 'dirty', label: 'dirty', note: 'will not be removed' };
    return { id: 'run', label: 'run', note: '' };
  }
  // The plate of a branch: merged | UNMERGED.
  function branchPlate(b) {
    const un = !!b && String(b.status).toUpperCase() === 'UNMERGED';
    return { id: un ? 'unmerged' : 'merged', label: un ? 'UNMERGED' : 'merged', note: b && b.checked_out ? 'checked out: kept' : '' };
  }

  function stepsOf(result) {
    return list(object(result) && result.steps).filter(object).map(s => ({ name: text(s.step || s.name), ok: s.ok !== false, detail: text(s.detail) }));
  }

  // What a POST answer means. kind: 'success' (a full success), 'partial' (the CLI failed part-way: steps and guidance),
  // 'busy' (409: active or busy), 'error' (anything else: the page prints it with ForgeUI.outcome).
  function outcomeOf(res) {
    const r = res || {};
    if (r.ok && object(r.data) && r.data.ok !== false) {
      const result = r.data;
      return { kind: 'success', result, steps: stepsOf(result), failed: [], guidance: '' };
    }
    if (r.ok) {
      const steps = stepsOf(r.data);
      return { kind: 'partial', result: r.data, steps, failed: steps.filter(s => !s.ok), guidance: '' };
    }
    if (r.status === 409) {
      return { kind: 'busy', text: r.error === 'busy' ? 'busy: another change to this run is in progress, try again' : text(r.detail) || 'the run is active' };
    }
    const result = object(r.result);
    if (r.status === 500 && result) {
      const steps = stepsOf(result);
      return { kind: 'partial', result, steps, failed: steps.filter(s => !s.ok), guidance: text(r.detail) };
    }
    return { kind: 'error', text: text(r.detail) || text(r.error) || 'failed' };
  }

  // What a dry-run answer means: 'preview' with the result (and whether the real run would complete, with the CLI's
  // words when it would not), 'busy', or 'error'. A dry run that reports failure still describes what it would do.
  function previewOf(res) {
    const r = res || {};
    if (r.ok && object(r.data)) return { kind: 'preview', result: r.data, completes: r.data.ok !== false, note: '' };
    if (r.status === 409) return { kind: 'busy', text: outcomeOf(r).text };
    if (r.status === 500 && object(r.result)) return { kind: 'preview', result: r.result, completes: false, note: text(r.detail) };
    return { kind: 'error', text: text(r.detail) || text(r.error) || 'failed' };
  }

  function fmtBytes(n) {
    if (typeof n !== 'number' || !isFinite(n) || n < 0) return 'unknown';
    let v = n;
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (v < 1024 || unit === 'GB') return unit === 'B' ? v + ' B' : v.toFixed(1) + ' ' + unit;
      v /= 1024;
    }
    return 'unknown';
  }

  const deletePath = key => '/api/runs/' + encodeURIComponent(key) + '/delete';

  /* ---------- DOM ---------- */
  let current = null;   // the one dialog that may be open

  function open(run, ctx) {
    const UI = root.ForgeUI;
    if (!UI || !run || !run.key) return null;
    if (current) current.close();
    const c = ctx || {};
    const el = (tag, t, cls) => UI.el(tag, t, cls);
    const s = { v: initial(run.run_id), preview: null, seq: 0, loading: false, posting: false, done: false };
    const lead = text(run.goal).split('\n')[0];

    const dlg = el('dialog', undefined, 'del-dialog'); dlg.setAttribute('aria-labelledby', 'del-title');
    const title = el('h2', 'Delete this run', 'del-title'); title.id = 'del-title';
    const ident = el('p', undefined, 'del-ident');
    ident.append(el('code', run.run_id || run.key, 'del-id'), el('span', lead, 'muted'));

    const lamp = el('p', undefined, 'del-status'); lamp.setAttribute('role', 'status'); lamp.setAttribute('aria-live', 'polite');
    const previewHost = el('div', undefined, 'del-preview');

    const optHost = el('fieldset', undefined, 'del-options'); optHost.append(el('legend', 'Options', 'plate'));
    const boxes = {};
    const option = (name, label, help) => {
      const l = el('label', undefined, 'del-opt'), box = el('input'); box.type = 'checkbox'; box.checked = s.v[name];
      const t = el('span'); t.append(el('strong', label), el('span', help, 'muted'));
      l.append(box, t); optHost.append(l); boxes[name] = box;
      box.addEventListener('change', () => { s.v = withOption(s.v, name, box.checked); confirmInput.value = ''; load(); syncAll(); });
    };
    option('archive', 'Archive transcripts and run files for the dataset', 'A tar.gz under the dataset home. The dataset events themselves are always preserved first.');
    option('worktrees', 'Remove worktrees', 'Only the run\'s own clean worktrees. Foreign and dirty ones are never touched.');
    option('branches', 'Delete branches', 'The run\'s branches that are not checked out anywhere.');
    const warnHost = el('ul', undefined, 'del-warnings');

    const confirmBox = el('div', undefined, 'del-confirm');
    const confirmLabel = el('label', undefined, 'del-confirm-label'); confirmLabel.htmlFor = 'del-confirm-input';
    confirmLabel.append(document.createTextNode('To delete, type the run id: '), el('code', run.run_id || '', 'del-id'));
    const confirmInput = el('input', undefined, 'del-confirm-input'); confirmInput.id = 'del-confirm-input'; confirmInput.type = 'text';
    confirmInput.autocomplete = 'off'; confirmInput.spellcheck = false; confirmInput.autocapitalize = 'off'; confirmInput.value = '';
    confirmBox.append(confirmLabel, confirmInput);

    const resultHost = el('div', undefined, 'del-result');
    const cancel = el('button', 'Cancel'); cancel.type = 'button';
    const go = el('button', 'Delete', 'ui-danger del-go'); go.type = 'button'; go.disabled = true;
    const actions = el('div', undefined, 'del-actions'); actions.append(cancel, go);
    dlg.append(title, ident, lamp, previewHost, optHost, warnHost, confirmBox, resultHost, actions);

    function setLamp(cls, t) { lamp.replaceChildren(el('i', undefined, 'lamp ' + cls), el('span', t)); }
    function row(host, plate, label, note) {
      const li = el('li', undefined, 'del-row');
      li.append(el('span', plate.label, 'plate del-plate del-plate-' + plate.id), el('span', label, 'del-row-text mono'));
      if (note) li.append(el('span', note, 'muted del-row-note'));
      host.append(li);
    }
    function section(name, body) {
      const sec = el('section', undefined, 'del-section');
      sec.append(el('h3', name, 'plate'), body);
      return sec;
    }

    function drawPreview() {
      const p = s.preview, r = p && p.result;
      if (!r) { previewHost.replaceChildren(); return; }
      const facts = el('dl', undefined, 'del-facts');
      const fact = (k, v) => { facts.append(el('dt', k), el('dd', v, 'mono')); };
      fact('Size', fmtBytes(r.size_bytes));
      if (s.v.archive) fact('Archive', (r.archive ? text(r.archive) : 'under the dataset home') + ' · about ' + fmtBytes(r.archive_estimate_bytes));
      else fact('Archive', 'none');
      const parts = [section('Would delete', facts)];
      const steps = stepsOf(r);
      if (steps.length) {
        const ul = el('ul', undefined, 'del-steps');
        for (const st of steps) {
          const li = el('li', undefined, 'del-step');
          li.append(el('i', undefined, 'lamp ' + (st.ok ? 'st-pass' : 'st-fail')), el('span', st.name, 'mono del-step-name'), el('span', st.detail, 'muted'));
          ul.append(li);
        }
        parts.push(section('Steps', ul));
      }
      const wts = list(r.worktrees).filter(object);
      if (wts.length) {
        const ul = el('ul', undefined, 'del-list');
        for (const w of wts) { const pl = worktreePlate(w); row(ul, pl, text(w.path) + (w.branch ? '  ' + text(w.branch) : ''), pl.note || text(w.note)); }
        parts.push(section('Worktrees', ul));
      }
      const brs = list(r.branches).filter(object);
      if (brs.length) {
        const ul = el('ul', undefined, 'del-list');
        for (const b of brs) { const pl = branchPlate(b); row(ul, pl, text(b.name), pl.note || text(b.note)); }
        parts.push(section('Branches', ul));
      }
      previewHost.replaceChildren(...parts);
    }

    function drawWarnings() {
      const ws = warningsOf(s.preview && s.preview.result, s.v);
      warnHost.replaceChildren(...ws.map(w => {
        const li = el('li', undefined, 'del-warning del-warning-' + w.tone);
        li.append(el('i', undefined, 'lamp ' + (w.tone === 'warn' ? 'st-fail' : 'st-running')), el('span', w.text));
        return li;
      }));
      if (s.preview && !s.preview.completes) {
        const li = el('li', undefined, 'del-warning del-warning-warn');
        li.append(el('i', undefined, 'lamp st-fail'), el('span', 'The dry run says a real delete would not complete' + (s.preview.note ? ': ' + s.preview.note : '') + '. See the steps above.'));
        warnHost.append(li);
      }
    }

    function syncAll() {
      for (const name of OPTIONS) boxes[name].checked = s.v[name];
      for (const b of Object.values(boxes)) b.disabled = s.posting || s.done;
      confirmInput.disabled = s.posting || s.done || !s.preview;
      go.disabled = s.done || !canDelete(s.v, s.preview, s.loading || s.posting);
      go.textContent = s.posting ? 'Deleting…' : 'Delete';
      go.hidden = s.done;
      cancel.textContent = s.done ? 'Close' : 'Cancel';
      cancel.disabled = s.posting;
      drawWarnings();
    }

    // The dry run for the options as they are now. A later call makes an earlier answer stale.
    async function load(keepResult) {
      const seq = ++s.seq;
      s.loading = true; s.preview = null;
      if (!keepResult) resultHost.replaceChildren();
      previewHost.replaceChildren(); setLamp('st-running', 'Checking what would be deleted…'); syncAll();
      const res = await UI.post(deletePath(run.key), bodyOf(s.v, true));
      if (seq !== s.seq || s.done) return;
      s.loading = false;
      const p = previewOf(res);
      if (p.kind === 'preview') {
        s.preview = p;
        setLamp(p.completes ? 'st-pass' : 'st-fail', p.completes ? 'Dry run: nothing has been changed yet.' : 'Dry run: nothing has been changed yet, but it would not complete.');
        drawPreview();
      } else {
        setLamp('st-fail', p.kind === 'busy' ? p.text : 'Could not check: ' + (res.status === 404 ? 'this run no longer exists' : p.text));
      }
      syncAll();
    }

    function showResult(o, res) {
      resultHost.replaceChildren();
      if (o.kind === 'error') { UI.outcome(resultHost, res); return; }
      if (o.kind === 'busy') { UI.outcome(resultHost, { ok: false, status: 0, error: '', detail: 'Not deleted: ' + o.text }); return; }
      const box = el('div', undefined, 'del-outcome');
      box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
      const head = el('p', undefined, 'del-outcome-head');
      head.append(el('i', undefined, 'lamp ' + (o.kind === 'success' ? 'st-pass' : 'st-fail')),
        el('strong', o.kind === 'success' ? 'Run deleted' : 'Delete did not complete: ' + o.failed.length + ' step' + (o.failed.length === 1 ? '' : 's') + ' failed'));
      box.append(head);
      const ul = el('ul', undefined, 'del-steps');
      for (const st of o.steps) {
        const li = el('li', undefined, 'del-step');
        li.append(el('i', undefined, 'lamp ' + (st.ok ? 'st-pass' : 'st-fail')), el('span', st.name, 'mono del-step-name'), el('span', st.detail, st.ok ? 'muted' : 'del-step-fail'));
        ul.append(li);
      }
      box.append(ul);
      if (o.kind === 'success' && o.result.archive) box.append(el('p', 'Archive: ' + text(o.result.archive), 'mono del-archive'));
      if (o.kind === 'partial') {
        if (o.guidance) box.append(el('p', o.guidance, 'del-guidance'));
        box.append(el('p', 'Fix what failed, then delete again: the same command finishes the job.', 'muted'));
      }
      resultHost.append(box);
    }

    async function submit() {
      if (!canDelete(s.v, s.preview, s.loading || s.posting)) return;
      s.posting = true; syncAll();
      const res = await UI.post(deletePath(run.key), bodyOf(s.v, false));
      s.posting = false;
      const o = outcomeOf(res);
      showResult(o, res);
      if (o.kind === 'success') {
        s.done = true; s.seq++;
        setLamp('st-pass', 'Deleted.');
        previewHost.replaceChildren(); warnHost.replaceChildren();
        syncAll(); cancel.focus();
        if (c.onDeleted) c.onDeleted(run);
        return;
      }
      s.v = withTyped(s.v, ''); confirmInput.value = '';
      if (o.kind === 'partial') load(true);   // what is left now; the result stays on screen
      else syncAll();
    }

    confirmInput.addEventListener('input', () => { s.v = withTyped(s.v, confirmInput.value); syncAll(); });
    confirmInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); if (!go.disabled) submit(); } });
    go.addEventListener('click', submit);
    cancel.addEventListener('click', () => { if (!s.posting) dlg.close(); });
    dlg.addEventListener('cancel', e => { if (s.posting) e.preventDefault(); });
    dlg.addEventListener('click', e => { if (e.target === dlg && !s.posting) dlg.close(); });
    dlg.addEventListener('close', () => { s.seq++; dlg.remove(); if (current && current.dlg === dlg) current = null; });

    document.body.append(dlg);
    current = { dlg, close: () => { if (!s.posting) dlg.close(); } };
    dlg.showModal();
    syncAll(); load();
    return dlg;
  }

  return { open, deleteBody, initial, withOption, withTyped, bodyOf, confirmOk, canDelete, warningsOf, worktreePlate, branchPlate,
    stepsOf, outcomeOf, previewOf, fmtBytes, deletePath, OPTIONS };
});
