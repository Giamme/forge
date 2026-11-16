/* forge-ui finding actions (#161): the pure logic behind the Findings tab's write controls (window.ForgeFindingsActions,
   and a CommonJS module under node:test). No DOM. Nothing is decided here that the server does not decide again: these
   functions only build the request body of each op (or say why it cannot be built), build the `forge findings …` line
   a read-only page offers to copy, and say what a selection allows.
   A row is the Projects answer's finding row: `record_fp` is the record key (null when the row cannot be acted on),
   `status` one of STATUSES, `tags` the effective tags, `record.tags` the stored ones, `fp` and `run` what `forge issues`
   files by. Tested in ui/tests/js/findingsactions.test.js. */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeFindingsActions = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const MAX_FPS = 200, MAX_FILE = 25, MAX_TAGS = 8, MAX_NOTE = 2000, MAX_REASON = 500, MAX_EDIT_TEXT = 4000, MAX_TITLE = 200, MAX_BODY = 20000;
  const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
  const OPEN_STATUSES = ['open', 'to-file'];
  const OPS = ['fix', 'dismiss', 'reopen', 'to-file', 'dup', 'tag', 'note', 'edit'];
  const SINGLE = ['dup', 'note', 'edit'];
  const TAG_RE = /^[a-z][a-z0-9-]{0,23}$/, FP_RE = /^[0-9a-f]{12}$/, COMMIT_RE = /^[0-9a-f]{7,40}$/;
  const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;   // newline and tab are allowed, as the routes allow them
  const PAST = { fix: 'marked fixed', dismiss: 'dismissed', reopen: 'reopened', 'to-file': 'marked to file', dup: 'marked duplicate', tag: 'tagged', note: 'noted', edit: 'edited' };

  const str = v => typeof v === 'string' ? v : v == null ? '' : String(v);
  const fail = error => ({ error });

  /* ---------- rows ---------- */
  const selectable = row => !!(row && typeof row.record_fp === 'string' && row.record_fp);
  const isOpen = row => !!row && OPEN_STATUSES.includes(row.status);
  // The tags a person stored on this row. With none stored the row shows the defaults forge derives from the path.
  const storedTags = row => list(row && row.record && row.record.tags).filter(t => typeof t === 'string');
  const tagsOf = row => list(row && row.tags).filter(t => typeof t === 'string');
  const derivedTags = row => !!row && storedTags(row).length === 0;
  function tagsIn(rows) { return Array.from(new Set(list(rows).flatMap(tagsOf))).sort(); }
  function filterByTag(rows, tag) { return tag ? list(rows).filter(r => tagsOf(r).includes(tag)) : list(rows); }

  /* ---------- the selection ---------- */
  // The selected rows among `rows` (the rows the filters show), in row order, once each. `picked` is a Set of record_fp.
  function selectedRows(rows, picked) {
    const want = picked instanceof Set ? picked : new Set(list(picked)), seen = new Set(), out = [];
    for (const row of list(rows)) {
      if (!selectable(row) || !want.has(row.record_fp) || seen.has(row.record_fp)) continue;
      seen.add(row.record_fp); out.push(row);
    }
    return out;
  }
  const fpsOf = rows => list(rows).filter(selectable).map(r => r.record_fp);
  // Which actions a selection (rows already narrowed to the shown ones) allows. `why` says what holds them all back.
  function allowed(selected) {
    const rows = list(selected).filter(selectable), n = rows.length, many = n >= 1 && n <= MAX_FPS, one = n === 1;
    const why = n > MAX_FPS ? `${n} selected: at most ${MAX_FPS} per action` : '';
    return { count: n, why, fix: many, dismiss: many, reopen: many && rows.some(r => r.status !== 'open'), 'to-file': many, tag: many,
      dup: one, note: one, edit: one, file: fileableFps(rows).length > 0 };
  }
  // The fingerprints "File selected" hands to forge issues: rows `forge issues` can file, still open, at most 25.
  function fileableFps(rows) {
    const out = [];
    for (const r of list(rows)) {
      if (out.length >= MAX_FILE) break;
      if (r && r.fileable && isOpen(r) && typeof r.fp === 'string' && r.fp && !out.includes(r.fp)) out.push(r.fp);
    }
    return out;
  }
  // The run `forge issues` reads for the filing: the first fileable selected row's.
  function fileRun(rows) {
    const row = list(rows).find(r => r && r.fileable && isOpen(r) && r.fp && r.run);
    return row ? row.run : null;
  }

  /* ---------- tags ---------- */
  // "a, b" → {tags, error}. Commas and spaces separate; each tag is a lowercase letter then up to 23 letters, digits or dashes.
  function parseTags(text) {
    const tags = Array.from(new Set(str(text).split(/[\s,]+/).filter(Boolean)));
    if (!tags.length) return { tags: [], error: 'give at least one tag' };
    if (tags.length > MAX_TAGS) return { tags: [], error: `at most ${MAX_TAGS} tags` };
    const bad = tags.find(t => !TAG_RE.test(t));
    if (bad !== undefined) return { tags: [], error: `"${bad}" is not a tag: a lowercase letter, then lowercase letters, digits or dashes, at most 24 characters` };
    return { tags, error: null };
  }

  /* ---------- bodies ---------- */
  function checkText(value, name, limit, required) {
    const text = str(value).trim();
    if (!text) return required ? fail(`${name} is required`) : { text: null };
    if (text.length > limit) return fail(`${name} is at most ${limit} characters`);
    if (CONTROL_RE.test(text)) return fail(`${name} cannot hold control characters`);
    return { text };
  }
  // The POST body of one op for these record fingerprints: {body} or {error}. `o` holds what the person typed.
  function bodyFor(op, fps, o) {
    o = o || {};
    if (!OPS.includes(op)) return fail('unknown action');
    const keys = Array.from(new Set(list(fps).filter(f => typeof f === 'string' && f)));
    if (!keys.length) return fail('select a finding first');
    if (SINGLE.includes(op) ? keys.length !== 1 : keys.length > MAX_FPS) return fail(SINGLE.includes(op) ? 'this action takes one finding' : `at most ${MAX_FPS} findings per action`);
    const body = { fps: keys };
    if (op === 'fix') {
      const commit = str(o.commit).trim().toLowerCase(), reason = checkText(o.reason, 'the reason', MAX_REASON, false);
      if (reason.error) return reason;
      if (!commit && reason.text === null) return fail('give a commit or a reason');
      if (commit) { if (!COMMIT_RE.test(commit)) return fail('a commit is 7 to 40 hexadecimal characters'); body.commit = commit; }
      if (reason.text !== null) body.reason = reason.text;
    } else if (op === 'dismiss') {
      const reason = checkText(o.reason, 'the reason', MAX_REASON, true);
      if (reason.error) return reason;
      body.reason = reason.text;
    } else if (op === 'to-file') {
      const title = checkText(o.title, 'the title', MAX_TITLE, false), text = checkText(o.body, 'the body', MAX_BODY, false);
      if (title.error) return title;
      if (text.error) return text;
      if (title.text !== null) body.title = title.text;
      if (text.text !== null) body.body = text.text;
    } else if (op === 'dup') {
      const of = str(o.of).trim();
      if (!FP_RE.test(of)) return fail('pick the finding it duplicates');
      if (of === keys[0]) return fail('a finding cannot duplicate itself');
      body.of = of;
    } else if (op === 'tag') {
      const parsed = Array.isArray(o.tags) ? parseTags(o.tags.join(' ')) : parseTags(o.tags);
      if (parsed.error) return fail(parsed.error);
      body.tags = parsed.tags; body.remove = !!o.remove;
    } else if (op === 'note') {
      const text = checkText(o.text, 'the note', MAX_NOTE, false);   // empty text clears the note
      if (text.error) return text;
      body.text = text.text === null ? '' : text.text;
    } else if (op === 'edit') {
      const severity = str(o.severity).trim();
      if (severity && !SEVERITIES.includes(severity)) return fail('the severity is P0, P1, P2 or P3');
      if (!severity && (o.text === undefined || o.text === null)) return fail('give a severity or a text');
      if (severity) body.severity = severity;
      if (o.text !== undefined && o.text !== null) {              // an empty text clears the edit
        const text = str(o.text);
        if (text.length > MAX_EDIT_TEXT) return fail(`the text is at most ${MAX_EDIT_TEXT} characters`);
        body.text = text;
      }
    }
    return { body };
  }
  const pathOf = (id, op) => '/api/projects/' + encodeURIComponent(id) + '/findings/' + op;

  /* ---------- the copyable command ---------- */
  // One argument for a POSIX shell: single quotes keep `$`, backticks and quotes literal. A safe word stays bare.
  function shellQuote(value) {
    const text = str(value);
    if (text !== '' && /^[A-Za-z0-9_@%+=:,./-]+$/.test(text) && !text.startsWith('-')) return text;
    return "'" + text.replace(/'/g, "'\\''") + "'";
  }
  // `--name VALUE`, or `--name=VALUE` when the value starts with "-" so the CLI does not read it as a flag.
  const option = (name, value) => { const v = shellQuote(value); return str(value).startsWith('-') ? `--${name}=${v}` : `--${name} ${v}`; };
  // `forge findings <op> REPO FP… …`. What the person has not typed stays a placeholder word (SHA, TEXT, FP, TAG).
  function commandFor(op, repo, fps, o) {
    o = o || {};
    const head = ['forge findings', op, shellQuote(repo)].concat(list(fps).map(shellQuote));
    const has = v => str(v).trim() !== '';
    if (op === 'fix') {
      if (has(o.commit)) head.push(option('commit', str(o.commit).trim()));
      if (has(o.reason)) head.push(option('reason', str(o.reason).trim()));
      if (!has(o.commit) && !has(o.reason)) head.push('--commit SHA');
    } else if (op === 'dismiss') head.push(has(o.reason) ? option('reason', str(o.reason).trim()) : '--reason TEXT');
    else if (op === 'to-file') { if (has(o.title)) head.push(option('title', str(o.title).trim())); if (has(o.body)) head.push('--body-file FILE'); }
    else if (op === 'dup') head.push(has(o.of) ? option('of', str(o.of).trim()) : '--of FP');
    else if (op === 'tag') {
      const tags = Array.isArray(o.tags) ? o.tags : has(o.tags) ? parseTags(o.tags).tags : [];
      head.push(...(tags.length ? tags.map(shellQuote) : ['TAG']), o.remove ? '--remove' : '--add');
    } else if (op === 'note') { const text = o.text === undefined ? 'TEXT' : str(o.text).trim(); head.push(...(text.startsWith('-') ? ['--', shellQuote(text)] : [shellQuote(text)])); }
    else if (op === 'edit') {
      if (has(o.severity)) head.push(option('severity', str(o.severity).trim()));
      if (o.text !== undefined && o.text !== null) head.push('--text-file FILE');
      if (!has(o.severity) && (o.text === undefined || o.text === null)) head.push('--severity P2');
    }
    return head.join(' ');
  }
  // The commands a read-only page offers for one row: [{op, label, command}].
  const COMMAND_LABELS = { fix: 'Mark fixed', dismiss: 'Dismiss', reopen: 'Reopen', 'to-file': 'Mark to file', dup: 'Duplicate of…', tag: 'Add a tag', note: 'Note', edit: 'Edit' };
  function commandsFor(repo, row) {
    if (!selectable(row)) return [];
    return OPS.filter(op => op !== 'reopen' || row.status !== 'open').map(op => ({ op, label: COMMAND_LABELS[op], command: commandFor(op, repo, [row.record_fp]) }));
  }

  /* ---------- the confirm sentence ---------- */
  const noun = n => `${n} finding${n === 1 ? '' : 's'}`;
  function confirmText(op, count, body) {
    const b = body || {}, n = noun(count);
    if (op === 'fix') return `Mark ${n} fixed${b.commit ? ` by ${b.commit}` : ''}${b.reason ? `: ${b.reason}` : ''}?`;
    if (op === 'dismiss') return `Dismiss ${n}: ${b.reason}?`;
    if (op === 'reopen') return `Reopen ${n}?`;
    if (op === 'to-file') return `Mark ${n} to file${b.title ? ` as "${b.title}"` : ''}?`;
    if (op === 'dup') return `Mark ${n} a duplicate of ${b.of}?`;
    if (op === 'tag') return `${b.remove ? 'Remove' : 'Add'} ${list(b.tags).join(', ')} ${b.remove ? 'from' : 'to'} ${n}?`;
    if (op === 'note') return b.text ? `Set the note of ${n}?` : `Clear the note of ${n}?`;
    if (op === 'edit') return `Edit ${n}${b.severity ? `: severity ${b.severity}` : ''}${b.text !== undefined ? ': replace the text' : ''}?`;
    return `${op} ${n}?`;
  }

  return { MAX_FPS, MAX_FILE, MAX_TAGS, OPS, SINGLE, PAST, OPEN_STATUSES, selectable, isOpen, storedTags, tagsOf, derivedTags, tagsIn, filterByTag, selectedRows, fpsOf,
    allowed, fileableFps, fileRun, parseTags, bodyFor, pathOf, shellQuote, commandFor, commandsFor, confirmText };
});
