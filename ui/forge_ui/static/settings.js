/* forge-ui settings view (window.ForgeSettings, and a CommonJS module under node:test). Route #/settings: the model
   registry (read-only), the decision-model gates, the three per-repo .forge files of one repository, the dataset and
   the FORGE_* environment. The pure helpers (request bodies, value checks, the decision-model view, the repository
   picker, the editors' unsaved-text state) are tested in ui/tests/js/settings.test.js; render(host, ctx) is thin DOM
   over them. Every value from the server enters the page as text.
   ctx = { key (the run whose repo to show, or null), runs() (the run list), onPick?(key) }. render returns { poll() }:
   the page calls it on each refresh, and unsaved editor text survives it.
   The calls are GET /api/config?run=<key> and POST /api/config, see ui/docs/api.md, "Controls (write endpoints)". A write
   changes a file in the repository's working tree, or a decision-model gate in forge's user config, and is only offered
   with controls on. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeSettings = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  const FILES = ['verify', 'setup', 'verify-subset'];
  const VALUE_MAX = 4096;
  const DECIDE_VALUE_MAX = 256;
  // The same names as forge_decide.GATES. The section renders this list, not whatever keys a payload happens to carry.
  const GATES = ['drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover'];
  const MODES = ['off', 'shadow', 'on'];
  const ADD_COMMAND = 'forge decide provider add …';
  const POLL_MS = 20000;                                         // the config rarely changes, and each read starts a CLI process
  const list = v => Array.isArray(v) ? v : [];
  const object = v => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  const text = v => typeof v === 'string' ? v : '';
  const basename = p => String(p || '').replace(/\/+$/, '').split('/').pop() || String(p || '');

  /* ---------- pure ---------- */
  // The POST body: a set carries `value`, an unset carries `unset: true`, never both.
  function configBody(run, key, value, unset) {
    if (unset) return { run: String(run), key: String(key), unset: true };
    return { run: String(run), key: String(key), value: String(value) };
  }
  const repoKey = name => 'repo.' + name;
  // decide.gate.<gate>, or the same with .provider or .model.
  const gateKey = (name, field) => 'decide.gate.' + name + (field ? '.' + field : '');
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

  // Why a provider or model would be refused: empty, too long, a control character (newline and tab included),
  // leading or trailing whitespace, or a leading '-'. The same rules as the server, so a bad value is never posted.
  function decideTextProblem(value) {
    const v = text(value);
    if (!v.trim()) return 'Empty.';
    if (v !== v.trim()) return 'The text cannot start or end with a space.';
    const chars = Array.from(v).length;
    if (chars > DECIDE_VALUE_MAX) return `Too long: ${chars} of ${DECIDE_VALUE_MAX} characters.`;
    if (/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(v)) return 'The text has an unpaired surrogate, which cannot be saved.';
    if (/[\u0000-\u001f\u007f]/.test(v)) return 'Control characters are not allowed.';
    if (v.startsWith('-')) return 'The text cannot start with "-".';
    return null;
  }

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const capTokens = n => finite(n) ? String(n) : 'none';
  const capMoney = n => finite(n) ? n.toFixed(4) : 'none';
  const money = n => finite(n) ? n.toFixed(4) : '0.0000';
  const count = n => finite(n) ? String(n) : '0';
  // How a shadowed value is named in the note. Strings keep their JSON quotes so a mode is not a bare word.
  function literal(value) {
    if (value == null) return 'nothing';
    if (typeof value === 'string') return JSON.stringify(value);
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    if (typeof value === 'boolean') return String(value);
    return 'a value';
  }

  // The Decision models section, as data. render() draws only this, so the page and the test share one reading of the
  // status object. Provider key fields are dropped here and never reach a string the page shows.
  function describeDecide(decide) {
    const d = object(decide);
    const gatesIn = object(d.gates);
    const floor = object(d.privacy).floor === true;
    const providers = Object.keys(object(d.providers)).sort().map(name => {
      const row = object(object(d.providers)[name]);
      return { name, type: text(row.type), local: row.local === true };
    });
    const gates = GATES.map(name => {
      const g = object(gatesIn[name]);
      const skip = floor ? 'privacy' : (typeof g.skip === 'string' ? g.skip : '');
      return {
        name,
        mode: MODES.includes(g.mode) ? g.mode : 'off',
        source: text(g.source) || 'default',
        provider: typeof g.provider === 'string' ? g.provider : '',
        model: typeof g.model === 'string' ? g.model : '',
        skip,
        ready: g.ready === true,
      };
    });
    const caps = object(object(d.budget).caps);
    const spend = object(object(d.budget).spend);
    const today = object(spend.today);
    const date = text(spend.date) || '—';
    const line = 'run cap ' + capTokens(caps.run_tokens) + ' tokens, $' + capMoney(caps.run_usd)
      + '; daily cap ' + capTokens(caps.daily_tokens) + ' tokens, $' + capMoney(caps.daily_usd)
      + '; today (' + date + '): ' + count(today.tokens) + ' tokens of ' + capTokens(caps.daily_tokens)
      + ', $' + money(today.usd) + ' of $' + capMoney(caps.daily_usd);
    const shadowed = list(d.shadowed).filter(e => e && typeof e === 'object').map(e => ({
      key: text(e.key),
      note: text(e.key) + ': the repo sets ' + literal(e.repo) + ', the user file\'s ' + literal(e.user) + ' wins',
    }));
    return {
      gates, privacyFloor: floor,
      banner: floor ? 'Nothing is sent. This repository is under a privacy floor.' : '',
      budget: {
        run: { tokens: caps.run_tokens == null ? null : caps.run_tokens, usd: caps.run_usd == null ? null : caps.run_usd },
        daily: { tokens: caps.daily_tokens == null ? null : caps.daily_tokens, usd: caps.daily_usd == null ? null : caps.daily_usd },
        today: { tokens: today.tokens == null ? 0 : today.tokens, usd: today.usd == null ? 0 : today.usd, date },
        line,
      },
      shadowed, providers, noProviders: providers.length === 0, addCommand: ADD_COMMAND,
    };
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
  // The editor after typing in a textarea drawn from the file text `from`. When a read moved `base` on since the textarea
  // was drawn (a held body is not redrawn), the typing sits on the old text, so the save would overwrite the new file.
  function typed(editor, draft, from) {
    const e = edited(editor, draft), dirty = isDirty(e);
    return { editor: e, dirty, moved: dirty && norm(from) !== norm(e.base) };
  }
  const shown = editor => editor.draft !== null ? editor.draft : editor.base;
  // What a failed GET /api/config does: with a good read of this run on the page, it stays and a note names the
  // failure; with none yet, the message replaces the page. Status 0 is no answer at all.
  function readFailure(res, hasData) {
    const message = (res && res.status !== 0 && (res.detail || res.error)) || 'could not reach forge-ui';
    return { keep: !!hasData, message };
  }

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI, c = ctx || {};
    const el = (tag, t, cls) => UI.el(tag, t, cls);
    const s = { run: null, data: null, dataText: null, error: null, writes: false, loading: true, editors: {}, moved: {}, models: {}, modelMoved: {}, hold: 0, redraw: false, seq: 0, gen: 0, runsSig: null, ready: false, loadedAt: 0, repo: null, gone: false, asking: 0, asks: 0, pickerLate: false };
    const key_ = (label, cls, onclick) => { const b = el('button', label, cls); b.type = 'button'; if (onclick) b.addEventListener('click', onclick); return b; };

    const head = el('header', undefined, 'set-head');
    head.append(el('h1', 'Settings', 'module-title'),
      el('p', 'What forge reads when it runs: its model registry, the decision-model gates, and the three files a repository can keep in .forge.', 'muted set-lede'));
    const picker = el('div', undefined, 'set-picker');
    const body = el('div', undefined, 'set-body');
    const note = el('p', 'This page is read-only: this server has no write token, or has controls off.', 'muted set-readonly');
    note.hidden = true;
    // A failed read over a good one: the page stays, and this says the shown values may be old. Outside the body, so a
    // held body (an open confirm) still gets it.
    const stale = el('p', undefined, 'warn set-problem set-stale'); stale.setAttribute('role', 'status');
    stale.hidden = true;
    host.replaceChildren(head, picker, note, stale, body);
    const staleNote = msg => { stale.hidden = !msg; stale.textContent = msg ? `The last read of the configuration failed: ${msg}. This shows the read before it; the next refresh tries again.` : ''; };

    /* the repository picker */
    function drawPicker() {
      // The switch confirm lives in the picker: while it is open the rebuild waits, and runs when it is answered.
      if (s.asking) { s.pickerLate = true; return; }
      const runs = c.runs ? c.runs() : [];
      const groups = groupRuns(runs);
      const before = s.run, usable = withRepo(runs);
      let kept = false;
      s.gone = false;
      if (s.run != null && !usable.some(r => r.key === s.run)) {
        // The run left the list. Another run of the same repository carries on with the editors as they are; with unsaved
        // text and no such run the page stays on it, flagged, rather than throwing the text away.
        const sibling = usable.slice().sort(newestFirst).find(r => r.repo === s.repo);
        if (sibling) { s.run = sibling.key; kept = true; }
        else if (FILES.some(n => isDirty(s.editors[n]))) s.gone = true;
        else s.run = null;
      }
      if (s.run == null) s.run = pickDefault(runs, c.key);
      const now = usable.find(r => r.key === s.run);
      if (now) s.repo = now.repo;
      if (s.run !== before) {
        if (!kept) resetRun();
        if (s.ready) { if (!kept) drawBody(); load(); }           // the default arrived, or the run went away
      }
      const sig = JSON.stringify([s.run, s.gone, groups.map(g => [g.repo, g.runs.map(r => [r.key, optionLabel(r)])])]);
      if (sig === s.runsSig) return;
      s.runsSig = sig;
      if (!groups.length) { picker.replaceChildren(el('p', 'No run names a repository yet, so there is nothing to configure.', 'muted')); return; }
      const sel = el('select'); sel.id = 'set-run';
      if (s.gone) { const o = el('option', 'This run is no longer listed'); o.value = ''; o.selected = true; o.disabled = true; sel.append(o); }
      for (const g of groups) {
        const og = el('optgroup'); og.label = g.name + ' · ' + g.repo;
        for (const r of g.runs) { const o = el('option', optionLabel(r)); o.value = r.key; o.selected = r.key === s.run; og.append(o); }
        sel.append(og);
      }
      sel.addEventListener('change', async () => {
        const unsaved = FILES.filter(n => s.editors[n] && isDirty(s.editors[n]));
        if (unsaved.length) {
          const ask = s.asking = ++s.asks;
          const yes = await UI.confirmInline(picker, `Switch repository and discard the unsaved edits to ${unsaved.map(n => '.forge/' + n).join(', ')}?`, { danger: true });
          if (s.asking !== ask) return;                           // a later change asked again, and that confirm decides
          const late = s.pickerLate;
          s.asking = 0; s.pickerLate = false;
          if (!yes) { sel.value = s.gone ? '' : s.run; if (late) drawPicker(); return; }
        }
        pick(sel.value);
      });
      const lab = el('label', undefined, 'set-field'); lab.htmlFor = 'set-run';
      lab.append(el('span', 'Repository of run', 'plate'));
      const path = el('p', undefined, 'set-repo-path');
      path.append(el('i', undefined, 'lamp lamp-route'), el('span', s.repo || '', 'mono'));
      const row = el('div', undefined, 'set-picker-row'); row.append(lab, sel);
      picker.replaceChildren(row, path);
      if (s.gone) picker.append(el('p', 'The run is gone from the list. Your unsaved edits are kept; pick another run to continue, which discards them.', 'warn set-problem'));
    }
    // A new run: nothing of the old one stays, and confirms of the old body (now detached) no longer hold a redraw.
    function resetRun() {
      staleNote(null);
      s.data = null; s.dataText = null; s.error = null; s.editors = {}; s.moved = {}; s.models = {}; s.modelMoved = {}; s.loading = s.run != null;
      s.gen++; s.hold = 0; s.redraw = false;
    }
    function pick(key) {
      s.run = key; resetRun();
      s.asking = 0; s.pickerLate = false;                         // a switch confirm still open goes with the old picker
      s.runsSig = null; drawPicker(); drawBody();
      if (c.onPick) c.onPick(key);
      load();
    }

    /* loading, and keeping unsaved text across it */
    async function load() {
      const run = s.run, seq = ++s.seq;
      if (run == null) { s.loading = false; drawBody(); return; }
      if (s.gone) return;                                         // the run is not listed: nothing to read until another is picked
      s.loadedAt = Date.now();
      const res = await UI.get('/api/config?run=' + encodeURIComponent(run));
      if (seq !== s.seq || run !== s.run) return;                 // a later pick or load answered
      s.loading = false;
      if (!res.ok) {
        const failed = readFailure(res, s.data);
        if (failed.keep) { staleNote(failed.message); return; }   // the last good page, its editors and their unsaved text stay
        if (s.hold > 0) return;                                   // a confirm or a write is open: its strip must not be torn down
        s.error = failed.message; s.data = null; s.dataText = null; drawBody(); return;
      }
      s.error = null; staleNote(null);
      const files = object(object(res.data.repo).files);
      for (const name of FILES) {
        const next = dirtyState(s.editors[name], object(files[name]).content);
        s.editors[name] = next.editor; s.moved[name] = next.moved || (next.dirty && !!s.moved[name]);
      }
      for (const g of describeDecide(res.data.decide).gates) {
        const next = dirtyState(s.models[g.name], g.model);
        s.models[g.name] = next.editor; s.modelMoved[g.name] = next.moved || (next.dirty && !!s.modelMoved[g.name]);
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
      const at = focus && focus.dataset.editor ? { kind: 'editor', name: focus.dataset.editor, a: focus.selectionStart, b: focus.selectionEnd }
        : focus && focus.dataset.model ? { kind: 'model', name: focus.dataset.model, a: focus.selectionStart, b: focus.selectionEnd } : null;
      if (s.loading) { body.replaceChildren(el('p', 'Reading the configuration…', 'muted')); return; }
      if (s.error) { body.replaceChildren(el('p', 'Could not read the configuration: ' + s.error, 'warn')); return; }
      if (!s.data) { body.replaceChildren(); return; }
      body.replaceChildren(registrySection(), decideSection(), repoSection(), datasetSection(), envSection());
      if (at) {
        const t = at.kind === 'model' ? body.querySelector(`input[data-model="${at.name}"]`) : body.querySelector(`textarea[data-editor="${at.name}"]`);
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

    function decideSection() {
      const view = describeDecide(s.data.decide);
      const sec = section('decide', 'Decision models', 'Each gate is off, shadow or on, with a provider and a model. A change writes your forge config (the user level), so it applies to every repository. The mode shown is the one in effect for this repository, and where it came from.');
      const where = text(object(s.data.decide).user_path);
      if (where) sec.append(pathLine('config', where));
      if (view.privacyFloor) {
        const banner = el('p', view.banner, 'set-banner');
        banner.id = 'set-decide-privacy';
        sec.append(banner);
      }
      const budget = el('p', view.budget.line, 'muted set-budget');
      budget.id = 'set-decide-budget';
      sec.append(budget);
      if (view.noProviders) {
        const note = el('p', undefined, 'muted');
        note.append(el('span', 'No provider is configured. Add one with '), el('span', view.addCommand, 'mono'), el('span', '.'));
        sec.append(note);
      }
      for (const item of view.shadowed) sec.append(el('p', item.note, 'muted'));
      const gates = el('div', undefined, 'set-gates');
      for (const gate of view.gates) gates.append(gateRow(gate, view.providers));
      sec.append(gates);
      return sec;
    }

    const providerLabel = p => [p.name, p.type, p.local ? 'local' : ''].filter(Boolean).join(' · ');

    function gateRow(gate, providers) {
      const art = el('article', undefined, 'set-file set-gate');
      art.id = 'set-gate-' + gate.name;
      const h = el('h3', gate.name, 'set-file-name');
      const status = el('p', undefined, 'set-file-status');
      const source = el('span', gate.source, 'tag');
      source.id = 'set-gate-' + gate.name + '-source';
      status.append(source);
      if (gate.skip) status.append(el('span', 'skip: ' + gate.skip, 'set-gate-skip'));
      const top = el('div', undefined, 'set-file-head');
      top.append(h, status);
      art.append(top);
      if (!s.writes) {
        const bits = ['mode ' + gate.mode, gate.provider ? 'provider ' + gate.provider : 'no provider', gate.model ? 'model ' + gate.model : 'no model'];
        art.append(el('p', bits.join(', '), 'muted'));
        return art;
      }
      const result = el('div', undefined, 'set-result');
      const row = el('div', undefined, 'filters');
      row.append(modeField(gate, result), providerField(gate, providers, result), modelField(gate, result));
      art.append(row, result);
      return art;
    }

    function modeField(gate, result) {
      const lab = el('label', undefined, 'filter');
      const sel = el('select');
      sel.id = 'set-gate-' + gate.name + '-mode';
      sel.setAttribute('aria-label', 'Mode for ' + gate.name);
      for (const mode of MODES) {
        const o = el('option', mode);
        o.value = mode;
        o.selected = mode === gate.mode;
        sel.append(o);
      }
      sel.addEventListener('change', async () => {
        const next = sel.value;
        if (!MODES.includes(next) || next === gate.mode) { sel.value = gate.mode; return; }
        const gen = s.gen, release = grab();
        sel.disabled = true;
        const yes = await UI.confirmInline(result, `Set ${gate.name} to ${next}? This writes your forge config (the user level) for every repository.`);
        if (!yes || gen !== s.gen) {
          sel.value = gate.mode; sel.disabled = false; release();
          if (!yes && gen === s.gen) sel.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config', configBody(s.run, gateKey(gate.name), next));
        UI.outcome(result, res, `${gate.name} is set to ${next}.`);
        if (!res.ok) sel.value = gate.mode;
        sel.disabled = false; release();
        if (res.ok && gen === s.gen) await reload();
      });
      lab.append(el('span', 'Mode'), sel);
      return lab;
    }

    function providerField(gate, providers, result) {
      const lab = el('label', undefined, 'filter');
      const sel = el('select');
      sel.id = 'set-gate-' + gate.name + '-provider';
      sel.setAttribute('aria-label', 'Provider for ' + gate.name);
      const blank = el('option', providers.length ? 'Default' : 'No provider');
      blank.value = '';
      if (gate.provider) blank.disabled = true;
      sel.append(blank);
      const known = new Set(providers.map(p => p.name));
      for (const p of providers) {
        const o = el('option', providerLabel(p));
        o.value = p.name;
        o.selected = p.name === gate.provider;
        sel.append(o);
      }
      if (gate.provider && !known.has(gate.provider)) {
        const o = el('option', gate.provider + ' · not configured');
        o.value = gate.provider;
        o.selected = true;
        sel.append(o);
      }
      if (!providers.length) sel.disabled = true;
      sel.addEventListener('change', async () => {
        const next = sel.value;
        if (!next || next === gate.provider) { sel.value = gate.provider; return; }
        if (decideTextProblem(next)) { sel.value = gate.provider; return; }
        const gen = s.gen, release = grab();
        sel.disabled = true;
        const yes = await UI.confirmInline(result, `Set the provider for ${gate.name} to ${next}? This writes your forge config (the user level) for every repository.`);
        if (!yes || gen !== s.gen) {
          sel.value = gate.provider;
          sel.disabled = providers.length === 0;
          release();
          if (!yes && gen === s.gen) sel.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config', configBody(s.run, gateKey(gate.name, 'provider'), next));
        UI.outcome(result, res, `The provider for ${gate.name} is set to ${next}.`);
        if (!res.ok) sel.value = gate.provider;
        sel.disabled = false; release();
        if (res.ok && gen === s.gen) await reload();
      });
      lab.append(el('span', 'Provider'), sel);
      return lab;
    }

    function modelField(gate, result) {
      const id = 'set-gate-' + gate.name + '-model';
      const ed = s.models[gate.name] || (s.models[gate.name] = editorOf(gate.model));
      const wrap = el('div', undefined, 'filter');
      const input = el('input', undefined, 'mono');
      input.type = 'text'; input.id = id; input.dataset.model = gate.name;
      input.spellcheck = false; input.setAttribute('autocomplete', 'off'); input.setAttribute('autocapitalize', 'off');
      input.setAttribute('aria-label', 'Model for ' + gate.name);
      input.value = shown(ed);
      let from = ed.base;
      const movedNote = el('p', 'The model changed on disk after you began editing; saving replaces it with your text.', 'warn set-file-note');
      movedNote.hidden = !(isDirty(ed) && s.modelMoved[gate.name]);
      const problem = el('p', undefined, 'warn set-problem');
      problem.hidden = true; problem.id = id + '-problem';
      const set = key_('Set', 'primary');
      set.setAttribute('aria-label', 'Set model for ' + gate.name);
      const sync = () => {
        const cur = s.models[gate.name], dirtyNow = isDirty(cur), bad = dirtyNow ? decideTextProblem(cur.draft) : null;
        problem.hidden = !bad; problem.textContent = bad || '';
        if (bad) { input.setAttribute('aria-invalid', 'true'); input.setAttribute('aria-describedby', problem.id); }
        else { input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby'); }
        set.disabled = !dirtyNow || !!bad;
        movedNote.hidden = !(dirtyNow && s.modelMoved[gate.name]);
      };
      input.addEventListener('input', () => {
        const next = typed(s.models[gate.name], input.value, from);
        s.models[gate.name] = next.editor;
        s.modelMoved[gate.name] = next.moved || (next.dirty && !!s.modelMoved[gate.name]);
        sync();
      });
      set.addEventListener('click', async () => {
        const cur = s.models[gate.name], bad = decideTextProblem(shown(cur));
        if (!isDirty(cur) || bad) { sync(); return; }
        const draft = cur.draft;
        const gen = s.gen, release = grab();
        set.disabled = true; input.readOnly = true;
        const yes = await UI.confirmInline(result, `Set the model for ${gate.name} to ${draft}? This writes your forge config (the user level) for every repository.`);
        if (!yes || gen !== s.gen) {
          input.readOnly = false; release(); sync();
          if (!yes && gen === s.gen) set.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config', configBody(s.run, gateKey(gate.name, 'model'), draft));
        UI.outcome(result, res, `The model for ${gate.name} is set.`);
        if (res.ok && gen === s.gen) { s.models[gate.name] = editorOf(draft); s.modelMoved[gate.name] = false; from = draft; }
        input.readOnly = false; release(); sync();
        if (res.ok && gen === s.gen) await reload();
      });
      wrap.append(el('span', 'Model'), input, problem, movedNote, set);
      sync();
      return wrap;
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
      const movedNote = el('p', 'The file changed on disk after you began editing; saving replaces it with your text.', 'warn set-file-note');
      movedNote.hidden = !(isDirty(ed) && s.moved[name]);              // sync() keeps it current as the text is typed
      art.append(movedNote);
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
      let from = ed.base;                                         // the file text this textarea was drawn from
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
        movedNote.hidden = !(dirtyNow && s.moved[name]);
        status.replaceChildren(el('i', undefined, 'lamp ' + (dirtyNow ? 'set-amber' : file.exists ? 'st-pass' : '')),
          el('span', dirtyNow ? 'unsaved edits' : file.exists ? (unreadable ? 'present, not shown' : 'present') : 'absent'));
      };
      ta.addEventListener('input', () => {
        const next = typed(s.editors[name], ta.value, from);
        s.editors[name] = next.editor; s.moved[name] = next.moved || (next.dirty && !!s.moved[name]);
        sync();
      });
      remove.disabled = !file.exists;
      save.addEventListener('click', async () => {
        const cur = s.editors[name], bad = valueProblem(name, cur.draft);
        if (bad) { sync(); return; }
        const gen = s.gen, release = grab();
        save.disabled = remove.disabled = ta.readOnly = true;       // the text sent is the text that was confirmed
        const yes = await UI.confirmInline(result, `Write ${target}? This changes a file in the repository's working tree.`);
        if (!yes) { ta.readOnly = false; remove.disabled = !file.exists; release(); sync(); save.focus({ preventScroll: true }); return; }
        const res = await UI.post('/api/config', configBody(s.run, repoKey(name), cur.draft));
        UI.outcome(result, res, `Saved ${target}.`);
        if (res.ok && gen === s.gen) { s.editors[name] = editorOf(cur.draft.endsWith('\n') ? cur.draft : cur.draft + '\n'); s.moved[name] = false; from = s.editors[name].base; }
        ta.readOnly = false; remove.disabled = !file.exists; release(); sync();
        if (res.ok && gen === s.gen) await reload();
      });
      remove.addEventListener('click', async () => {
        const gen = s.gen, release = grab();
        save.disabled = remove.disabled = ta.readOnly = true;
        const yes = await UI.confirmInline(result, `Delete ${target}? This removes a file from the repository's working tree.`, { danger: true });
        if (!yes) { ta.readOnly = false; release(); sync(); remove.disabled = !file.exists; remove.focus({ preventScroll: true }); return; }
        const res = await UI.post('/api/config', configBody(s.run, repoKey(name), null, true));
        UI.outcome(result, res, `Removed ${target}.`);
        if (res.ok && gen === s.gen) { s.editors[name] = editorOf(''); s.moved[name] = false; from = ''; }
        ta.readOnly = false; release();
        if (res.ok && gen === s.gen) await reload(); else if (!res.ok) { remove.disabled = !file.exists; sync(); }
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
    UI.controlsEnabled().then(on => { s.writes = !!on; s.ready = true; note.hidden = on; const asked = s.seq; drawPicker(); if (s.seq === asked) load(); });
    return { poll, pick };
  }

  return { render, configBody, repoKey, gateKey, targetPath, subsetOk, valueProblem, decideTextProblem, describeDecide,
    pickDefault, groupRuns, optionLabel, editorOf, isDirty, dirtyState, edited, typed, shown, readFailure,
    FILES, GATES, MODES, VALUE_MAX, DECIDE_VALUE_MAX, ADD_COMMAND };
});
