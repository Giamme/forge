/* forge website, home page: the session that replays a run beside its 3D plate, the spec composer
   (from forge's own registry.tsv), the approval table, the steering desk and the run cloud.
   Every run shown is demo data. */

const reduce = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

/* ================================================================================================
   The demo run: five tasks in three waves, then the finish line.
   ================================================================================================ */
const RUN = {
  tasks: [
    { id: 'token-model', wave: 0, row: 0, difficulty: 'high', dwarf: 'sol:xhigh codex' },
    { id: 'middleware', wave: 0, row: 1, difficulty: 'medium', dwarf: 'sonnet:medium claude' },
    { id: 'login-route', wave: 1, row: 0, difficulty: 'medium', dwarf: 'gemini:high antigravity', deps: ['token-model', 'middleware'] },
    { id: 'rate-limit', wave: 1, row: 1, difficulty: 'medium', dwarf: 'grok:high grok', deps: ['middleware'] },
    { id: 'docs', wave: 2, row: 0.5, difficulty: 'low', dwarf: 'haiku:xhigh claude', deps: ['login-route'] },
    { id: 'integration', wave: 3, row: -0.15, finish: true, deps: ['docs', 'rate-limit'] },
    { id: 'verification', wave: 3, row: 0.5, finish: true, deps: ['integration'] },
    { id: 'your-branch', wave: 3, row: 1.15, finish: true, deps: ['verification'] },
  ],
};

// One line of the transcript and what it does to the plate. kind: cmd (typed), out, c (comment).
const S = (kind, html, wait, act, pin) => ({ kind, html, wait, act, pin });
const PRELANDED = 5; // the command and the plan are on screen from the start
const SCRIPT = [
  S('cmd', '<span class="p">$</span> /forge "add auth: tokens, middleware, login, rate limit, docs" \\', 300),
  S('cmd', '    --decompose-level medium --qa sonnet:high --qa-high opus:high \\', 120),
  S('cmd', '    --dwarf-high sol:xhigh --dwarf-low haiku:xhigh \\', 120),
  S('cmd', '    --dwarf-medium sonnet:medium,gemini:high:antigravity,grok:high', 120),
  S('c', '# planned 5 tasks in 3 waves, each routed by its difficulty', 700, p => p.reveal()),
  S('c', '# the approval table is on screen. nothing has been dispatched yet.', 1300),
  S('out', '<span class="p">&gt;</span> approve', 1200),
  S('out', '<span class="run">wave 1</span>  token-model   sol:xhigh on codex', 700, p => { p.setStatus('token-model', 'running'); }),
  S('out', '        middleware    sonnet:medium on claude', 450, p => { p.setStatus('middleware', 'running'); }),
  S('out', 'middleware   qa sonnet:high   <span class="ok">PASS</span>  merged', 2300, p => { p.setStatus('middleware', 'merged'); p.flow('middleware', null, true, 'merged'); }),
  S('out', 'token-model  qa opus:high     <span class="bad">FAIL</span>  <span class="v">P1</span> src/auth/tokens.py:32', 1500, p => { p.setStatus('token-model', 'fail'); p.beacon('token-model', true); }, true),
  S('c', '             digest comparison is not constant-time', 250),
  S('out', 'token-model  retry 2 with 1 finding, in the same worktree', 1700, p => { p.setStatus('token-model', 'running'); }),
  S('out', 'token-model  qa opus:high     <span class="ok">PASS</span>  merged', 2400, p => { p.beacon('token-model', false); p.setStatus('token-model', 'merged'); p.flow('token-model', null, true, 'merged'); }),
  S('out', '<span class="run">wave 2</span>  login-route   gemini:high on antigravity', 900, p => { p.setStatus('login-route', 'running'); }),
  S('out', '        rate-limit    grok:high on grok', 450, p => { p.setStatus('rate-limit', 'running'); }),
  S('out', 'rate-limit   qa sonnet:high   <span class="ok">PASS</span>  merged', 2100, p => { p.setStatus('rate-limit', 'merged'); p.flow('rate-limit', null, true, 'merged'); }),
  S('out', 'login-route  qa sonnet:high   <span class="ok">PASS</span>  merged', 900, p => { p.setStatus('login-route', 'merged'); p.flow('login-route', null, true, 'merged'); }),
  S('out', '<span class="run">wave 3</span>  docs          haiku:xhigh on claude', 800, p => { p.setStatus('docs', 'running'); }),
  S('out', 'docs         qa sonnet:high   <span class="ok">PASS</span>  merged', 1800, p => { p.setStatus('docs', 'merged'); p.flow('docs', null, true, 'merged'); p.setStatus('integration', 'merged'); }),
  S('out', 'verify       pytest -q        <span class="ok">PASS</span>  on the combined result', 1500, p => { p.flow('integration', null, true, 'merged'); p.setStatus('verification', 'merged'); }),
  S('c', '# 5 of 5 merged into forge/a1c4-integration. your branch is untouched.', 1100),
  S('cmd', '<span class="p">$</span> forge-parallel.sh integrate "$PLAN" --approved', 1500, p => { p.flow('verification', null, true, 'route'); p.setStatus('your-branch', 'route'); }),
];

function startSession(plate) {
  const body = $('.session-body');
  const replay = $('[data-replay]');
  const lamp = $('.session .module-head .lamp');
  const status = $('[data-session-status]');
  const pinBox = $('.session-pin'), pinText = $('[data-pin]');
  const pin = item => { if (!pinBox) return; if (item) { pinText.innerHTML = item.html; pinBox.classList.add('is-on'); } else pinBox.classList.remove('is-on'); };
  if (!body) return;
  let token = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const MAX = 22;

  function addLine(item, partial) {
    const ln = el('div', 'ln' + (item.kind === 'c' ? ' c' : ''));
    ln.innerHTML = partial ?? item.html;
    body.append(ln);
    while (body.children.length > MAX) body.firstChild.remove();
    return ln;
  }
  function setState(s, text) {
    lamp.className = 'lamp' + (s === 'run' ? ' is-amber is-blink' : s === 'done' ? ' is-green' : '');
    status.textContent = text;
  }

  async function typeLine(item, my) {
    // type the visible text, then swap in the marked-up line
    const tmp = document.createElement('div'); tmp.innerHTML = item.html; const text = tmp.textContent;
    const ln = addLine(item, '');
    const caret = el('span', 'caret');
    for (let i = 1; i <= text.length; i += 2) {
      if (my !== token) return;
      ln.textContent = text.slice(0, i); ln.append(caret);
      await sleep(14);
    }
    ln.innerHTML = item.html;
  }

  async function play() {
    const my = ++token;
    body.replaceChildren();
    plate && plate.reset();
    pin(null);
    setState('run', 'running');
    if (reduce()) {
      for (const item of SCRIPT) { addLine(item); item.act && plate && item.act(plate); if (item.pin) pin(item); }
      setState('done', '5 of 5 merged');
      return;
    }
    SCRIPT.slice(0, PRELANDED).forEach(item => addLine(item));
    await sleep(900);
    plate && plate.reveal();
    for (const item of SCRIPT.slice(PRELANDED)) {
      if (my !== token) return;
      await sleep(item.wait);
      if (my !== token) return;
      if (item.kind === 'cmd') await typeLine(item, my); else addLine(item);
      if (my !== token) return;
      item.act && plate && item.act(plate);
      if (item.pin) pin(item);
    }
    setState('done', '5 of 5 merged');
    await sleep(9000);
    if (my === token && document.visibilityState === 'visible') play();
  }
  replay && replay.addEventListener('click', () => play());

  // start when the stage is on screen
  const stage = $('.stage');
  let started = false;
  new IntersectionObserver(es => {
    if (!started && es[0].isIntersecting) { started = true; play(); }
  }, { threshold: 0.2 }).observe(stage);
}

/* ================================================================================================
   The spec composer: alias, effort and CLI, resolved exactly as registry.tsv says.
   ================================================================================================ */
const LADDERS = {
  codex: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  claude: ['low', 'medium', 'high', 'xhigh', 'max'],
  openclaude: ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode'],
  antigravity: ['low', 'medium', 'high'],
  grok: ['low', 'medium', 'high', 'xhigh', 'max'],
  opencode: null,
};
const HARNESSES = ['codex', 'claude', 'openclaude', 'opencode', 'antigravity', 'grok'];
const BINARY = { codex: 'codex', claude: 'claude', openclaude: 'openclaude', opencode: 'opencode', antigravity: 'agy', grok: 'grok' };
const ROLE_DEFAULT = { dwarf: 'medium', qa: 'high' };

function resolveEffort(row, want) {
  const ladder = LADDERS[row.harness];
  const form = row.effort;
  if (form === '-') return { effort: want, note: `${row.harness} has no validated ladder: forwarded as written` };
  if (form === 'none') return { effort: null, note: `this model takes no effort flag on ${row.harness}: forge drops it` };
  if (form.startsWith('<=')) {
    const cap = form.slice(2), ci = ladder.indexOf(cap), wi = ladder.indexOf(want);
    if (wi > ci) return { effort: cap, note: `clamped=${want} -> ${cap}` };
    return { effort: want, note: '' };
  }
  const allowed = form.split(',');
  if (allowed.includes(want)) return { effort: want, note: '' };
  const wi = ladder.indexOf(want);
  const lower = allowed.filter(a => ladder.indexOf(a) < wi).sort((a, b) => ladder.indexOf(b) - ladder.indexOf(a))[0];
  const pick = lower || allowed[0];
  return { effort: pick, note: `clamped=${want} -> ${pick} (offers ${allowed.join(', ')})` };
}

async function startComposer() {
  const root = $('[data-composer]');
  if (!root) return;
  let rows = [];
  try { rows = await (await fetch(root.dataset.registry)).json(); } catch { root.hidden = true; return; }
  const aliases = [...new Set(rows.map(r => r.alias))];
  const st = { role: 'dwarf', alias: 'sol', harness: null, effort: 'xhigh' };
  const banks = { role: $('[data-bank="role"]', root), alias: $('[data-bank="alias"]', root), harness: $('[data-bank="harness"]', root), effort: $('[data-bank="effort"]', root) };

  function btn(bank, value, label, pressed, disabled, extra) {
    const b = el('button', extra || '');
    b.type = 'button'; b.dataset.value = value;
    b.setAttribute('aria-pressed', String(pressed)); b.disabled = !!disabled;
    b.append(el('span', 'lamp'), document.createTextNode(label));
    bank.append(b);
  }
  function render() {
    const forAlias = rows.filter(r => r.alias === st.alias);
    const def = forAlias[0];
    const row = forAlias.find(r => r.harness === st.harness) || def;
    st.harness = row.harness;
    for (const k of Object.keys(banks)) banks[k].replaceChildren();
    btn(banks.role, 'dwarf', 'dwarf builds', st.role === 'dwarf');
    btn(banks.role, 'qa', 'qa reviews', st.role === 'qa');
    for (const a of aliases) btn(banks.alias, a, a, a === st.alias);
    for (const h of HARNESSES) btn(banks.harness, h, h, h === st.harness, !forAlias.some(r => r.harness === h));
    const ladder = LADDERS[row.harness] || ['low', 'medium', 'high', 'xhigh', 'max'];
    btn(banks.effort, 'default', 'default', st.effort === 'default');
    for (const e of ladder) {
      const res = row.effort.startsWith('<=') || row.effort.includes(',') ? resolveEffort(row, e) : { effort: e, note: '' };
      btn(banks.effort, e, e, st.effort === e, false, res.note.startsWith('clamped') ? 'is-clamped' : '');
    }

    const want = st.effort === 'default' ? ROLE_DEFAULT[st.role] : st.effort;
    const res = resolveEffort(row, want);
    const out = $('[data-out]', root);
    const flag = $('.flag', out); flag.replaceChildren();
    flag.append(el('span', '', `--${st.role} `), el('span', 'a', st.alias));
    const showEffort = st.effort !== 'default';
    const showHarness = row !== def;
    if (showEffort || showHarness) { flag.append(el('span', 'colon', ':')); if (showEffort) flag.append(el('span', 'e', st.effort)); }
    if (showHarness) flag.append(el('span', 'colon', ':'), el('span', 'h', row.harness));
    $('[data-r="model"]', out).textContent = row.model;
    $('[data-r="cli"]', out).textContent = `${row.harness} (${BINARY[row.harness]})${row === def ? ', its default CLI' : ''}`;
    $('[data-r="effort"]', out).textContent = res.effort ? `${res.effort}${st.effort === 'default' ? ` (the ${st.role} default)` : ''}` : 'none';
    $('[data-r="note"]', out).textContent = res.note || (st.role === 'qa' ? 'a reviewer thinking less than its builder tends to wave things through' : '');
  }
  root.addEventListener('click', ev => {
    const b = ev.target.closest('button[data-value]'); if (!b || b.disabled) return;
    const bank = b.parentElement.dataset.bank;
    st[bank] = b.dataset.value;
    if (bank === 'alias') st.harness = null;
    render();
  });
  render();
}

/* ================================================================================================
   The approval table: retier a row and watch its models change; approve and it runs.
   ================================================================================================ */
const BALANCED = {
  low: { dwarf: ['haiku', 'xhigh'], qa: ['sonnet', 'medium'] },
  medium: { dwarf: ['sonnet', 'medium'], qa: ['sonnet', 'high'] },
  high: { dwarf: ['opus', 'high'], qa: ['opus', 'high'] },
};
function startApproval() {
  const t = $('[data-approval]');
  if (!t) return;
  const plateEl = (alias, effort) => { const s = el('span', 'mplate'); s.append(el('b', '', alias), el('i', '', effort)); return s; };
  const countEl = $('[data-count]', t);
  function paint(tr) {
    const tier = tr.dataset.tier, r = BALANCED[tier];
    tr.querySelector('[data-dwarf]').replaceChildren(plateEl(...r.dwarf));
    tr.querySelector('[data-qa]').replaceChildren(plateEl(...r.qa));
    tr.querySelectorAll('.bank button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.tier === tier)));
  }
  const rows = [...t.querySelectorAll('tbody tr')];
  rows.forEach(tr => { tr.dataset.orig = tr.dataset.tier; paint(tr); });
  function count() {
    const by = { low: 0, medium: 0, high: 0 };
    rows.forEach(tr => by[tr.dataset.tier]++);
    countEl.textContent = `${rows.length} tasks: ${by.high} high, ${by.medium} medium, ${by.low} low. ${rows.length * 2} dispatches before any retry. Nothing spent yet.`;
  }
  t.addEventListener('click', async ev => {
    const b = ev.target.closest('button[data-tier]');
    if (b) { const tr = b.closest('tr'); tr.dataset.tier = b.dataset.tier; tr.classList.toggle('is-retiered', tr.dataset.tier !== tr.dataset.orig); paint(tr); count(); return; }
    const go = ev.target.closest('[data-approve]');
    if (!go) return;
    go.disabled = true;
    const waves = [...new Set(rows.map(r => r.dataset.wave))];
    rows.forEach(r => r.querySelector('.lamp').className = 'lamp');
    for (const w of waves) {
      const these = rows.filter(r => r.dataset.wave === w);
      these.forEach(r => r.querySelector('.lamp').className = 'lamp is-amber is-blink');
      await new Promise(res => setTimeout(res, reduce() ? 0 : 1100));
      these.forEach(r => r.querySelector('.lamp').className = 'lamp is-green');
    }
    countEl.textContent = 'Demo run finished: every task reviewed and merged on forge\'s own branch. Install forge to approve a real one.';
    go.disabled = false;
  });
  count();
}

/* ================================================================================================
   The steering desk: each control prints the forge control command and its log line.
   ================================================================================================ */
const CONTROLS = {
  drain: ['forge control drain "$PLAN"', 'drain: start nothing new; 2 running tasks finish, then the run waits'],
  resume: ['forge control resume "$PLAN"', 'resume: the scheduler starts eligible tasks again'],
  pin: ['forge control pin "$PLAN" token-model sol:high:codex', 'pin: token-model\'s next full retry runs on sol:high:codex, once'],
  retry: ['forge control retry "$PLAN" docs --dwarf opus:high', 'retry queued for docs (request 7); applied at the next pass'],
  accept: ['forge control accept "$PLAN" tooling --reason "test-only P2" --approved', 'accept queued for tooling: findings kept as known issues, dependents start'],
  stop: ['forge control stop "$PLAN"', 'stop: running tasks finish, nothing new starts, the run ends (exit 130)'],
};
function startSteer() {
  const box = $('[data-steer]'); if (!box) return;
  const out = $('pre', box);
  box.addEventListener('click', ev => {
    const b = ev.target.closest('button[data-control]'); if (!b) return;
    box.querySelectorAll('button[data-control]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    const [cmd, log] = CONTROLS[b.dataset.control];
    out.innerHTML = '';
    const a = el('span', 'p', '$ '); out.append(a, document.createTextNode(cmd + '\n'));
    out.append(el('span', 'c', '# control.log  ui  ' + log));
  });
}

/* ================================================================================================
   The run cloud: demo runs across five repositories.
   ================================================================================================ */
function demoRuns() {
  const projects = {
    'payments-api': ['Reconcile payouts against the bank file', 'Idempotent refund endpoint', 'Retry webhooks with backoff', 'Split the ledger service', 'Add currency rounding rules'],
    'catalog-service': ['Paginate the product search', 'Remove the legacy XML exporter', 'Cache category trees per tenant', 'Typed SKU parser', 'Bulk price import'],
    'web-dashboard': ['Saved filters for the reports page', 'Dark theme tokens', 'Virtualise the orders table', 'Keyboard shortcuts for review', 'Chart export to CSV'],
    'deploy-cli': ['Parallel uploads in the asset sync', 'Dry-run for rollbacks', 'Config loader to TOML', 'Signed release artifacts', 'Progress bars that respect CI'],
    'auth-service': ['Add auth: token model, middleware, login route', 'Rotate signing keys', 'Rate limit the login route', 'Session revocation list', 'Audit log for admin actions'],
  };
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const runs = [];
  for (const [project, goals] of Object.entries(projects)) {
    const count = 12 + Math.floor(rnd() * 5);
    for (let i = 0; i < count; i++) {
      const r = rnd();
      runs.push({ project, idx: i, count, goal: goals[i % goals.length], tasks: 1 + Math.floor(rnd() * 8),
        state: i >= count - 1 && r < 0.6 ? 'running' : r < 0.12 ? 'failed' : 'done', day: Math.floor(rnd() * 30) });
    }
  }
  return runs;
}

/* ================================================================================================
   Boot: the 3D comes from three.js on a CDN; without it (or without WebGL) the page stays whole.
   ================================================================================================ */
async function boot() {
  startComposer(); startApproval(); startSteer();
  let mod = null;
  try {
    const probe = document.createElement('canvas');
    if (!(probe.getContext('webgl2') || probe.getContext('webgl'))) throw new Error('no webgl');
    mod = await import('./plate.js');
  } catch {
    document.querySelectorAll('.stage, .cloud').forEach(n => n.classList.add('is-flat'));
  }
  let plate = null;
  const stage = $('.stage');
  if (mod && stage) {
    try { plate = await mod.createPlate($('.stage-canvas'), $('.stage-tags'), RUN); } catch { stage.classList.add('is-flat'); }
  }
  startSession(plate);

  const cloudHost = $('[data-cloud]');
  if (mod && cloudHost) {
    const card = $('.cloud-card');
    const runs = demoRuns();
    const show = r => {
      if (!r) { card.hidden = true; return; }
      card.hidden = false;
      $('[data-c="goal"]', card).textContent = r.goal;
      $('[data-c="meta"]', card).textContent = `${r.project}  ${r.tasks} task${r.tasks > 1 ? 's' : ''}  ${r.state === 'done' ? 'merged' : r.state}`;
      $('.lamp', card).className = 'lamp ' + (r.state === 'running' ? 'is-amber is-blink' : r.state === 'failed' ? 'is-red' : 'is-green');
    };
    let cloud = null;
    new IntersectionObserver(async (es, obs) => {
      if (!es[0].isIntersecting) return; obs.disconnect();
      try { cloud = await mod.createCloud(cloudHost, runs, { onPick: show }); } catch { cloudHost.closest('.cloud').classList.add('is-flat'); }
    }, { rootMargin: '200px' }).observe(cloudHost);
    $('[data-layouts]').addEventListener('click', ev => {
      const b = ev.target.closest('button[data-layout]'); if (!b || !cloud) return;
      ev.currentTarget.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
      cloud.setLayout(b.dataset.layout);
    });
  }
}
boot();
