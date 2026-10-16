/* forge-ui graph: tasks as SVG nodes in wave columns, dependencies as edges.
   Defines window.ForgeGraph.render(container, run, {selected, onSelect}). */
(function () {
  'use strict';
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  var NS = 'http://www.w3.org/2000/svg';
  var NODE_W = 176, NODE_H = 46, COL_GAP = 72, ROW_GAP = 14;
  var PAD = 16, HEADER_H = 26;
  var uid = 0;

  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    if (attrs) for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function textEl(parent, str, attrs) {
    var t = el('text', attrs, parent);
    t.textContent = str;
    return t;
  }

  // Conservative per-glyph advance (in em) so wide letters cannot overflow the node.
  function glyphEm(c) {
    if (/[WM@%]/.test(c)) return 1.0;
    if (/[mw]/.test(c)) return 0.9;
    if (/[A-Z0-9_#&$]/.test(c)) return 0.75;
    if (/[il.,:;'|!\[\]() \-]/.test(c)) return 0.4;
    if (c.charCodeAt(0) > 0x7e) return 1.1; // em dash, ellipsis, CJK, emoji: about one em or more
    return 0.64;
  }

  // Cut s so its estimated width at fontSize (times extra, e.g. 1.1 for bold) fits maxPx.
  function ellipsize(s, maxPx, fontSize, extra) {
    s = String(s);
    var k = fontSize * (extra || 1), w = 0, i, total = 0;
    for (i = 0; i < s.length; i++) total += glyphEm(s.charAt(i)) * k;
    if (total <= maxPx) return s;
    var room = maxPx - glyphEm('…') * k;
    for (i = 0; i < s.length; i++) {
      var cw = glyphEm(s.charAt(i)) * k;
      if (w + cw > room) break;
      w += cw;
    }
    return s.slice(0, Math.max(1, i)) + '…';
  }

  function str(v, dflt) {
    return v === null || v === undefined || v === '' ? dflt : String(v);
  }

  function message(container, text) {
    var p = document.createElement('p');
    p.className = 'fg-empty';
    p.textContent = text;
    container.appendChild(p);
  }

  // Column (1-based) of every task: its wave, else one right of its deepest dep.
  function computeColumns(tasks, byId) {
    var col = Object.create(null), i, pass;
    for (i = 0; i < tasks.length; i++) {
      var w = Math.floor(Number(tasks[i].wave));
      col[tasks[i].id] = isFinite(w) && w >= 1 && tasks[i].wave !== null ? w : 0;
    }
    var floating = tasks.filter(function (t) { return col[t.id] === 0; });
    floating.forEach(function (t) { col[t.id] = 1; });
    for (pass = 0; pass < tasks.length; pass++) {
      var changed = false;
      for (i = 0; i < floating.length; i++) {
        var t = floating[i], deepest = 0;
        var deps = Array.isArray(t.deps) ? t.deps : [];
        for (var d = 0; d < deps.length; d++) {
          if (deps[d] === t.id || !byId[deps[d]]) continue;
          if (col[deps[d]] > deepest) deepest = col[deps[d]];
        }
        var want = deepest ? deepest + 1 : 1;
        if (want !== col[t.id]) { col[t.id] = want; changed = true; }
      }
      if (!changed) break;
    }
    return col;
  }

  function edgePath(a, b, self) {
    var x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    var x2 = b.x, y2 = b.y + NODE_H / 2;
    if (self) {
      return 'M' + x1 + ' ' + y1 + ' C' + (x1 + 34) + ' ' + (y1 - NODE_H) + ' ' +
        (x2 - 34) + ' ' + (y2 - NODE_H) + ' ' + x2 + ' ' + y2;
    }
    var dx = x2 - x1;
    var k = dx >= 2 * 30 ? dx / 2 : 60; // backward/same-column edges bulge out so they stay visible
    return 'M' + x1 + ' ' + y1 + ' C' + (x1 + k) + ' ' + y1 + ' ' + (x2 - k) + ' ' + y2 + ' ' + x2 + ' ' + y2;
  }

  function render(container, run, options) {
    if (!container) return;
    options = options || {};
    while (container.firstChild) container.removeChild(container.firstChild);

    var tasks = run && Array.isArray(run.tasks) ? run.tasks.filter(function (t) {
      return t && t.id !== null && t.id !== undefined;
    }) : [];
    if (!tasks.length) { message(container, 'No tasks'); return; }

    var byId = Object.create(null);
    tasks.forEach(function (t) { if (!byId[t.id]) byId[t.id] = t; });
    var col = computeColumns(tasks, byId);

    // Densify columns: only waves actually used get a column, so a huge wave number can't blow up the layout.
    var used = Object.create(null), waves = [];
    tasks.forEach(function (t) { if (!used[col[t.id]]) { used[col[t.id]] = true; waves.push(col[t.id]); } });
    waves.sort(function (a, b) { return a - b; });
    var idx = Object.create(null);
    waves.forEach(function (w, i) { idx[w] = i + 1; });
    var label = {};
    waves.forEach(function (w) { label[idx[w]] = w; });
    tasks.forEach(function (t) { col[t.id] = idx[col[t.id]]; });

    var maxCol = 1, rows = Object.create(null), pos = Object.create(null), maxRows = 0;
    tasks.forEach(function (t) { if (col[t.id] > maxCol) maxCol = col[t.id]; });
    tasks.forEach(function (t) {
      if (pos[t.id]) return;
      var c = col[t.id];
      var r = rows[c] = (rows[c] || 0);
      rows[c] = r + 1;
      if (rows[c] > maxRows) maxRows = rows[c];
      pos[t.id] = {
        x: PAD + (c - 1) * (NODE_W + COL_GAP),
        y: PAD + HEADER_H + r * (NODE_H + ROW_GAP)
      };
    });

    var width = PAD * 2 + maxCol * NODE_W + (maxCol - 1) * COL_GAP;
    var height = PAD * 2 + HEADER_H + maxRows * NODE_H + (maxRows - 1) * ROW_GAP + 20;

    var svg = el('svg', {
      'class': 'fg-svg', viewBox: '0 0 ' + width + ' ' + height,
      width: width, height: height, role: 'group', 'aria-label': 'Task dependency graph'
    });
    svg.style.maxWidth = 'none';

    var markerId = 'fg-arrow-' + (++uid);
    var defs = el('defs', null, svg);
    var marker = el('marker', {
      id: markerId, viewBox: '0 0 10 10', refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: 'auto'
    }, defs);
    el('path', { d: 'M0 0 L10 5 L0 10 z', fill: 'currentColor', 'fill-opacity': 0.6 }, marker);

    for (var c = 1; c <= maxCol; c++) {
      textEl(svg, 'wave ' + label[c], {
        'class': 'fg-wave', x: PAD + (c - 1) * (NODE_W + COL_GAP), y: PAD + 10,
        'font-size': 12, fill: 'currentColor', 'fill-opacity': 0.6
      });
    }

    var edgeLayer = el('g', { 'class': 'fg-edges', color: 'inherit' }, svg);
    var edgesOf = Object.create(null);
    var seen = Object.create(null);
    tasks.forEach(function (t) {
      if (seen[t.id]) return;
      seen[t.id] = true;
      var deps = Array.isArray(t.deps) ? t.deps : [];
      var done = Object.create(null);
      deps.forEach(function (d) {
        if (!byId[d] || done[d]) return;
        done[d] = true;
        var path = el('path', {
          'class': 'fg-edge', d: edgePath(pos[d], pos[t.id], d === t.id),
          fill: 'none', stroke: 'currentColor', 'stroke-opacity': 0.4,
          'stroke-width': 1.5, 'marker-end': 'url(#' + markerId + ')'
        }, edgeLayer);
        var ends = { path: path, from: d, to: t.id };
        (edgesOf[t.id] = edgesOf[t.id] || []).push(ends);
        if (d !== t.id) (edgesOf[d] = edgesOf[d] || []).push(ends);
      });
    });

    var active = Object.create(null); // id -> {hover, focus}
    function highlight(id, kind, flag) {
      var a = active[id] = active[id] || {};
      a[kind] = flag;
      (edgesOf[id] || []).forEach(function (e) {
        var p = e.path;
        var fa = active[e.from], ta = active[e.to];
        var on = !!((fa && (fa.hover || fa.focus)) || (ta && (ta.hover || ta.focus)));
        p.setAttribute('stroke-opacity', on ? 0.95 : 0.4);
        p.setAttribute('stroke-width', on ? 2.5 : 1.5);
        if (on) p.setAttribute('class', 'fg-edge fg-edge-hi');
        else p.setAttribute('class', 'fg-edge');
      });
    }

    var drawn = Object.create(null);
    tasks.forEach(function (t) {
      if (drawn[t.id]) return;
      drawn[t.id] = true;
      var p = pos[t.id];
      var status = str(t.status, 'pending');
      var stClass = 'st-' + status.toLowerCase().replace(/[^a-z0-9_-]/g, '');
      var selected = options.selected === t.id;
      var id = String(t.id);

      var g = el('g', {
        'class': 'fg-node ' + stClass + (selected ? ' fg-selected' : ''),
        transform: 'translate(' + p.x + ' ' + p.y + ')',
        tabindex: 0, role: 'button',
        'aria-label': 'Task ' + id + ', status ' + status,
        'data-id': id
      }, svg);
      g.style.cursor = 'pointer';

      var tip = [str(t.title, id), 'status: ' + status,
        'dwarf: ' + str(t.dwarf, '—') + ' / qa: ' + str(t.qa, '—')];
      if (t.verdict) tip.push('verdict: ' + t.verdict);
      if (t.explored) tip.push('explored');
      var title = el('title', null, g);
      title.textContent = tip.join('\n');

      el('rect', {
        'class': 'fg-box', x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 8, ry: 8,
        style: 'fill:var(--st, #888);fill-opacity:0.12;stroke:var(--st, #888);',
        'stroke-width': selected ? 3.5 : 1.5,
        'stroke-dasharray': t.explored ? '5 3' : 'none'
      }, g);
      el('rect', {
        x: 0, y: 6, width: 4, height: NODE_H - 12, rx: 2,
        style: 'fill:var(--st, #888);'
      }, g);

      var maxPx = NODE_W - 36;
      textEl(g, ellipsize(id, maxPx, 13, 1.1), {
        x: 12, y: 19, 'font-size': 13, 'font-weight': 'bold', fill: 'currentColor'
      });
      var spec = t.explored ? ellipsize(str(t.dwarf, '—'), maxPx - 12, 12) + ' *' : ellipsize(str(t.dwarf, '—'), maxPx, 12);
      textEl(g, spec, {
        x: 12, y: 36, 'font-size': 12, fill: 'currentColor', 'fill-opacity': 0.75
      });

      if (t.verdict === 'PASS' || t.verdict === 'FAIL') {
        var pass = t.verdict === 'PASS';
        textEl(g, pass ? '✓' : '✗', {
          x: NODE_W - 8, y: 17, 'text-anchor': 'end', 'font-size': 14, 'font-weight': 'bold',
          style: 'fill:' + (pass ? 'var(--st, #888)' : 'currentColor') + ';'
        });
      }

      function select() { if (typeof options.onSelect === 'function') options.onSelect(t.id); }
      g.addEventListener('click', select);
      g.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
          ev.preventDefault();
          select();
        }
      });
      g.addEventListener('mouseenter', function () { highlight(t.id, 'hover', true); });
      g.addEventListener('mouseleave', function () { highlight(t.id, 'hover', false); });
      g.addEventListener('focus', function () { highlight(t.id, 'focus', true); });
      g.addEventListener('blur', function () { highlight(t.id, 'focus', false); });
    });

    container.appendChild(svg);
  }

  window.ForgeGraph = { render: render };
})();
