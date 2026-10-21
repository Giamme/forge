/* forge-ui icons: one hairline set drawn for forge-ui on a 24-unit grid, 1.5 stroke, round joins, currentColor.
   States come from CSS (colour, opacity); an icon never carries its own colour. Built with createElementNS: the page CSP
   refuses markup parsed from strings. Also the model name plate, the one way a model is shown everywhere.
   window.ForgeIcons = { icon(name, title?) → <svg class="icon icon-<name>">, modelPlate(spec) → <span class="model-plate">, names } */
window.ForgeIcons = (() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const P = d => ['path', { d }], C = (cx, cy, r) => ['circle', { cx, cy, r }], R = (x, y, width, height, rx = 1.5) => ['rect', { x, y, width, height, rx }];
  const SET = {
    map: [R(3, 4, 18, 16, 2), P('M7 9h4v3H7zM13 9h4v6h-4zM7 14h4v2H7')],
    cloud: [C(12, 12, 2.2), P('M3.5 12c0-2.5 3.8-4.5 8.5-4.5s8.5 2 8.5 4.5-3.8 4.5-8.5 4.5S3.5 14.5 3.5 12Z'), C(18.5, 9.2, 1)],
    models: [P('M5 20V11M10 20V5M15 20v-7M20 20V8M3 20h18')],
    search: [C(10.5, 10.5, 6), P('m15 15 5.5 5.5')],
    bell: [P('M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15Z'), P('M10 20.5a2.2 2.2 0 0 0 4 0')],
    live: [C(12, 12, 2), P('M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14')],
    sun: [C(12, 12, 3.8), P('M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4')],
    moon: [P('M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z')],
    system: [R(3, 4.5, 18, 12, 1.5), P('M9 20h6M12 16.5V20')],
    immersive: [R(3, 4, 18, 16, 2), R(5.5, 6.5, 4, 8, 1), R(14.5, 6.5, 4, 11, 1)],
    workbench: [R(3, 4, 18, 16, 2), P('M8 4v16M16 4v16M8 15h8')],
    'panel-left': [R(3, 4, 18, 16, 2), P('M9 4v16')],
    'panel-right': [R(3, 4, 18, 16, 2), P('M15 4v16')],
    'panel-bottom': [R(3, 4, 18, 16, 2), P('M3 14h18')],
    close: [P('M6 6l12 12M18 6 6 18')],
    'chevron-down': [P('m6 9 6 6 6-6')],
    'chevron-up': [P('m6 15 6-6 6 6')],
    'chevron-right': [P('m9 6 6 6-6 6')],
    'chevron-left': [P('m15 6-6 6 6 6')],
    play: [P('M8 5.5v13l10-6.5Z')],
    pause: [P('M8 5.5v13M16 5.5v13')],
    plan: [P('M4 5v14M10 5v14M16 5v14'), R(5.5, 7, 3, 3, .8), R(11.5, 9, 3, 3, .8), R(11.5, 14, 3, 3, .8), R(17.5, 11, 3, 3, .8)],
    exec: [P('M3 5v14'), R(5, 6, 8, 3, 1), R(9, 10.5, 10, 3, 1), R(6, 15, 6, 3, 1)],
    files: [P('M6 3.5h8l4 4v13H6Z'), P('M14 3.5v4h4M9 12h6M9 15.5h6')],
    cube: [P('M12 3 20 7.5v9L12 21l-8-4.5v-9Z'), P('M4 7.5 12 12l8-4.5M12 12v9')],
    square: [R(5, 5, 14, 14, 1.5), P('M5 12h14M12 5v14')],
    layers: [P('m12 4 9 4.5-9 4.5-9-4.5Z'), P('m3 12.5 9 4.5 9-4.5'), P('m3 16.5 9 4.5 9-4.5')],
    fit: [P('M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5'), R(9, 9, 6, 6, 1)],
    persp: [P('M3.5 19 8.5 5h7l5 14Z'), P('M10.5 5 9.5 19M13.5 5l1 14M6 12h12')],
    iso: [P('M12 4.5 18.5 8v8L12 19.5 5.5 16V8Z'), P('M5.5 8 12 11.5 18.5 8M12 11.5v8')],
    top: [R(4, 4, 16, 16, 2), R(8.5, 8.5, 7, 7, 1)],
    reset: [P('M4.5 12a7.5 7.5 0 1 0 2.2-5.3'), P('M4.5 4.5v3.5H8')],
    'zoom-in': [C(11, 11, 6.5), P('m16 16 4.5 4.5M8.5 11h5M11 8.5v5')],
    'zoom-out': [C(11, 11, 6.5), P('m16 16 4.5 4.5M8.5 11h5')],
    fullscreen: [P('M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5')],
    legend: [C(6, 7, 1.5), C(6, 12, 1.5), C(6, 17, 1.5), P('M10 7h9M10 12h9M10 17h6')],
    help: [C(12, 12, 9), P('M9.5 9.5a2.6 2.6 0 0 1 5 1c0 1.8-2.5 2.2-2.5 3.8'), C(12, 17, .6)],
    sort: [P('M8 5v14M5 8l3-3 3 3M16 19V5M13 16l3 3 3-3')],
    'sort-asc': [P('M12 19V5M7 10l5-5 5 5')],
    'sort-desc': [P('M12 5v14M7 14l5 5 5-5')],
    columns: [R(3, 4, 18, 16, 2), P('M9 4v16M15 4v16')],
    grip: [C(9, 7, .9), C(15, 7, .9), C(9, 12, .9), C(15, 12, .9), C(9, 17, .9), C(15, 17, .9)],
    download: [P('M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14')],
    flag: [P('M6 21V4M6 4.5h11l-2.5 4 2.5 4H6')],
    check: [P('m5 12.5 4.5 4.5L19 7.5')],
    cross: [P('M6 6l12 12M18 6 6 18')],
    filter: [P('M4 5h16l-6 7.5V19l-4-2v-4.5Z')],
    arrow: [P('M5 12h14M13 6l6 6-6 6')],
    eye: [P('M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z'), C(12, 12, 3)],
    route: [C(5, 17, 2), C(19, 7, 2), P('M7 17h4l2-10h4')],
    keyboard: [R(2.5, 6, 19, 12, 2), P('M6 10h1M9.5 10h1M13 10h1M16.5 10h1M7 14h10')],
    chat: [P('M4 5.5h16v10.5H10l-4.5 3.5V16H4Z'), P('M8 9.5h8M8 12.5h5')],
    terminal: [R(3, 4.5, 18, 15, 2), P('m7 9.5 3 2.5-3 2.5M12.5 15H17')],
    pencil: [P('M4.5 19.5 5.5 15 15.5 5a2.1 2.1 0 0 1 3 3l-10 10Z'), P('m13.5 7 3 3')],
    tool: [P('M14.5 5.5a4 4 0 0 0-5 5L4.5 15.5a2.1 2.1 0 0 0 3 3l5-5a4 4 0 0 0 5-5l-2.4 2.4-2.4-.6-.6-2.4Z')],
    thought: [P('M7 15.5a5 5 0 1 1 8.5-3.5 4 4 0 0 1 2 7.5H8a3 3 0 0 1-1-4Z'), C(6, 20.5, .8)],
  };
  function icon(name, title) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'icon icon-' + name);
    svg.setAttribute('focusable', 'false');
    if (title) { const t = document.createElementNS(NS, 'title'); t.textContent = title; svg.append(t); svg.setAttribute('role', 'img'); }
    else svg.setAttribute('aria-hidden', 'true');
    for (const [tag, attrs] of SET[name] || SET.square) {
      const n = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
      svg.append(n);
    }
    return svg;
  }
  // a model's name plate: the alias engraved, the effort beside it ("luna:high" → LUNA | high); a bare id stays whole
  function modelPlate(spec) {
    const s = String(spec || '').trim(), i = s.lastIndexOf(':'), out = document.createElement('span');
    out.className = 'model-plate'; out.title = s;
    if (!s) { out.textContent = '—'; out.className = 'muted'; return out; }
    const name = document.createElement('span'); name.className = 'mp-name'; name.textContent = i > 0 ? s.slice(0, i) : s; out.append(name);
    if (i > 0 && i < s.length - 1) {                    // the colon stays in the text, for copying and screen readers
      const c = document.createElement('span'); c.className = 'visually-hidden'; c.textContent = ':';
      const e = document.createElement('span'); e.className = 'mp-effort'; e.textContent = s.slice(i + 1); out.append(c, e);
    }
    return out;
  }
  return { icon, modelPlate, names: Object.keys(SET) };
})();
