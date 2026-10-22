/* forge-ui settings view (window.ForgeSettings, and a CommonJS module under node:test). Route #/settings: the model
   registry (read-only), Jev, the three per-repo .forge files of one repository, the dataset and the FORGE_* environment.
   The pure helpers (request bodies, value checks, the repository picker, the editors' unsaved-text state) are tested in
   ui/tests/js/settings.test.js; render(host, ctx) is thin DOM over them. Every value from the server enters the page as text.
   ctx = { key (the run whose repo to show, or null), runs() (the run list), onPick?(key) }. render returns { poll() }:
   the page calls it on each refresh, and unsaved editor text survives it.
   The calls are GET /api/config?run=<key> and POST /api/config, see ui/docs/api.md, "Controls (write endpoints)". A write
   changes a file in the repository's working tree (or forge's own Jev config) and is only offered with controls on. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeSettings = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const FILES = ['verify', 'setup', 'verify-subset'];
  const VALUE_MAX = 4096;
  const POLL_MS = 20000;                                         // the config rarely changes, and each read starts a CLI process
  const list = v => Array.isArray(v) ? v : [];
  const object = v => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  const text = v => typeof v === 'string' ? v : '';
  const basename = p => String(p || '').replace(/\/+$/, '').split('/').pop() || String(p || '');

  /* ---------- pure ---------- */
  // The POST body: a set carries `value`, an unset carries `unset: true`, never both. jev.enabled's value is the text
  // 'true' or 'false', as the CLI takes it.
  function configBody(run, key, value, unset) {
    if (unset) return { run: String(run), key: String(key), unset: true };
    return { run: String(run), key: String(key), value: String(value) };
  }
  const repoKey = name => 'repo.' + name;
  // The file a save or remove touches, as the confirm names it.
  const targetPath = (repo, name) => String(repo || '').replace(/\/+$/, '') + '/.forge/' + name;

  const subsetOk = value => typeof value === 'string' && value.includes('{files}');
  // Why the server would refuse this text for this file, or null: empty, too long, a control character other than
  // newline and tab, a leading '-', or (verify-subset) no {files}.
  function valueProblem(name, value) {
    const v = text(value);
    if (!v.trim()) return 'Empty: use Remove to delete the file.';
    const chars = Array.from(v).length;                         // code points, as the server counts them
    if (chars > VALUE_MAX) return `Too long: ${chars} of ${VALUE_MAX} characters.`;
    if (/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(v)) return 'The text has an unpaired surrogate, which cannot be saved.';
    if (/[\u0000-\u0008\u000b-\u001f\u007f]/.test(v)) return 'Control characters are not allowed (newline and tab are).';
    if (v.startsWith('-')) return 'The text cannot start with "-".';
    if (name === 'verify-subset' && !subsetOk(v)) return 'verify-subset must contain {files}: the per-file command needs the place for the file list.';
    return null;
  }

  // The runs a repository can be taken from (those that name one), newest first.
  const withRepo = runs => list(runs).filter(r => r && r.key != null && typeof r.repo === 'string' && r.repo);
  const newestFirst = (a, b) => (Number(b.updated) || 0) - (Number(a.updated) || 0);
  // The run whose repository the page opens on: the current run when it is in the list, else the most recent one.
  function pickDefault(runs, current) {
    const usable = withRepo(runs);
    if (current != null && usable.some(r => r.key === current)) return current;
    const latest = usable.slice().sort(newestFirst)[0];
    return latest ? latest.key : null;
  }
  // The choices grouped by repository: [{repo, name, runs}], the repository of the newest run first.
  function groupRuns(runs) {
    const groups = new Map();
    for (const r of withRepo(runs).sort(newestFirst)) {
      if (!groups.has(r.repo)) groups.set(r.repo, { repo: r.repo, name: basename(r.repo), runs: [] });
      groups.get(r.repo).runs.push(r);
    }
    return [...groups.values()];
  }
  const optionLabel = r => {
    const goal = text(r.goal).split('\n')[0].trim();
    return [r.run_id || r.key, goal.length > 60 ? goal.slice(0, 59) + '…' : goal].filter(Boolean).join(' · ');
  };

  // An editor's unsaved text. `base` is the file as last read, `draft` the textarea (null: untouched). The CLI ends a saved
  // file with a newline, so trailing newlines do not count as an edit.
  const norm = s => text(s).replace(/\n+$/, '');
  const editorOf = base => ({ base: text(base), draft: null });
  const isDirty = e => !!e && e.draft !== null && norm(e.draft) !== norm(e.base);
  // The editor after the file was read again: untouched or equal-to-disk text follows the disk; unsaved text stays, and
  // `moved` says the file changed under it (the next save overwrites that).
  function dirtyState(editor, content) {
    const e = editor || editorOf(content), fresh = text(content);
    if (!isDirty(e)) return { editor: editorOf(fresh), dirty: false, moved: false };
    return { editor: { base: fresh, draft: e.draft }, dirty: true, moved: norm(fresh) !== norm(e.base) };
  }
  const edited = (editor, draft) => ({ base: editor.base, draft: text(draft) });
  const shown = editor => editor.draft !== null ? editor.draft : editor.base;

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI, c = ctx || {};
    const el = (tag, t, cls) => UI.el(tag, t, cls);
    const s = { run: null, data: null, dataText: null, error: null, writes: false, loading: true, editors: {}, moved: {}, hold: 0, redraw: false, seq: 0, gen: 0, runsSig: null, ready: false, loadedAt: 0 };
    const key_ = (label, cls, onclick) => { const b = el('button', label, cls); b.type = 'button'; if (onclick) b.addEventListener('click', onclick); return b; };

    const head = el('header', undefined, 'set-head');
    head.append(el('h1', 'Settings', 'module-title'),
      el('p', 'What forge reads when it runs: its model registry and Jev switch, and the three files a repository can keep in .forge.', 'muted set-lede'));
    const picker = el('div', undefined, 'set-picker');
    const body = el('div', undefined, 'set-body');
    const note = el('p', 'This page is read-only: this server has no write token, or has controls off.', 'muted set-readonly');
    note.hidden = true;
    host.replaceChildren(head, picker, note, body);

    /* the repository picker */
    function drawPicker() {
      const runs = c.runs ? c.runs() : [];
      const groups = groupRuns(runs);
      const before = s.run;
      if (s.run != null && !withRepo(runs).some(r => r.key === s.run)) s.run = null;
      if (s.run == null) s.run = pickDefault(runs, c.key);
      if (s.run !== before) { resetRun(); if (s.ready) { drawBody(); load(); } }   // the default arrived, or the run went away
      const sig = JSON.stringify([s.run, groups.map(g => [g.repo, g.runs.map(r => [r.key, optionLabel(r)])])]);
      if (sig === s.runsSig) return;
      s.runsSig = sig;
      if (!groups.length) { picker.replaceChildren(el('p', 'No run names a repository yet, so there is nothing to configure.', 'muted')); return; }
      const sel = el('select'); sel.id = 'set-run';
      for (const g of groups) {
        const og = el('optgroup'); og.label = g.name + ' · ' + g.repo;
        for (const r of g.runs) { const o = el('option', optionLabel(r)); o.value = r.key; o.selected = r.key === s.run; og.append(o); }
        sel.append(og);
      }
      sel.addEventListener('change', async () => {
        const unsaved = FILES.filter(n => s.editors[n] && isDirty(s.editors[n]));
        if (unsaved.length && !(await UI.confirmInline(picker, `Switch repository and discard the unsaved edits to ${unsaved.map(n => '.forge/' + n).join(', ')}?`, { danger: true }))) { sel.value = s.run; return; }
        pick(sel.value);
      });
      const lab = el('label', undefined, 'set-field'); lab.htmlFor = 'set-run';
      lab.append(el('span', 'Repository of run', 'plate'));
      const current = withRepo(runs).find(r => r.key === s.run);
      const path = el('p', undefined, 'set-repo-path');
      path.append(el('i', undefined, 'lamp lamp-route'), el('span', current ? current.repo : '', 'mono'));
      const row = el('div', undefined, 'set-picker-row'); row.append(lab, sel);
      picker.replaceChildren(row, path);
    }
    // A new run: nothing of the old one stays, and confirms of the old body (now detached) no longer hold a redraw.
    function resetRun() {
      s.data = null; s.dataText = null; s.error = null; s.editors = {}; s.moved = {}; s.loading = s.run != null;
      s.gen++; s.hold = 0; s.redraw = false;
    }
    function pick(key) {
      s.run = key; resetRun();
      s.runsSig = null; drawPicker(); drawBody();
      if (c.onPick) c.onPick(key);
      load();
    }

    /* loading, and keeping unsaved text across it */
    async function load() {
      const run = s.run, seq = ++s.seq;
      if (run == null) { s.loading = false; drawBody(); return; }
      const res = await UI.get('/api/config?run=' + encodeURIComponent(run));
      if (seq !== s.seq || run !== s.run) return;                 // a later pick or load answered
      s.loading = false; s.loadedAt = Date.now();
      if (!res.ok) {
        const msg = res.status === 0 ? null : res.detail || res.error;
        if (msg === null && s.data) return;                       // a poll that did not reach the server keeps the last good page
        s.error = msg || 'could not reach forge-ui'; s.data = null; s.dataText = null; drawBody(); return;
      }
      s.error = null;
      const files = object(object(res.data.repo).files);
      for (const name of FILES) {
        const next = dirtyState(s.editors[name], object(files[name]).content);
        s.editors[name] = next.editor; s.moved[name] = next.moved || (next.dirty && !!s.moved[name]);
      }
      const t = JSON.stringify(res.data);
      if (t === s.dataText && !s.redraw) return;
      s.data = res.data; s.dataText = t;
      if (s.hold > 0) { s.redraw = true; return; }                // a confirm or a write is open: draw after it
      drawBody();
    }
    // Holds the redraw while a confirm or a write is open; the returned release is a no-op once the run changed.
    function grab() {
      const gen = s.gen; s.hold++;
      return () => { if (gen !== s.gen) return; s.hold--; if (!s.hold && s.redraw) { s.redraw = false; drawBody(); } };
    }

    function drawBody() {
      const focus = document.activeElement && body.contains(document.activeElement) ? document.activeElement : null;
      const at = focus && focus.dataset.editor ? { name: focus.dataset.editor, a: focus.selectionStart, b: focus.selectionEnd } : null;
      if (s.loading) { body.replaceChildren(el('p', 'Reading the configuration…', 'muted')); return; }
      if (s.error) { body.replaceChildren(el('p', 'Could not read the configuration: ' + s.error, 'warn')); return; }
      if (!s.data) { body.replaceChildren(); return; }
      body.replaceChildren(registrySection(), jevSection(), repoSection(), datasetSection(), envSection());
      if (at) {
        const t = body.querySelector(`textarea[data-editor="${at.name}"]`);
        if (t) { t.focus({ preventScroll: true }); try { t.setSelectionRange(at.a, at.b); } catch (_) { /* not selectable */ } }
      }
    }

    /* sections */
    function section(id, title, lede) {
      const sec = el('section', undefined, 'set-section'); sec.id = 'set-' + id;
      const h = el('h2', title, 'plate'); h.id = 'set-' + id + '-h'; sec.setAttribute('aria-labelledby', h.id);
      sec.append(h);
      if (lede) sec.append(el('p', lede, 'muted set-lede'));
      return sec;
    }
    const pathLine = (label, value, cls) => {
      const p = el('p', undefined, 'set-path');
      p.append(el('span', label, 'set-path-label'), el('span', value, 'mono'));
      if (cls) p.append(el('span', cls, 'tag'));
      return p;
    };

    function registrySection() {
      const reg = object(s.data.registry), sec = section('registry', 'Model registry');
      sec.append(pathLine('file', text(reg.path), reg.overridden ? 'FORGE_REGISTRY' : null));
      const why = el('p', undefined, 'set-why');
      why.append(el('i', undefined, 'lamp'), el('span', text(reg.reason) || 'Read-only here.'));
      sec.append(why);
      const rows = list(reg.rows);
      if (!rows.length) { sec.append(el('p', 'The registry has no rows.', 'muted')); return sec; }
      const wrap = el('div', undefined, 'set-table-wrap'), table = el('table', undefined, 'set-table');
      const cap = el('caption', 'Model registry rows', 'visually-hidden');
      const thead = el('thead'), hr = el('tr');
      for (const h of ['Alias', 'Harness', 'Model', 'Effort', 'Timeout']) { const th = el('th', h); th.scope = 'col'; hr.append(th); }
      thead.append(hr);
      const tbody = el('tbody');
      for (const r of rows) {
        const tr = el('tr');
        const th = el('th', text(r.alias), 'set-alias'); th.scope = 'row';
        tr.append(th, el('td', text(r.harness)), el('td', text(r.model), 'mono'), el('td', text(r.effort) || '—', 'mono'), el('td', text(r.timeout) || '—', 'mono'));
        tbody.append(tr);
      }
      table.append(cap, thead, tbody); wrap.append(table); sec.append(wrap);
      return sec;
    }

    function jevSection() {
      const jev = object(s.data.jev), on = !!jev.enabled;
      const sec = section('jev', 'Jev', 'Forge\'s own switch for its decision model. It lives in forge\'s config, so it applies to every repository and run, not only this one.');
      sec.append(pathLine('config', text(jev.path), jev.exists ? null : 'absent'));
      const row = el('div', undefined, 'set-switch-row');
      const lamp = el('i', undefined, 'lamp ' + (on ? 'st-pass' : ''));
      const state = el('span', on ? 'Jev is enabled' : 'Jev is disabled', 'set-state');
      row.append(lamp, state);
      const result = el('div', undefined, 'set-result');
      if (s.writes) {
        const sw = el('button', undefined, 'set-switch'); sw.type = 'button';
        sw.setAttribute('role', 'switch'); sw.setAttribute('aria-checked', String(on));
        sw.append(el('span', undefined, 'set-switch-knob'), el('span', on ? 'On' : 'Off', 'set-switch-label'));
        sw.setAttribute('aria-label', 'Jev enabled');
        sw.addEventListener('click', async () => {
          const next = !on;
          const run = s.run, release = grab();
          sw.disabled = true;
          const yes = await UI.confirmInline(result, `Turn Jev ${next ? 'on' : 'off'}? This changes forge's own config (${text(jev.path)}) for every repository.`);
          if (!yes) { sw.disabled = false; release(); sw.focus({ preventScroll: true }); return; }
          const res = await UI.post('/api/config', configBody(run, 'jev.enabled', next));
          UI.outcome(result, res, `Jev is now ${next ? 'enabled' : 'disabled'}.`);
          sw.disabled = false; release();
          if (res.ok && run === s.run) await reload();
        });
        row.append(sw);
      }
      sec.append(row, result);
      return sec;
    }

    function repoSection() {
      const repo = object(s.data.repo), files = object(repo.files);
      const sec = section('repo', 'This repository');
      if (!repo.path) {
        sec.append(el('p', 'forge found no repository for this run.', 'muted'));
        return sec;
      }
      sec.append(pathLine('repository', text(repo.path)));
      sec.append(el('p', 'These files sit in the repository\'s working tree. Saving writes into it; commit the file to share it.', 'muted set-lede'));
      const what = {
        verify: 'The command that verifies the combined result of a run.',
        setup: 'A shell command forge runs to prepare a worktree (executable, or a file holding one command).',
        'verify-subset': 'The per-file test command used by the review checks; it must contain {files}.',
      };
      for (const name of FILES) sec.append(fileEditor(name, object(files[name]), text(repo.path), what[name]));
      return sec;
    }

    function fileEditor(name, file, repoPath, blurb) {
      const target = targetPath(repoPath, name), id = 'set-f-' + name;
      const ed = s.editors[name] || (s.editors[name] = editorOf(file.content));
      const art = el('article', undefined, 'set-file'); art.setAttribute('aria-labelledby', id + '-h');
      const unreadable = file.content === null || file.content === undefined;
      const h = el('h3', '.forge/' + name, 'set-file-name'); h.id = id + '-h';
      const status = el('p', undefined, 'set-file-status');
      const top = el('div', undefined, 'set-file-head'); top.append(h, status);
      art.append(top, el('p', blurb, 'muted set-file-blurb'));
      if (file.note) art.append(el('p', text(file.note), 'warn set-file-note'));
      if (isDirty(ed) && s.moved[name]) art.append(el('p', 'The file changed on disk after you began editing; saving replaces it with your text.', 'warn set-file-note'));
      if (!s.writes) {
        status.replaceChildren(el('i', undefined, 'lamp ' + (file.exists ? 'st-pass' : '')), el('span', file.exists ? (unreadable ? 'present, not shown' : 'present') : 'absent'));
        art.append(unreadable ? el('p', file.exists ? 'The content is not shown.' : 'Not set.', 'muted') : el('pre', text(file.content), 'set-pre'));
        return art;
      }
      const lab = el('label', 'Contents of ' + target, 'visually-hidden'); lab.htmlFor = id;
      const ta = el('textarea', undefined, 'set-editor mono'); ta.id = id;
      ta.dataset.editor = name; ta.rows = Math.max(3, Math.min(14, shown(ed).split('\n').length + 1));
      ta.spellcheck = false; ta.setAttribute('autocapitalize', 'off'); ta.setAttribute('autocomplete', 'off');
      ta.value = shown(ed);
      if (unreadable && file.exists) { ta.disabled = true; ta.placeholder = 'The content is not shown, so it cannot be edited here.'; }
      const problem = el('p', undefined, 'warn set-problem'); problem.hidden = true; problem.id = id + '-problem';
      const result = el('div', undefined, 'set-result');
      const save = key_('Save', 'primary'), remove = key_('Remove');
      remove.classList.add('set-remove');
      save.setAttribute('aria-label', 'Save ' + name); remove.setAttribute('aria-label', 'Remove ' + name);
      const sync = () => {
        const cur = s.editors[name], dirtyNow = isDirty(cur), bad = dirtyNow ? valueProblem(name, cur.draft) : null;
        problem.hidden = !bad; problem.textContent = bad || '';
        if (bad) { ta.setAttribute('aria-invalid', 'true'); ta.setAttribute('aria-describedby', problem.id); }
        else { ta.removeAttribute('aria-invalid'); ta.removeAttribute('aria-describedby'); }
        save.disabled = !dirtyNow || !!bad;
        status.replaceChildren(el('i', undefined, 'lamp ' + (dirtyNow ? 'set-amber' : file.exists ? 'st-pass' : '')),
          el('span', dirtyNow ? 'unsaved edits' : file.exists ? (unreadable ? 'present, not shown' : 'present') : 'absent'));
      };
      ta.addEventListener('input', () => { s.editors[name] = edited(s.editors[name], ta.value); sync(); });
      remove.disabled = !file.exists;
      save.addEventListener('click', async () => {
        const cur = s.editors[name], bad = valueProblem(name, cur.draft);
        if (bad) { sync(); return; }
        const run = s.run, release = grab();
        save.disabled = remove.disabled = true;
        const yes = await UI.confirmInline(result, `Write ${target}? This changes a file in the repository's working tree.`);
        if (!yes) { remove.disabled = !file.exists; release(); sync(); save.focus({ preventScroll: true }); return; }
        const res = await UI.post('/api/config', configBody(run, repoKey(name), cur.draft));
        UI.outcome(result, res, `Saved ${target}.`);
        if (res.ok && run === s.run) s.editors[name] = editorOf(cur.draft.endsWith('\n') ? cur.draft : cur.draft + '\n');
        remove.disabled = !file.exists; release(); sync();
        if (res.ok && run === s.run) await reload();
      });
      remove.addEventListener('click', async () => {
        const run = s.run, release = grab();
        save.disabled = remove.disabled = true;
        const yes = await UI.confirmInline(result, `Delete ${target}? This removes a file from the repository's working tree.`, { danger: true });
        if (!yes) { release(); sync(); remove.disabled = !file.exists; remove.focus({ preventScroll: true }); return; }
        const res = await UI.post('/api/config', configBody(run, repoKey(name), null, true));
        UI.outcome(result, res, `Removed ${target}.`);
        if (res.ok && run === s.run) s.editors[name] = editorOf('');
        release();
        if (res.ok && run === s.run) await reload(); else if (!res.ok) { remove.disabled = !file.exists; sync(); }
      });
      const keys = el('div', undefined, 'set-keys'); keys.append(save, remove);
      art.append(lab, ta, problem, keys, result);
      sync();
      return art;
    }

    function datasetSection() {
      const d = object(s.data.dataset), sec = section('dataset', 'Dataset');
      sec.append(pathLine('folder', text(d.path)));
      const p = el('p', undefined, 'set-state-line');
      p.append(el('i', undefined, 'lamp ' + (d.enabled ? 'st-pass' : '')), el('span', d.enabled ? 'Capture is on' : 'Capture is off'));
      sec.append(p);
      return sec;
    }

    function envSection() {
      const env = object(s.data.env), names = Object.keys(env).sort(), sec = section('env', 'Environment', 'The FORGE_* variables of the forge-ui process. Names that hold a secret show only as <set>.');
      if (!names.length) { sec.append(el('p', 'No FORGE_* variable is set.', 'muted')); return sec; }
      const dl = el('dl', undefined, 'set-env');
      for (const n of names) dl.append(el('dt', n, 'mono'), el('dd', text(env[n]) || '(empty)', 'mono'));
      sec.append(dl);
      return sec;
    }

    /* start, and the page's refresh */
    async function reload() { s.dataText = null; await load(); }
    // The page's refresh: the picker follows the run list at once (and a run that just appeared loads), the config itself
    // is read again at most every POLL_MS.
    const poll = () => {
      if (!s.ready) return Promise.resolve();
      drawPicker();
      return Date.now() - s.loadedAt >= POLL_MS ? load() : Promise.resolve();
    };
    UI.controlsEnabled().then(on => { s.writes = !!on; s.ready = true; note.hidden = on; drawPicker(); load(); });
    return { poll, pick };
  }

  return { render, configBody, repoKey, targetPath, subsetOk, valueProblem, pickDefault, groupRuns, optionLabel,
    editorOf, isDirty, dirtyState, edited, shown, FILES, VALUE_MAX };
});
