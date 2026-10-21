/* forge-ui data tables: sortable, reorderable columns that can be hidden, remembered per table.
   A column is { id, label, title?, cls?, mono?, value(row) → number|string|null (sort key), cell(row) → Node|<td>|text,
   sortable? (default true), hideable? (default true; the first column never hides), hidden? (starts hidden until shown) }.
   Click a header to sort (ascending, descending, off); drag a header, or Alt+←/→ on it, to move it; the Columns menu shows
   or hides columns and resets the table. Empty values always sort last.
   window.ForgeTable = { render(parent, opts) → root, normPrefs, sortRows, compare, move } and module.exports for node tests.
   opts = { id, label, columns, rows, prefs, savePrefs(prefs), rowClass(row)?, onRow(row, tr)?, scrollKey?, tools? (an element
   that receives the Columns button instead of a toolbar row) }. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeTable = api;
})(typeof self !== 'undefined' ? self : this, () => {
  'use strict';
  const list = v => Array.isArray(v) ? v : [];
  const empty = v => v === null || v === undefined || v === '' || (typeof v === 'number' && !Number.isFinite(v));

  /* ---------- pure: preferences, ordering, sorting ---------- */
  // Saved preferences against today's columns: unknown ids dropped, new columns slotted in at their default place
  // (and hidden when they start hidden).
  function normPrefs(prefs, columns) {
    prefs = prefs && typeof prefs === 'object' ? prefs : {};
    const ids = list(columns).map(c => c.id), known = new Set(ids);
    const saved = list(prefs.order).filter((id, i, a) => known.has(id) && a.indexOf(id) === i);
    const order = saved.slice();
    ids.forEach((id, i) => {
      if (order.includes(id)) return;
      // after the nearest column that precedes it by default and is already placed
      let at = 0;
      for (let j = i - 1; j >= 0; j--) { const k = order.indexOf(ids[j]); if (k >= 0) { at = k + 1; break; } }
      order.splice(at, 0, id);
    });
    const hideable = new Set(list(columns).filter((c, i) => i > 0 && c.hideable !== false).map(c => c.id));
    const fresh = list(columns).filter(c => c.hidden && !saved.includes(c.id)).map(c => c.id);
    const hidden = list(prefs.hidden).concat(fresh).filter((id, i, a) => hideable.has(id) && a.indexOf(id) === i);
    const col = list(columns).find(c => prefs.sort && c.id === prefs.sort.id && c.sortable !== false);
    const sort = col && (prefs.sort.dir === 'asc' || prefs.sort.dir === 'desc') ? { id: col.id, dir: prefs.sort.dir } : null;
    return { order, hidden, sort };
  }
  // Numbers by value, text in natural order ("task-2" before "task-10"), numbers before text, empty last.
  function compare(a, b) {
    const ea = empty(a), eb = empty(b);
    if (ea || eb) return ea === eb ? 0 : ea ? 1 : -1;
    const na = typeof a === 'number', nb = typeof b === 'number';
    if (na && nb) return a - b;
    if (na !== nb) return na ? -1 : 1;
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
  }
  // A stable sort by one column; empty values stay at the bottom whichever way it runs.
  function sortRows(rows, column, dir) {
    const items = list(rows).map((row, i) => ({ row, i, v: column && column.value ? column.value(row) : null }));
    if (!column || (dir !== 'asc' && dir !== 'desc')) return items.map(x => x.row);
    const sign = dir === 'desc' ? -1 : 1;
    items.sort((x, y) => {
      const ex = empty(x.v), ey = empty(y.v);
      if (ex || ey) return ex === ey ? x.i - y.i : ex ? 1 : -1;
      return sign * compare(x.v, y.v) || x.i - y.i;
    });
    return items.map(x => x.row);
  }
  // The order with `id` moved to index `to` (clamped).
  function move(order, id, to) {
    const out = list(order).filter(x => x !== id);
    if (out.length === list(order).length) return list(order).slice();
    out.splice(Math.max(0, Math.min(out.length, to)), 0, id);
    return out;
  }
  const nextSort = (sort, id) => !sort || sort.id !== id ? { id, dir: 'asc' } : sort.dir === 'asc' ? { id, dir: 'desc' } : null;

  /* ---------- DOM ---------- */
  const menusOpen = new Set();                              // tables whose Columns menu was open, kept across re-renders
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  const icon = (name) => window.ForgeIcons ? window.ForgeIcons.icon(name) : document.createTextNode('');

  function render(parent, opts) {
    const columns = list(opts.columns), byId = new Map(columns.map(c => [c.id, c]));
    let prefs = normPrefs(opts.prefs, columns);
    const save = next => { prefs = next; if (opts.savePrefs) opts.savePrefs({ order: next.order, hidden: next.hidden, sort: next.sort }); redraw(); };
    let current = null;

    function menu() {
      const btn = el('button', undefined, 'dt-columns-btn'); btn.type = 'button';
      btn.append(icon('columns'), el('span', 'Columns'));
      btn.title = 'Show, hide and reset this table\'s columns';
      const pop = el('div', undefined, 'dt-menu popover'); pop.popover = 'auto'; pop.id = 'dt-menu-' + opts.id;
      pop.setAttribute('aria-label', `Columns of ${opts.label || opts.id}`);
      btn.popoverTargetElement = pop; btn.setAttribute('aria-haspopup', 'true');
      const head = el('div', undefined, 'dt-menu-head'); head.append(el('span', 'Columns', 'plate'));
      const reset = el('button', 'Reset', 'dt-reset'); reset.type = 'button';
      reset.title = 'Default order, every column shown, no sort';
      reset.addEventListener('click', () => save(normPrefs({}, columns)));
      head.append(reset); pop.append(head);
      const ul = el('ul', undefined, 'dt-menu-list');
      prefs.order.forEach((id, i) => {
        const c = byId.get(id), li = el('li'), lab = el('label'), box = el('input');
        box.type = 'checkbox'; box.checked = !prefs.hidden.includes(id);
        box.disabled = columns[0].id === id || c.hideable === false;
        box.addEventListener('change', () => save({ ...prefs, hidden: box.checked ? prefs.hidden.filter(x => x !== id) : prefs.hidden.concat(id) }));
        lab.append(box, el('span', c.label));
        const up = el('button', undefined, 'dt-step'); up.type = 'button'; up.append(icon('chevron-up')); up.disabled = i === 0;
        up.setAttribute('aria-label', `Move ${c.label} left`); up.addEventListener('click', () => save({ ...prefs, order: move(prefs.order, id, i - 1) }));
        const down = el('button', undefined, 'dt-step'); down.type = 'button'; down.append(icon('chevron-down')); down.disabled = i === prefs.order.length - 1;
        down.setAttribute('aria-label', `Move ${c.label} right`); down.addEventListener('click', () => save({ ...prefs, order: move(prefs.order, id, i + 1) }));
        li.append(lab, up, down); ul.append(li);
      });
      pop.append(ul);
      pop.addEventListener('toggle', e => {
        if (e.newState === 'open') {
          menusOpen.add(opts.id);
          const r = btn.getBoundingClientRect(), w = pop.offsetWidth || 260;
          pop.style.top = Math.round(r.bottom + 6) + 'px';
          pop.style.left = Math.round(Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w))) + 'px';
        } else menusOpen.delete(opts.id);
      });
      return [btn, pop];
    }

    function header(shown) {
      const thead = el('thead'), tr = el('tr');
      shown.forEach((c, i) => {
        const th = el('th', undefined, c.cls || ''); th.scope = 'col'; th.dataset.col = c.id;
        if (c.title) th.title = c.title;
        const sorted = prefs.sort && prefs.sort.id === c.id;
        if (sorted) th.setAttribute('aria-sort', prefs.sort.dir === 'asc' ? 'ascending' : 'descending');
        th.draggable = true;
        const lead = c.sortable === false ? el('span', undefined, 'th-label') : el('button', undefined, 'th-btn');
        if (c.sortable !== false) {
          lead.type = 'button';
          lead.addEventListener('click', () => save({ ...prefs, sort: nextSort(prefs.sort, c.id) }));
        }
        lead.append(el('span', c.label, 'th-text'));
        if (c.sortable !== false) {
          const mark = el('span', undefined, 'th-sort' + (sorted ? ' on' : ''));
          mark.append(icon(sorted ? (prefs.sort.dir === 'asc' ? 'sort-asc' : 'sort-desc') : 'sort'));
          lead.append(mark);
        }
        lead.addEventListener('keydown', e => {
          if (!e.altKey || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
          e.preventDefault();
          const at = prefs.order.indexOf(c.id), to = e.key === 'ArrowLeft' ? at - 1 : at + 1;
          if (to < 0 || to >= prefs.order.length) return;
          save({ ...prefs, order: move(prefs.order, c.id, to) });
          current.querySelector(`th[data-col="${CSS.escape(c.id)}"] .th-btn, th[data-col="${CSS.escape(c.id)}"] .th-label`)?.focus();
        });
        th.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', c.id); e.dataTransfer.effectAllowed = 'move'; th.classList.add('dragging'); });
        th.addEventListener('dragend', () => { th.classList.remove('dragging'); for (const n of thead.querySelectorAll('.drop-before, .drop-after')) n.classList.remove('drop-before', 'drop-after'); });
        th.addEventListener('dragover', e => {
          e.preventDefault(); e.dataTransfer.dropEffect = 'move';
          const r = th.getBoundingClientRect(), after = e.clientX > r.left + r.width / 2;
          th.classList.toggle('drop-after', after); th.classList.toggle('drop-before', !after);
        });
        th.addEventListener('dragleave', () => th.classList.remove('drop-before', 'drop-after'));
        th.addEventListener('drop', e => {
          e.preventDefault();
          const id = e.dataTransfer.getData('text/plain'); if (!byId.has(id) || id === c.id) return;
          const r = th.getBoundingClientRect(), after = e.clientX > r.left + r.width / 2;
          const rest = prefs.order.filter(x => x !== id), at = rest.indexOf(c.id) + (after ? 1 : 0);
          save({ ...prefs, order: move(prefs.order, id, at) });
        });
        th.append(lead);
        if (i === 0) th.classList.add('dt-first');
        tr.append(th);
      });
      thead.append(tr);
      return thead;
    }

    function build() {
      const node = el('div', undefined, 'dt');
      node.dataset.table = opts.id;
      const shown = prefs.order.filter(id => !prefs.hidden.includes(id)).map(id => byId.get(id));
      const [btn, pop] = menu();
      if (opts.tools) opts.tools.replaceChildren(btn);
      else { const bar = el('div', undefined, 'dt-bar'); if (prefs.sort || prefs.hidden.length) bar.append(status()); bar.append(btn); node.append(bar); }
      node.append(pop);
      const wrap = el('div', undefined, 'table-scroll');
      if (opts.scrollKey) wrap.dataset.scroll = opts.scrollKey;
      wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', opts.label || opts.id);
      const table = el('table', undefined, opts.className || '');
      if (opts.label) table.append(el('caption', opts.label, 'visually-hidden'));
      table.append(header(shown));
      const body = el('tbody');
      const col = prefs.sort && byId.get(prefs.sort.id);
      for (const row of sortRows(opts.rows, col, prefs.sort && prefs.sort.dir)) {
        const tr = el('tr', undefined, opts.rowClass ? opts.rowClass(row) || '' : '');
        for (const c of shown) {
          let v = c.cell ? c.cell(row) : c.value ? c.value(row) : null;
          let td;
          if (v instanceof Node && v.nodeName === 'TD') td = v;
          else {
            td = el('td', undefined, [c.cls, c.mono ? 'mono' : ''].filter(Boolean).join(' '));
            td.append(v instanceof Node ? v : document.createTextNode(empty(v) ? '—' : String(v)));
          }
          td.dataset.col = c.id;
          tr.append(td);
        }
        if (opts.onRow) opts.onRow(row, tr);
        body.append(tr);
      }
      table.append(body); wrap.append(table);
      // a fade on each edge that has more table past it
      const frame = el('div', undefined, 'dt-frame'); frame.append(wrap); node.append(frame);
      const edges = () => { const end = wrap.scrollWidth - wrap.clientWidth; frame.classList.toggle('more-left', wrap.scrollLeft > 1); frame.classList.toggle('more-right', wrap.scrollLeft < end - 1); };
      wrap.addEventListener('scroll', edges, { passive: true });
      if (typeof ResizeObserver !== 'undefined') new ResizeObserver(edges).observe(wrap);
      if (menusOpen.has(opts.id)) queueMicrotask(() => { if (pop.isConnected && pop.showPopover) try { pop.showPopover(); } catch (_) { /* already open */ } });
      return node;
    }
    function status() {
      const s = el('span', undefined, 'dt-status muted');
      const bits = [];
      if (prefs.sort) bits.push(`sorted by ${byId.get(prefs.sort.id).label}, ${prefs.sort.dir === 'asc' ? 'ascending' : 'descending'}`);
      if (prefs.hidden.length) bits.push(`${prefs.hidden.length} hidden`);
      s.textContent = bits.join(' · ');
      return s;
    }
    function redraw() {
      const next = build();
      current.replaceWith(next); current = next;
    }
    current = build();
    parent.append(current);
    return current;
  }

  return { render, normPrefs, sortRows, compare, move, nextSort };
});
