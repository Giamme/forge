/* forge-ui settings view (window.ForgeSettings, and a CommonJS module under node:test). Route #/settings: the model
   registry (read-only), the decision-model gates, named profiles, the three per-repo .forge files of one repository,
   the dataset and the FORGE_* environment. The pure helpers (request bodies, value checks, the decision-model view,
   the profile summary, the repository picker, the editors' unsaved-text state) are tested in
   ui/tests/js/settings.test.js; render(host, ctx) is thin DOM over them. Every value from the server enters the page
   as text.
   ctx = { key (the run whose repo to show, or null), runs() (the run list), onPick?(key) }. render returns { poll() }:
   the page calls it on each refresh, and unsaved editor text survives it.
   The calls are GET /api/config?run=<key> and POST /api/config, see ui/docs/api.md, "Controls (write endpoints)". A write
   changes a file in the repository's working tree, or a decision-model gate in forge's user config, and is only offered
   with controls on. */
(function (root, factory) {
  'use strict';
  const RM = typeof module === 'object' && module.exports ? require('./runmodel.js') : root.ForgeRunModel;
  const api = factory(root, RM);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeSettings = api;
})(typeof self !== 'undefined' ? self : this, function (root, RM) {
  'use strict';

  const FILES = ['verify', 'setup', 'verify-subset'];
  const VALUE_MAX = 4096;
  const DECIDE_VALUE_MAX = 256;
  // The same names as forge_decide.GATES. The section renders this list, not whatever keys a payload happens to carry.
  const GATES = ['drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover',
    'coverage', 'retry-addressed', 'dwarf-message', 'attribution', 'findings'];
  const MODES = ['off', 'shadow', 'on'];
  const ADD_COMMAND = 'forge decide provider add …';
  // What turning a gate on means, per gate: references/decision-models.md in words a first-time reader can act on. `sends`
  // follows the state the site builds in scripts/forge_jev/; a change there changes what a user agreed to send.
  const GATE_HELP = {
    drift: { summary: 'Predicts files a task will edit but did not declare, so two tasks that touch one file do not run at once.',
      when: 'At plan, once per task.', sends: "The goal, and the task's title, declared files, prompt and approach.",
      on: "Adds each predicted file to the task's files before the waves are computed." },
    forward: { summary: 'Spots an earlier task written against something a later task builds.',
      when: 'At plan, once.', sends: 'The title, files and prompt of the tasks in each pair it checks.',
      on: 'Makes the earlier task wait for the later one, unless that forms a cycle.' },
    prompt: { summary: "Checks that a reviewer could judge a task from the task's own text.",
      when: 'At plan, once per task.', sends: "The goal, the task and its prompt and approach, the plan's other tasks, excerpts of its files, and project memory.",
      on: 'Sends a task whose prompt falls short back to the planner, once.' },
    'memory-curate': { summary: 'Keeps project memory clean as reviewers and implementers record facts.',
      when: 'Each time a fact is recorded.', sends: 'The new fact and the existing memory entries.',
      on: 'Refiles the fact, merges it with an existing one, or withholds it. The ledger keeps everything.' },
    'memory-slice': { summary: 'Leaves out memory lines that do not concern the task about to run.',
      when: 'Each time memory is added to a prompt.', sends: 'The task and the memory facts.',
      on: 'Removes lines from what the model is given. It never adds or reorders any.' },
    'memory-stale': { summary: 'Finds memory facts that are no longer true.',
      when: 'Only during a deep memory prune.', sends: 'The fact and the path of the file it is about.',
      on: 'Drops a fact that went false.' },
    'verify-discover': { summary: 'Picks a verification command for a repository that has none configured.',
      when: 'When neither --verify nor .forge/verify exists.', sends: "The repository's file list, the candidate commands, and memory's lines about verifying.",
      on: "Runs the chosen command. That command's own result decides verification." },
    coverage: { summary: 'Finds parts of your request that no task covers.',
      when: 'At plan, once.', sends: "Your request, its requirements, and each task's acceptance criteria.",
      on: 'Sends each uncovered requirement back to the planner, once.' },
    'retry-addressed': { summary: 'Checks that a retry actually touched what its review asked to fix.',
      when: 'On a retry after a review, before the next review.', sends: "The review's blocking findings and the retry's diff.",
      on: 'Sends the retry back to its implementer without paying for a review.' },
    'dwarf-message': { summary: "Reads an implementer's final message for unfinished work or a question for you.",
      when: "On each implementer's final message, before any review.", sends: "The task's title and the end of the message.",
      on: 'Skips the review of an attempt that says it did not finish, or stops the task to ask you.' },
    attribution: { summary: 'Guesses which merged task broke the combined verification, to shorten the search.',
      when: 'After a combined verification fails.', sends: "The end of the failing log, and each merged task's title, files and diff.",
      on: "Checks the likeliest task first. Nothing is merged, reverted or accepted." },
    findings: { summary: 'Finds review findings that repeat an open issue, and the ones concrete enough to file.',
      when: 'When you run forge issues.', sends: "The findings, and open issues' titles and bodies.",
      on: 'Turns a duplicate into a comment on that issue (posted by forge issues --apply), and pre-selects concrete findings.' },
  };
  // The setup guide, as data. A key never passes through this page: each command runs in the reader's own terminal.
  const SETUP_GUIDE = {
    intro: 'A decision model is a small hosted model that answers narrow questions about your run, in about a second. It is optional and off until you turn a gate on. It never changes a review verdict or makes work pass, and if it fails, the run goes on as if it were off.',
    steps: [
      { title: 'Add a provider', text: 'Run one of these in a terminal. A key is read from stdin or the environment and is stored outside your repositories.',
        commands: [
          ['TypeSafe (Jev)', 'printf \'%s\\n\' "$TYPESAFE_API_KEY" | forge decide provider add jev --name typesafe --key-stdin'],
          ['Cloudflare (Clef)', 'forge decide provider add clef --name cloudflare --account-id <id> --key-env CLOUDFLARE_API_TOKEN'],
          ['A local server, no key', 'forge decide provider add jev --name local --url http://127.0.0.1:8080 --local'],
        ] },
      { title: 'Check that it answers', text: 'This sends one fixed test request with no data from your repositories.',
        commands: [['', 'forge decide doctor --live']] },
      { title: 'Start one gate in shadow', text: 'In shadow a gate asks and records the answer, and the run goes on exactly as if it were off. It does send data: open a gate below to see what. Use the gate\'s mode and provider below, or run:',
        commands: [['', 'forge decide gate memory-curate shadow --provider typesafe']] },
      { title: 'Run forge as usual, then look', text: 'Each answer is kept with the run. These show the gates in effect and how they did.',
        commands: [['', 'forge decide status'], ['', 'forge decide report']] },
      { title: 'Turn a gate on when it has earned it', text: 'A gate goes on only after a replay of its recorded requests passes, which takes about 15 runs of shadow data. Without --send a replay only counts what it would send.',
        commands: [
          ['', 'forge decide replay --site jev-drift --provider typesafe --model jev-latest'],
          ['', 'forge decide replay --site jev-drift --provider typesafe --model jev-latest --send'],
          ['', 'forge decide gate drift on --provider typesafe'],
        ] },
    ],
    stop: [
      ['Keep one repository out', 'forge decide privacy never-send --repo DIR'],
      ['Send nothing while this is set', 'export FORGE_JEV=off'],
    ],
  };
  const POLL_MS = 20000;                                         // the config rarely changes, and each read starts a CLI process
  // The same order as forge_decide.profiles. A tier or key the body carries and this list does not still shows, after these.
  const TIERS = ['any', 'low', 'medium', 'high'];
  const POOLS = ['dwarf', 'qa'];
  const ROUTING_ORDER = ['planner', 'fractal_planner', 'final_reviewer', 'escalate_after', 'escalate_effort'];
  const POLICY_ORDER = ['qa_threshold', 'retry_failed', 'infra_retries', 'verify_retries', 'timeout', 'explore', 'shadow_rate', 'shadow_qa', 'fast'];
  const BUDGET_PAIRS = [['run', 'run_tokens', 'run_usd'], ['daily', 'daily_tokens', 'daily_usd']];
  const PROFILE_LEVELS = ['user', 'repo', 'builtin'];
  const WRITE_LEVELS = ['user', 'repo'];                        // POST /api/config/profiles refuses builtin
  const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
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

  // The replay a gate carries, or null: `forge decide replay`'s recorded result for the gate's provider and model. An older
  // forge sends none, which reads as no replay, never as an error.
  function describeReplay(replay) {
    if (!replay || typeof replay !== 'object' || Array.isArray(replay)) return null;
    return { provider: text(replay.provider), model: text(replay.model), passes: replay.passes === true, at: finite(replay.at) ? replay.at : null };
  }
  // The day a replay was recorded, in local time like the CLI's `replayed <date>`; '?' when the time is unusable.
  function replayDate(at) {
    const d = finite(at) ? new Date(at * 1000) : null;
    if (!d || Number.isNaN(d.getTime())) return '?';
    const two = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate());
  }
  // The tags a gate row shows beside its source tag: [{suffix, text, title, bad}], the element id being
  // set-gate-<name>-<suffix>. A forced gate acts on without a passing replay; a replay shows when it was recorded and
  // whether it passed. A gate with neither has none.
  function gateTags(gate) {
    const tags = [];
    const replay = gate.replay;
    const on = replay ? replay.provider + '/' + replay.model : '';
    if (gate.forced && gate.mode === 'on') {
      tags.push({ suffix: 'forced', text: 'forced', bad: true,
        title: 'This gate acts without a recorded replay for its provider and model that passes.' });
    }
    if (replay) {
      tags.push(replay.passes
        ? { suffix: 'replay', text: 'replayed ' + replayDate(replay.at), title: 'Replay on ' + on + ' passed.', bad: false }
        : { suffix: 'replay', text: 'replay failed', title: 'Replay on ' + on + ' did not pass (' + replayDate(replay.at) + ').', bad: true });
    }
    return tags;
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
      const mode = MODES.includes(g.mode) ? g.mode : 'off';
      const source = text(g.source) || 'default';
      return {
        name,
        mode,
        source,
        // The mode the user level holds, where the status shows it: the user file set it and no override applies.
        userMode: source === 'user' && !skip ? mode : null,
        provider: typeof g.provider === 'string' ? g.provider : '',
        model: typeof g.model === 'string' ? g.model : '',
        skip,
        ready: g.ready === true,
        forced: g.forced === true,
        replay: describeReplay(g.replay),
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
  const withRepo = runs => list(runs).filter(r => r && r.key != null && typeof RM.repoKey(r) === 'string');
  const newestFirst = (a, b) => (Number(b.updated) || 0) - (Number(a.updated) || 0);
  // The run whose repository the page opens on: the current run when it is in the list, else the most recent one.
  function pickDefault(runs, current) {
    const usable = withRepo(runs);
    if (current != null && usable.some(r => r.key === current)) return current;
    const latest = usable.slice().sort(newestFirst)[0];
    return latest ? latest.key : null;
  }
  // The selected run has left the list. 'keep': another run of the same repository takes over and the editors stay.
  // 'stay': nothing of this repository remains, but text is unsaved, so the page holds the gone run instead of
  // picking a new one and discarding that text. 'drop': nothing unsaved, so a new default may replace it.
  // 'here': the run is still listed, or none was selected.
  function departedRun(run, repo, runs, unsaved) {
    const usable = withRepo(runs);
    if (run == null || usable.some(r => r.key === run)) return 'here';
    if (usable.some(r => RM.repoKey(r) === repo)) return 'keep';
    return unsaved ? 'stay' : 'drop';
  }
  // The choices grouped by repository root: [{repo, name, runs}], the repository of the newest run first.
  function groupRuns(runs) {
    const groups = new Map();
    for (const r of withRepo(runs).sort(newestFirst)) {
      const repo = RM.repoKey(r);
      if (!groups.has(repo)) groups.set(repo, { repo, name: basename(repo), runs: [] });
      groups.get(repo).runs.push(r);
    }
    return [...groups.values()];
  }
  // The run inside its repository's group. The worktree it started in belongs to the run too, and the picker shows it
  // beside this label as secondary text; the group names the repository.
  const optionLabel = r => {
    const goal = text(r.goal).split('\n')[0].trim();
    const short = goal.length > 60 ? goal.slice(0, 59) + '…' : goal;
    return [r.run_id || r.key, short].filter(Boolean).join(' · ');
  };
  // The option the picker's type-ahead lands on: the first label that starts with `query`, ignoring case, searching on from
  // the active one `from` and wrapping round; -1 when none does. One letter typed again and again steps through the labels
  // that start with it, so the search starts after the active label; a longer query starts at it, which stays put.
  function typeAhead(labels, from, query) {
    const all = list(labels), q = text(query).toLowerCase();
    if (!q || !all.length) return -1;
    const repeat = [...q].every(ch => ch === q[0]);
    const want = repeat ? q[0] : q, start = repeat ? from + 1 : Math.max(from, 0);
    for (let i = 0; i < all.length; i++) {
      const at = (start + i) % all.length;
      if (text(all[at]).toLowerCase().startsWith(want)) return at;
    }
    return -1;
  }

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
  // What a repository switch would discard. A profile's unsaved JSON counts: resetRun drops those editors, so a
  // poll that sees the run leave the list must not move to another repository while one is dirty. Profile keys
  // are `name\0level`. File names stay in the order the page edits them; profile names are sorted.
  function unsavedLabels(editors, profileEditors) {
    const labels = [];
    const files = object(editors);
    for (const name of FILES) {
      if (isDirty(files[name])) labels.push('.forge/' + name);
    }
    const profiles = object(profileEditors);
    for (const key of Object.keys(profiles).sort()) {
      if (!isDirty(profiles[key])) continue;
      const name = text(String(key).split('\0')[0]);
      if (name) labels.push('profile ' + name);
    }
    return labels;
  }
  // What a failed GET /api/config does: with a good read of this run on the page, it stays and a note names the
  // failure; with none yet, the message replaces the page. Status 0 is no answer at all.
  function readFailure(res, hasData) {
    const message = (res && res.status !== 0 && (res.detail || res.error)) || 'could not reach forge-ui';
    return { keep: !!hasData, message };
  }

  // A scalar, or a list of them, as the summary shows it. An object is not a routing or policy value the table can name.
  function showValue(value) {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (Array.isArray(value)) return value.map(showValue).filter(Boolean).join(', ');
    return '';
  }
  const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  // Known keys first, in `order`, then any other own key, sorted. `skip` names keys a table already showed.
  function orderedEntries(obj, order, skip) {
    const src = object(obj), seen = new Set(skip || []), out = [];
    for (const key of order) {
      if (has(src, key)) { out.push([key, src[key]]); seen.add(key); }
    }
    for (const key of Object.keys(src).sort()) {
      if (!seen.has(key)) out.push([key, src[key]]);
    }
    return out;
  }
  function budgetPart(label, tokensKey, usdKey, budget) {
    const hasTokens = has(budget, tokensKey), hasUsd = has(budget, usdKey);
    if (!hasTokens && !hasUsd) return '';
    const bits = [];
    if (hasTokens) bits.push((finite(budget[tokensKey]) ? String(budget[tokensKey]) : 'none') + ' tokens');
    if (hasUsd) bits.push('$' + (finite(budget[usdKey]) ? Number(budget[usdKey]).toFixed(4) : 'none'));
    return label + ' ' + bits.join(', ');
  }
  // One gate as `name mode · provider · model`. A mode string is the short form the file stores.
  function gateText(name, value) {
    if (typeof value === 'string') return name + ' ' + value;
    const g = object(value);
    const bits = [text(g.mode), text(g.provider), text(g.model)].filter(Boolean);
    return bits.length ? name + ' ' + bits.join(' · ') : name;
  }

  // The selected profile's body, as data. render() draws only this: pools per tier, then the other routing and policy
  // values, then one line each for gates and budget. A missing block is an empty list or ''.
  function describeProfile(body) {
    const b = object(body), routing = object(b.routing), pools = {};
    for (const name of POOLS) {
      const value = routing[name];
      pools[name] = value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    }
    const tierNames = [];
    for (const tier of TIERS) {
      if (POOLS.some(name => pools[name] && has(pools[name], tier))) tierNames.push(tier);
    }
    const known = new Set(TIERS);
    const extra = new Set();
    for (const name of POOLS) {
      if (!pools[name]) continue;
      for (const tier of Object.keys(pools[name])) if (!known.has(tier)) extra.add(tier);
    }
    for (const tier of [...extra].sort()) tierNames.push(tier);
    const tiers = tierNames.map(tier => ({
      tier,
      dwarf: (pools.dwarf && showValue(pools.dwarf[tier])) || '—',
      qa: (pools.qa && showValue(pools.qa[tier])) || '—',
    })).filter(row => row.dwarf !== '—' || row.qa !== '—');
    const poolSkip = POOLS.filter(name => pools[name]);
    const routingLines = orderedEntries(routing, ROUTING_ORDER, poolSkip)
      .map(([key, value]) => ({ key, text: showValue(value) }))
      .filter(row => row.text);
    const policy = orderedEntries(b.policy, POLICY_ORDER)
      .map(([key, value]) => ({ key, text: showValue(value) }))
      .filter(row => row.text);
    const gates = b.gates && typeof b.gates === 'object' && !Array.isArray(b.gates)
      ? Object.keys(b.gates).sort().map(name => gateText(name, b.gates[name])).join('; ') : '';
    const budgetObj = b.budget && typeof b.budget === 'object' && !Array.isArray(b.budget) ? b.budget : {};
    const budgetBits = BUDGET_PAIRS.map(([label, tokens, usd]) => budgetPart(label, tokens, usd, budgetObj)).filter(Boolean);
    const budgetSeen = new Set(BUDGET_PAIRS.flatMap(([, tokens, usd]) => [tokens, usd]));
    for (const key of Object.keys(budgetObj).sort()) {
      if (!budgetSeen.has(key)) {
        const shown = showValue(budgetObj[key]);
        if (shown) budgetBits.push(key + ' ' + shown);
      }
    }
    return { tiers, routing: routingLines, policy, gates, budget: budgetBits.join('; ') };
  }

  // The POST /api/config/profiles body for one action. `use` with none clears the default and carries no name.
  // create carries `from` or `body`, never both; a missing body is the blank profile {}.
  function profileRequest(run, spec) {
    const s = object(spec), action = text(s.action);
    const body = { run: String(run == null ? '' : run), action };
    if (s.level != null) body.level = String(s.level);
    if (action === 'use' && s.none === true) { body.none = true; return body; }
    if (s.name != null) body.name = String(s.name);
    if (action === 'create' && s.from != null && text(s.from) !== '') body.from = String(s.from);
    else if (action === 'create' || action === 'edit') body.body = object(s.body);
    if (action === 'set' || action === 'unset') body.key = String(s.key == null ? '' : s.key);
    if (action === 'set') body.value = String(s.value == null ? '' : s.value);
    return body;
  }
  // The create spec for a blank profile, or a copy of `source` ({name, level, body}). A user-level write passes
  // no --repo, so --from resolves against the user file and the built-ins only. A repo profile is not there: the
  // CLI reports it missing, or copies a built-in of the same name. Send the body the list shows instead.
  // Every other copy is `from`, which is what a repo-level write resolves, and what a user-level copy of a user
  // or built-in profile resolves.
  function profileCopySpec(name, level, source) {
    const spec = { action: 'create', name: text(name), level: text(level) };
    const row = source && typeof source === 'object' ? source : null;
    const sourceName = row ? text(row.name) : '';
    if (!sourceName) { spec.body = {}; return spec; }
    if (spec.level === 'user' && text(row.level) === 'repo') spec.body = object(row.body);
    else spec.from = sourceName;
    return spec;
  }
  // `use --none` clears default_profile in the file that stores it. That file's level is payload.default.level
  // (the user file, else the repo file), not the profile's own level: a repo default_profile can name a user
  // profile, and clearing the user file would leave the repo default in effect while the CLI still says it cleared.
  // The profile's level is only the fallback when the payload names no user or repo level.
  function clearDefaultLevel(payload, profileLevel) {
    const stored = text(object(object(payload).default).level);
    if (stored === 'user' || stored === 'repo') return stored;
    return profileLevel === 'repo' ? 'repo' : 'user';
  }

  // What a row may do. A built-in (and anything that is not user or repo) can only be duplicated: the CLI refuses
  // to edit, remove or store a default at that level.
  function profileOffers(level) {
    const writable = level === 'user' || level === 'repo';
    return { edit: writable, duplicate: true, remove: writable, setDefault: writable };
  }
  // "shadows repo, builtin", or '' when the winning copy hides nothing.
  function shadowLabel(shadowed) {
    const levels = list(shadowed).map(text).filter(Boolean);
    return levels.length ? 'shadows ' + levels.join(', ') : '';
  }
  // The run record's Profile row: `NAME (level)`, and `· flags override: KEY, KEY` when `overridden` lists keys.
  // Null when there is no profile. Anything but a list in `overridden` names no key, so it adds nothing.
  function profileRowText(profile) {
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) return null;
    const name = text(profile.name), level = text(profile.level);
    if (!name || !level) return null;
    const keys = Array.isArray(profile.overridden) ? profile.overridden.map(text).filter(Boolean) : [];
    return name + ' (' + level + ')' + (keys.length ? ' · flags override: ' + keys.join(', ') : '');
  }
  // The editor's text. Pretty-printed so a refresh of an untouched profile does not look like an edit.
  function profileText(body) {
    try { return JSON.stringify(object(body), null, 2); }
    catch (_) { return '{}'; }
  }
  function profileNameProblem(name) {
    return PROFILE_NAME.test(text(name)) ? null : 'Use 1–64 letters, digits, ".", "_" or "-", starting with a letter or digit.';
  }
  // Why this text cannot be posted as a profile body, or null. The CLI still checks the keys.
  function profileJsonProblem(value) {
    const v = text(value);
    if (!v.trim()) return 'Empty: a profile body is a JSON object.';
    let parsed;
    try { parsed = JSON.parse(v); }
    catch (_) { return 'Not valid JSON.'; }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return 'A profile body is a JSON object.';
    return null;
  }
  // The CLI's reasons for a refused write. Exit 2's HTTP detail is only the last line; the whole list is result.error,
  // one reason per line. Empty when the write succeeded.
  function refusalReasons(res) {
    if (!res || res.ok) return [];
    const err = res.result && typeof res.result.error === 'string' ? res.result.error : '';
    const lines = err.split('\n').map(line => line.trim()).filter(Boolean);
    if (lines.length) return lines;
    const detail = typeof res.detail === 'string' ? res.detail.trim() : '';
    return detail ? [detail] : [];
  }

  /* ---------- DOM ---------- */
  function render(host, ctx) {
    const UI = root.ForgeUI, c = ctx || {};
    const el = (tag, t, cls) => UI.el(tag, t, cls);
    const s = { run: null, data: null, dataText: null, error: null, writes: false, loading: true, editors: {}, moved: {}, models: {}, modelMoved: {}, hold: 0, redraw: false, seq: 0, gen: 0, runsSig: null, ready: false, loadedAt: 0, repo: null, gone: false, asking: 0, asks: 0, pickerLate: false, listOpen: false,
      profiles: null, profilesText: null, profilesError: null, profileKey: null, profileEditors: {}, profileMoved: {}, profileReasons: [], profileReasonFor: null,
      forms: { create: { name: '', level: 'user', source: '' }, duplicate: null } };
    const key_ = (label, cls, onclick) => { const b = el('button', label, cls); b.type = 'button'; if (onclick) b.addEventListener('click', onclick); return b; };

    const head = el('header', undefined, 'set-head');
    head.append(el('h1', 'Settings', 'module-title'),
      el('p', 'What forge reads when it runs: its model registry, the decision-model gates, named profiles, and the three files a repository can keep in .forge.', 'muted set-lede'));
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

    /* the repository picker: a button and a listbox rather than a native select, whose options hold plain text only, so
       that each run can show the worktree it started in as secondary text. */
    // The repository the configuration was read from, as the server answered; until then the run's root.
    const repoPath = () => text(object(object(s.data).repo).path) || s.repo || '';
    // A run as the picker shows it: its label, then its worktree as secondary text, with the full path and branch in its title.
    function runFace(r) {
      const face = [el('span', optionLabel(r), 'set-run-label')];
      const worktree = RM.repoLabel(r).worktree;
      if (worktree) {
        const wt = el('span', worktree, 'muted repo-wt');
        const branch = typeof r.repo_branch === 'string' ? r.repo_branch.trim() : '';
        wt.title = r.repo_worktree + (branch ? `\nbranch ${branch}` : '');
        face.push(wt);
      }
      return face;
    }
    function drawPicker() {
      // The switch confirm lives in the picker, and the open list is part of it: while either is open the rebuild waits,
      // and runs when the confirm is answered or the list closes.
      if (s.asking || s.listOpen) { s.pickerLate = true; return; }
      const runs = c.runs ? c.runs() : [];
      const groups = groupRuns(runs);
      const before = s.run, usable = withRepo(runs);
      let kept = false;
      s.gone = false;
      // File editors and profile JSON both count. A poll that drops this run must not switch repositories
      // while either is dirty: resetRun would drop the text.
      const departure = departedRun(s.run, s.repo, runs, unsavedLabels(s.editors, s.profileEditors).length > 0);
      if (departure === 'keep') {
        const sibling = usable.slice().sort(newestFirst).find(r => RM.repoKey(r) === s.repo);
        if (sibling) { s.run = sibling.key; kept = true; }
      } else if (departure === 'stay') s.gone = true;
      else if (departure === 'drop') s.run = null;
      if (s.run == null) s.run = pickDefault(runs, c.key);
      const now = usable.find(r => r.key === s.run);
      if (now) s.repo = RM.repoKey(now);
      if (s.run !== before) {
        if (!kept) resetRun();
        if (s.ready) { if (!kept) drawBody(); load(); }           // the default arrived, or the run went away
      }
      const sig = JSON.stringify([s.run, s.gone, repoPath(), groups.map(g => [g.repo, g.runs.map(r => [r.key, optionLabel(r), r.repo_worktree || null, r.repo_branch || null])])]);
      if (sig === s.runsSig) return;
      s.runsSig = sig;
      if (!groups.length) { picker.replaceChildren(el('p', 'No run names a repository yet, so there is nothing to configure.', 'muted')); return; }
      const refocus = document.activeElement && document.activeElement.id === 'set-run';
      const current = s.gone ? null : usable.find(r => r.key === s.run);

      const btn = el('button', undefined, 'set-run'); btn.type = 'button'; btn.id = 'set-run';
      btn.dataset.run = current ? current.key : '';
      btn.setAttribute('aria-haspopup', 'listbox'); btn.setAttribute('aria-expanded', 'false');
      btn.setAttribute('aria-controls', 'set-run-list'); btn.setAttribute('aria-labelledby', 'set-run-cap set-run');
      if (current) btn.append(...runFace(current));
      else btn.append(el('span', 'This run is no longer listed', 'set-run-label'));
      const pop = el('div', undefined, 'set-run-pop popover'); pop.popover = 'auto'; pop.id = 'set-run-pop';
      const box = el('div', undefined, 'set-run-list'); box.id = 'set-run-list'; box.tabIndex = -1;
      box.setAttribute('role', 'listbox'); box.setAttribute('aria-labelledby', 'set-run-cap');
      const options = [];
      groups.forEach((g, i) => {
        const group = el('div', undefined, 'set-run-group'); group.setAttribute('role', 'group');
        const name = el('div', undefined, 'set-run-group-name'); name.id = 'set-run-g' + i;
        name.append(el('span', g.name, 'plate'), el('span', g.repo, 'muted mono'));
        group.setAttribute('aria-labelledby', name.id); group.append(name);
        for (const r of g.runs) {
          const o = el('div', undefined, 'set-run-opt'); o.id = 'set-run-o-' + r.key; o.dataset.run = r.key;
          o.setAttribute('role', 'option'); o.setAttribute('aria-selected', String(r.key === btn.dataset.run));
          o.append(...runFace(r)); group.append(o); options.push(o);
        }
        box.append(group);
      });
      pop.append(box);

      let active = -1, query = '', queryAt = 0;                    // the type-ahead's letters, and when the last came
      const typing = () => query !== '' && Date.now() - queryAt < 700;
      function activate(i) {
        active = i;
        options.forEach((o, j) => o.classList.toggle('is-active', j === i));
        if (i < 0) { box.removeAttribute('aria-activedescendant'); return; }
        box.setAttribute('aria-activedescendant', options[i].id);
        options[i].scrollIntoView({ block: 'nearest' });
      }
      const isOpen = () => pop.matches(':popover-open');
      const close = () => { s.listOpen = false; btn.setAttribute('aria-expanded', 'false'); if (isOpen()) pop.hidePopover(); };
      btn.popoverTargetElement = pop;                             // a click toggles the list; Escape or a click elsewhere closes it
      btn.addEventListener('keydown', e => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        if (!isOpen()) pop.showPopover();
      });
      // The open state as the list starts to open or close (beforetoggle runs at once); where it sits and the focus once it has
      // (toggle runs after).
      pop.addEventListener('beforetoggle', e => {
        s.listOpen = e.newState === 'open'; btn.setAttribute('aria-expanded', String(s.listOpen));
      });
      pop.addEventListener('toggle', e => {
        if (e.newState === 'open') {
          // under the button, or above it when the button sits low on the screen
          const r = btn.getBoundingClientRect();
          pop.style.minWidth = Math.round(r.width) + 'px';
          const h = pop.offsetHeight, below = window.innerHeight - r.bottom > h + 16;
          pop.style.left = Math.round(Math.max(8, Math.min(window.innerWidth - pop.offsetWidth - 8, r.left))) + 'px';
          pop.style.top = Math.round(below ? r.bottom + 4 : Math.max(8, r.top - h - 4)) + 'px';
          query = '';
          activate(Math.max(0, options.findIndex(o => o.dataset.run === btn.dataset.run)));
          box.focus({ preventScroll: true });
          return;
        }
        // Focus left with the list goes back to the button, the one on the page now (a choice may have rebuilt it).
        const at = document.activeElement;
        if (!at || at === document.body || pop.contains(at)) { const b = document.getElementById('set-run'); if (b) b.focus({ preventScroll: true }); }
        if (s.pickerLate && !s.asking) { s.pickerLate = false; drawPicker(); }
      });
      box.addEventListener('keydown', e => {
        const k = e.key, last = options.length - 1;
        if (k === 'ArrowDown') activate(Math.min(last, active + 1));
        else if (k === 'ArrowUp') activate(Math.max(0, active - 1));
        else if (k === 'Home') activate(0);
        else if (k === 'End') activate(last);
        else if (k === 'Enter' || (k === ' ' && !typing())) { if (active >= 0) choose(options[active].dataset.run); }
        else if (k === 'Tab') { close(); return; }
        else if (k.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey) {
          query = (typing() ? query : '') + k; queryAt = Date.now();
          const at = typeAhead(options.map(o => o.firstChild.textContent), active, query);
          if (at >= 0) activate(at);
        } else return;
        e.preventDefault();
      });
      box.addEventListener('mousemove', e => { const o = e.target.closest('[role=option]'); if (o && options.indexOf(o) !== active) activate(options.indexOf(o)); });
      box.addEventListener('click', e => { const o = e.target.closest('[role=option]'); if (o) choose(o.dataset.run); });
      async function choose(key) {
        close();
        if (key === btn.dataset.run) return;                      // the run already shown
        const unsaved = unsavedLabels(s.editors, s.profileEditors);
        if (unsaved.length) {
          const ask = s.asking = ++s.asks;
          const yes = await UI.confirmInline(picker, `Switch repository and discard the unsaved edits to ${unsaved.join(', ')}?`, { danger: true });
          if (s.asking !== ask) return;                           // a later choice asked again, and that confirm decides
          const late = s.pickerLate;
          s.asking = 0; s.pickerLate = false;
          if (!yes) { if (late) drawPicker(); return; }
        }
        pick(key);
      }

      const lab = el('label', undefined, 'set-field'); lab.htmlFor = 'set-run';
      const cap = el('span', 'Repository of run', 'plate'); cap.id = 'set-run-cap';
      lab.append(cap);
      const path = el('p', undefined, 'set-repo-path');
      path.append(el('i', undefined, 'lamp lamp-route'), el('span', repoPath(), 'mono'));
      const row = el('div', undefined, 'set-picker-row'); row.append(lab, btn, pop);
      picker.replaceChildren(row, path);
      if (s.gone) picker.append(el('p', 'The run is gone from the list. Your unsaved edits are kept; pick another run to continue, which discards them.', 'warn set-problem'));
      if (refocus) btn.focus({ preventScroll: true });
    }
    // A new run: nothing of the old one stays, and confirms of the old body (now detached) no longer hold a redraw.
    function resetRun() {
      staleNote(null);
      s.data = null; s.dataText = null; s.error = null; s.editors = {}; s.moved = {}; s.models = {}; s.modelMoved = {}; s.loading = s.run != null;
      s.profiles = null; s.profilesText = null; s.profilesError = null; s.profileKey = null;
      s.profileEditors = {}; s.profileMoved = {}; s.profileReasons = []; s.profileReasonFor = null;
      s.forms = { create: { name: '', level: 'user', source: '' }, duplicate: null };
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
    const profileId = row => text(row && row.name) + '\0' + text(row && row.level);
    // Fold a profiles read into the page. A failed read keeps the previous list when there is one. An editor's unsaved
    // JSON stays; `moved` says the stored body changed under it.
    function applyProfiles(res) {
      if (!res || !res.ok) {
        const failed = readFailure(res, s.profiles);
        s.profilesError = failed.message;
        if (!failed.keep) { s.profiles = null; }
        return;
      }
      s.profilesError = null;
      const data = object(res.data), rows = list(data.profiles);
      for (const row of rows) {
        const key = profileId(row);
        if (!s.profileEditors[key]) continue;
        const next = dirtyState(s.profileEditors[key], profileText(row.body));
        s.profileEditors[key] = next.editor;
        s.profileMoved[key] = next.moved || (next.dirty && !!s.profileMoved[key]);
      }
      if (!rows.some(row => profileId(row) === s.profileKey)) {
        const def = rows.find(row => row.default) || rows[0];
        s.profileKey = def ? profileId(def) : null;
      }
      s.profiles = data;
    }
    async function load() {
      const run = s.run, seq = ++s.seq;
      if (run == null) { s.loading = false; drawBody(); return; }
      if (s.gone) return;                                         // the run is not listed: nothing to read until another is picked
      s.loadedAt = Date.now();
      const query = encodeURIComponent(run);
      const [res, pres] = await Promise.all([
        UI.get('/api/config?run=' + query),
        UI.get('/api/config/profiles?run=' + query),
      ]);
      if (seq !== s.seq || run !== s.run) return;                 // a later pick or load answered
      s.loading = false;
      if (!res.ok) {
        const failed = readFailure(res, s.data);
        if (failed.keep) {                                       // the last good page, its editors and their unsaved text stay
          staleNote(failed.message);
          applyProfiles(pres);
          const pt = JSON.stringify([s.profiles, s.profilesError]);
          if (pt !== s.profilesText) {
            s.profilesText = pt;
            if (s.hold > 0) s.redraw = true; else drawBody();
          }
          return;
        }
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
      applyProfiles(pres);
      const t = JSON.stringify(res.data);
      const pt = JSON.stringify([s.profiles, s.profilesError]);
      if (t === s.dataText && pt === s.profilesText && !s.redraw) return;
      s.data = res.data; s.dataText = t; s.profilesText = pt;
      drawPicker();                                               // its path line names the repository this read came from
      if (s.hold > 0) { s.redraw = true; return; }                // a confirm or a write is open: draw after it
      drawBody();
    }
    // Holds the redraw while a confirm or a write is open; the returned release is a no-op once the run changed.
    function grab() {
      const gen = s.gen; s.hold++;
      return () => { if (gen !== s.gen) return; s.hold--; if (!s.hold && s.redraw) { s.redraw = false; drawBody(); } };
    }

    function drawBody() { UI.guard(body, drawBodyNow); }
    function drawBodyNow() {
      const focus = document.activeElement && body.contains(document.activeElement) ? document.activeElement : null;
      const at = focus && focus.dataset.editor ? { kind: 'editor', name: focus.dataset.editor, a: focus.selectionStart, b: focus.selectionEnd }
        : focus && focus.dataset.model ? { kind: 'model', name: focus.dataset.model, a: focus.selectionStart, b: focus.selectionEnd }
        : focus && focus.dataset.profileEditor ? { kind: 'profile', a: focus.selectionStart, b: focus.selectionEnd }
        : focus && focus.id ? { kind: 'id', id: focus.id, a: focus.selectionStart, b: focus.selectionEnd } : null;
      if (s.loading) { body.replaceChildren(el('p', 'Reading the configuration…', 'muted')); return; }
      if (s.error) { body.replaceChildren(el('p', 'Could not read the configuration: ' + s.error, 'warn')); return; }
      if (!s.data) { body.replaceChildren(); return; }
      body.replaceChildren(registrySection(), decideSection(), profilesSection(), repoSection(), datasetSection(), envSection());
      if (!at) return;
      const t = at.kind === 'model' ? body.querySelector(`input[data-model="${at.name}"]`)
        : at.kind === 'editor' ? body.querySelector(`textarea[data-editor="${at.name}"]`)
        : at.kind === 'profile' ? body.querySelector('textarea[data-profile-editor]')
        : document.getElementById(at.id);
      if (t && body.contains(t)) { t.focus({ preventScroll: true }); try { t.setSelectionRange(at.a, at.b); } catch (_) { /* not selectable */ } }
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
      const sec = section('decide', 'Decision models', 'Each gate is off, shadow or on, with a provider and a model. Off asks nothing. Shadow asks and records, and changes nothing. On lets the gate act. A change writes your forge config (the user level), so it applies to every repository. The mode shown is the one in effect for this repository, and where it came from.');
      const guide = el('button', 'How to set this up', 'set-guide-open');
      guide.type = 'button'; guide.id = 'set-decide-guide';
      guide.addEventListener('click', openGuide);
      sec.append(guide);
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

    // The guide lives on document.body, so a refresh of the page's body never closes it.
    function openGuide() {
      let dlg = document.getElementById('set-guide');
      if (!dlg) {
        dlg = el('dialog', undefined, 'set-guide');
        dlg.id = 'set-guide'; dlg.setAttribute('aria-labelledby', 'set-guide-h');
        const title = el('h2', 'Set up a decision model'); title.id = 'set-guide-h';
        const close = el('button', 'Close', 'set-guide-close'); close.type = 'button';
        close.addEventListener('click', () => dlg.close());
        const head = el('div', undefined, 'set-guide-head');
        head.append(title, close);
        dlg.append(head, el('p', SETUP_GUIDE.intro));
        const steps = el('ol', undefined, 'set-guide-steps');
        for (const step of SETUP_GUIDE.steps) {
          const li = el('li');
          li.append(el('strong', step.title), el('p', step.text, 'muted'));
          for (const [label, command] of step.commands) li.append(commandLine(label, command));
          steps.append(li);
        }
        dlg.append(steps, el('h3', 'To stop sending'));
        for (const [label, command] of SETUP_GUIDE.stop) dlg.append(commandLine(label, command));
        dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });   // a click on the backdrop
        document.body.append(dlg);
      }
      if (!dlg.open) dlg.showModal();
    }

    function commandLine(label, command) {
      const line = el('div', undefined, 'set-guide-command');
      if (label) line.append(el('span', label, 'muted'));
      const row = el('div', undefined, 'set-guide-row');
      const copy = el('button', 'Copy', 'set-guide-copy'); copy.type = 'button';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(command); copy.textContent = 'Copied'; }
        catch { copy.textContent = 'Copy failed'; }
      });
      const code = el('code', command, 'mono');
      code.tabIndex = 0;                                       // one line, scrolled: a wrap inside `--local` misleads anyone retyping it
      row.append(code, copy);
      line.append(row);
      return line;
    }

    // What a gate does. Its open state outlives a refresh, which rebuilds the row.
    function gateHelp(gate) {
      const help = GATE_HELP[gate.name];
      if (!help) return [];
      const more = el('details', undefined, 'set-gate-more');
      more.id = 'set-gate-' + gate.name + '-help';
      more.open = !!s.gateHelpOpen?.[gate.name];
      more.addEventListener('toggle', () => { (s.gateHelpOpen ||= {})[gate.name] = more.open; });
      more.append(el('summary', 'What it sends and what on does'));
      const dl = el('dl');
      for (const [term, value] of [['When', help.when], ['Sends', help.sends], ['When on', help.on]]) dl.append(el('dt', term), el('dd', value));
      more.append(dl);
      return [el('p', help.summary, 'set-gate-summary'), more];
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
      for (const t of gateTags(gate)) {
        const tag = el('span', t.text, t.bad ? 'tag tag-bad' : 'tag');
        tag.id = 'set-gate-' + gate.name + '-' + t.suffix;
        tag.title = t.title;
        status.append(tag);
      }
      if (gate.skip) status.append(el('span', 'skip: ' + gate.skip, 'set-gate-skip'));
      const top = el('div', undefined, 'set-file-head');
      top.append(h, status);
      art.append(top, ...gateHelp(gate));
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
      // The select shows the user level's mode when the status has it. Otherwise the mode in effect is a label that no
      // choice matches, so every mode is a choice that writes the user level, the one in effect included.
      const rest = gate.userMode || '';                             // the value the select goes back to
      if (!gate.userMode) {
        const effect = el('option', 'in effect: ' + gate.mode);
        effect.value = ''; effect.disabled = true; effect.selected = true;
        sel.append(effect);
      }
      for (const mode of MODES) {
        const o = el('option', mode);
        o.value = mode;
        o.selected = mode === gate.userMode;
        sel.append(o);
      }
      sel.addEventListener('change', async () => {
        const next = sel.value;
        if (!MODES.includes(next) || next === gate.userMode) { sel.value = rest; return; }
        const gen = s.gen, release = grab();
        sel.disabled = true;
        const yes = await UI.confirmInline(result, `Set ${gate.name} to ${next}? This writes your forge config (the user level) for every repository.`);
        if (!yes || gen !== s.gen) {
          sel.value = rest; sel.disabled = false; release();
          if (!yes && gen === s.gen) sel.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config', configBody(s.run, gateKey(gate.name), next));
        UI.outcome(result, res, `${gate.name} is set to ${next}.`);
        if (!res.ok) sel.value = rest;
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

    function profilesSection() {
      const sec = section('profiles', 'Profiles', 'A named bundle of routing, policy, gates and budget. A user profile hides a repo profile of the same name, which hides a built-in. Editing writes decide.json at the profile\'s level.');
      if (s.profilesError && !s.profiles) {
        sec.append(el('p', 'Could not read profiles: ' + s.profilesError, 'warn'));
        return sec;
      }
      if (!s.profiles) { sec.append(el('p', 'Reading profiles…', 'muted')); return sec; }
      if (s.profilesError) sec.append(el('p', 'The last read of profiles failed: ' + s.profilesError + '. This shows the read before it.', 'warn set-problem'));
      const rows = list(s.profiles.profiles);
      if (!rows.length) sec.append(el('p', 'No profiles.', 'muted'));
      else sec.append(profileList(rows));
      const row = rows.find(item => profileId(item) === s.profileKey);
      if (row) sec.append(profileDetail(row));
      if (s.writes) sec.append(newProfileForm(rows));
      return sec;
    }

    function profileList(rows) {
      const ul = el('ul', undefined, 'set-profiles');
      ul.setAttribute('aria-label', 'Profiles');
      for (const row of rows) {
        const li = el('li');
        const on = profileId(row) === s.profileKey;
        const b = el('button', undefined, 'set-profile');
        b.type = 'button';
        b.id = 'set-prof-' + text(row.name);
        b.dataset.profile = text(row.name);
        b.dataset.level = text(row.level);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        const shadow = shadowLabel(row.shadowed);
        b.setAttribute('aria-label', [text(row.name), text(row.level), row.default ? 'default' : '', shadow].filter(Boolean).join(', '));
        b.append(el('span', text(row.name), 'set-profile-name'), el('span', text(row.level), 'tag'));
        if (row.default) b.append(el('span', 'default', 'tag'));
        if (shadow) b.append(el('span', shadow, 'set-profile-meta'));
        b.addEventListener('click', () => {
          const key = profileId(row);
          if (s.profileKey === key) return;
          s.profileKey = key;
          s.forms.duplicate = null;
          s.profileReasons = [];
          s.profileReasonFor = null;
          drawBody();
          const again = document.getElementById(b.id);
          if (again) again.focus({ preventScroll: true });
        });
        li.append(b);
        ul.append(li);
      }
      return ul;
    }

    function summaryBlock(title, id) {
      const block = el('div');
      block.id = id;
      block.append(el('p', title, 'plate'));
      return block;
    }
    function profileSummary(body) {
      const view = describeProfile(body);
      const box = el('div', undefined, 'set-profile-summary');
      if (view.tiers.length) {
        const wrap = el('div', undefined, 'set-table-wrap');
        const table = el('table', undefined, 'set-table');
        const cap = el('caption', 'Dwarf and QA pools by tier', 'visually-hidden');
        const hr = el('tr');
        for (const h of ['Tier', 'Dwarf', 'QA']) { const th = el('th', h); th.scope = 'col'; hr.append(th); }
        const thead = el('thead'); thead.append(hr);
        const tbody = el('tbody');
        for (const tier of view.tiers) {
          const tr = el('tr');
          const th = el('th', tier.tier, 'set-alias'); th.scope = 'row';
          tr.append(th, el('td', tier.dwarf, 'mono'), el('td', tier.qa, 'mono'));
          tbody.append(tr);
        }
        table.append(cap, thead, tbody);
        wrap.append(table);
        box.append(wrap);
      }
      if (view.routing.length) {
        const block = summaryBlock('Routing', 'set-profile-routing');
        const dl = el('dl', undefined, 'set-kv');
        for (const item of view.routing) dl.append(el('dt', item.key), el('dd', item.text, 'mono'));
        block.append(dl);
        box.append(block);
      }
      if (view.policy.length) {
        const block = summaryBlock('Policy', 'set-profile-policy');
        const dl = el('dl', undefined, 'set-kv');
        for (const item of view.policy) dl.append(el('dt', item.key), el('dd', item.text, 'mono'));
        block.append(dl);
        box.append(block);
      }
      if (view.gates) {
        const block = summaryBlock('Gates', 'set-profile-gates');
        block.append(el('p', view.gates, 'mono set-summary-line'));
        box.append(block);
      }
      if (view.budget) {
        const block = summaryBlock('Budget', 'set-profile-budget');
        block.append(el('p', view.budget, 'mono set-summary-line'));
        box.append(block);
      }
      const source = text(object(body).source);
      if (source) {
        const block = summaryBlock('Source', 'set-profile-source');
        block.append(el('p', source, 'muted set-summary-line'));
        box.append(block);
      }
      if (!box.childElementCount) box.append(el('p', 'This profile sets nothing.', 'muted'));
      return box;
    }

    function showReasons(row) {
      if (s.profileReasonFor !== profileId(row) || !s.profileReasons.length) return null;
      const ul = el('ul', undefined, 'set-reasons');
      ul.id = 'set-profile-reasons';
      for (const reason of s.profileReasons) ul.append(el('li', reason));
      return ul;
    }

    function profileEditor(row, result) {
      const key = profileId(row);
      const fresh = profileText(row.body);
      const ed = s.profileEditors[key] || (s.profileEditors[key] = editorOf(fresh));
      const id = 'set-profile-body';
      const lab = el('label', 'Body of ' + text(row.name) + ' as JSON', 'visually-hidden');
      lab.htmlFor = id;
      const ta = el('textarea', undefined, 'set-editor mono');
      ta.id = id;
      ta.dataset.profileEditor = '1';
      ta.rows = Math.max(6, Math.min(18, shown(ed).split('\n').length + 1));
      ta.spellcheck = false;
      ta.setAttribute('autocapitalize', 'off');
      ta.setAttribute('autocomplete', 'off');
      ta.value = shown(ed);
      let from = ed.base;
      const movedNote = el('p', 'The profile changed after you began editing; saving replaces it with your text.', 'warn set-file-note');
      movedNote.hidden = !(isDirty(ed) && s.profileMoved[key]);
      const problem = el('p', undefined, 'warn set-problem');
      problem.hidden = true;
      problem.id = id + '-problem';
      const save = key_('Save', 'primary');
      save.id = 'set-profile-save';
      save.setAttribute('aria-label', 'Save profile ' + text(row.name));
      const sync = () => {
        const cur = s.profileEditors[key], dirtyNow = isDirty(cur);
        const bad = dirtyNow ? profileJsonProblem(cur.draft) : null;
        const reasons = s.profileReasonFor === key ? s.profileReasons : [];
        problem.hidden = !bad;
        problem.textContent = bad || '';
        const described = [bad ? problem.id : '', reasons.length ? 'set-profile-reasons' : ''].filter(Boolean).join(' ');
        if (bad || reasons.length) {
          ta.setAttribute('aria-invalid', 'true');
          if (described) ta.setAttribute('aria-describedby', described);
        } else {
          ta.removeAttribute('aria-invalid');
          ta.removeAttribute('aria-describedby');
        }
        save.disabled = !dirtyNow || !!bad;
        movedNote.hidden = !(dirtyNow && s.profileMoved[key]);
      };
      ta.addEventListener('input', () => {
        const next = typed(s.profileEditors[key], ta.value, from);
        s.profileEditors[key] = next.editor;
        s.profileMoved[key] = next.moved || (next.dirty && !!s.profileMoved[key]);
        if (s.profileReasonFor === key) {
          s.profileReasons = []; s.profileReasonFor = null;
          const old = document.getElementById('set-profile-reasons');
          if (old) old.remove();
        }
        sync();
      });
      save.addEventListener('click', async () => {
        const cur = s.profileEditors[key];
        const bad = profileJsonProblem(shown(cur));
        if (!isDirty(cur) || bad) { sync(); return; }
        let parsed;
        try { parsed = JSON.parse(cur.draft); } catch (_) { sync(); return; }
        const gen = s.gen, release = grab();
        save.disabled = true;
        ta.readOnly = true;
        const yes = await UI.confirmInline(result, `Replace the body of profile ${text(row.name)} at the ${text(row.level)} level?`);
        if (!yes || gen !== s.gen) {
          ta.readOnly = false;
          release();
          sync();
          if (!yes && gen === s.gen) save.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config/profiles', profileRequest(s.run, { action: 'edit', name: row.name, level: row.level, body: parsed }));
        const reasons = refusalReasons(res);
        s.profileReasons = res.ok ? [] : reasons;
        s.profileReasonFor = res.ok ? null : key;
        UI.outcome(result, res, `Saved ${text(row.name)}.`);
        if (res.ok && gen === s.gen) {
          s.profileEditors[key] = editorOf(profileText(parsed));
          s.profileMoved[key] = false;
          from = s.profileEditors[key].base;
        }
        ta.readOnly = false;
        release();
        // A refusal keeps this textarea. The reasons go under it; a redraw would drop the outcome line.
        if (!res.ok && gen === s.gen && ta.isConnected) {
          const old = document.getElementById('set-profile-reasons');
          if (old) old.remove();
          const list = showReasons(row);
          const editor = ta.closest('.set-profile-editor');
          if (list && editor) editor.after(list);
        }
        if (gen === s.gen && ta.isConnected) sync();
        if (res.ok && gen === s.gen) await reload();
      });
      const wrap = el('div', undefined, 'set-profile-editor');
      wrap.append(lab, ta, problem, movedNote, save);
      sync();
      return wrap;
    }

    function duplicateForm(row, result) {
      const draft = s.forms.duplicate;
      const form = el('div', undefined, 'set-profile-form');
      form.id = 'set-dup-form';
      const name = el('input');
      name.type = 'text';
      name.id = 'set-dup-name';
      name.value = draft.name;
      name.spellcheck = false;
      name.setAttribute('autocomplete', 'off');
      name.setAttribute('autocapitalize', 'off');
      const nameLab = el('label', undefined, 'filter');
      nameLab.htmlFor = name.id;
      nameLab.append(el('span', 'New name'), name);
      const sel = el('select');
      sel.id = 'set-dup-level';
      sel.setAttribute('aria-label', 'Level for the copy of ' + text(row.name));
      for (const level of WRITE_LEVELS) {
        const o = el('option', level);
        o.value = level;
        o.selected = level === draft.level;
        sel.append(o);
      }
      const levelLab = el('label', undefined, 'filter');
      levelLab.htmlFor = sel.id;
      levelLab.append(el('span', 'Level'), sel);
      const problem = el('p', undefined, 'warn set-problem');
      problem.hidden = true;
      problem.id = 'set-dup-problem';
      const go = key_('Create copy', 'primary');
      go.id = 'set-dup-save';
      go.setAttribute('aria-label', 'Create a copy of ' + text(row.name));
      const cancel = key_('Cancel');
      cancel.id = 'set-dup-cancel';
      cancel.setAttribute('aria-label', 'Cancel duplicating ' + text(row.name));
      name.addEventListener('input', () => { draft.name = name.value; problem.hidden = true; name.removeAttribute('aria-invalid'); });
      sel.addEventListener('change', () => { draft.level = sel.value; });
      cancel.addEventListener('click', () => {
        s.forms.duplicate = null;
        drawBody();
        const again = document.getElementById('set-profile-duplicate');
        if (again) again.focus({ preventScroll: true });
      });
      go.addEventListener('click', async () => {
        const bad = profileNameProblem(draft.name);
        if (bad) {
          problem.hidden = false;
          problem.textContent = bad;
          name.setAttribute('aria-invalid', 'true');
          name.setAttribute('aria-describedby', problem.id);
          name.focus({ preventScroll: true });
          return;
        }
        const gen = s.gen, release = grab();
        go.disabled = true;
        name.readOnly = true;
        const yes = await UI.confirmInline(result, `Create profile ${draft.name} at the ${draft.level} level as a copy of ${text(row.name)}?`);
        if (!yes || gen !== s.gen) {
          go.disabled = false;
          name.readOnly = false;
          release();
          if (!yes && gen === s.gen) go.focus({ preventScroll: true });
          return;
        }
        const created = draft.name, level = draft.level;
        const res = await UI.post('/api/config/profiles', profileRequest(s.run, profileCopySpec(created, level, row)));
        UI.outcome(result, res, `Created ${created}.`);
        name.readOnly = false;
        if (res.ok && gen === s.gen) {
          s.profileKey = created + '\0' + level;
          s.forms.duplicate = null;
        } else go.disabled = false;
        release();
        if (res.ok && gen === s.gen) await reload();
      });
      const keys = el('div', undefined, 'set-keys');
      keys.append(go, cancel);
      form.append(nameLab, levelLab, problem, keys);
      return form;
    }

    function defaultButton(row, result) {
      const clearing = row.default === true;
      // Setting writes default_profile at the profile's own level. Clearing removes it from the file that
      // actually stores it, which can be the other level (a repo default_profile naming a user profile).
      const level = clearing ? clearDefaultLevel(s.profiles, row.level) : text(row.level);
      const label = clearing ? 'Clear default' : 'Set as default';
      const b = key_(label);
      b.id = 'set-profile-default';
      b.setAttribute('aria-label', clearing
        ? 'Clear the default profile at the ' + level + ' level'
        : 'Set ' + text(row.name) + ' as the default profile at the ' + level + ' level');
      b.addEventListener('click', async () => {
        const gen = s.gen, release = grab();
        b.disabled = true;
        const question = clearing
          ? `Clear the default profile at the ${level} level?`
          : `Set ${text(row.name)} as the default profile at the ${level} level?`;
        const yes = await UI.confirmInline(result, question);
        if (!yes || gen !== s.gen) {
          b.disabled = false;
          release();
          if (!yes && gen === s.gen) b.focus({ preventScroll: true });
          return;
        }
        const spec = clearing
          ? { action: 'use', level, none: true }
          : { action: 'use', name: row.name, level };
        const res = await UI.post('/api/config/profiles', profileRequest(s.run, spec));
        UI.outcome(result, res, clearing ? `Cleared the default at the ${level} level.` : `${text(row.name)} is the default at the ${level} level.`);
        b.disabled = false;
        release();
        if (res.ok && gen === s.gen) await reload();
      });
      return b;
    }

    function deleteButton(row, result) {
      const b = key_('Delete');
      b.classList.add('set-remove');
      b.id = 'set-profile-delete';
      b.setAttribute('aria-label', 'Delete profile ' + text(row.name));
      b.addEventListener('click', async () => {
        const gen = s.gen, release = grab();
        b.disabled = true;
        const yes = await UI.confirmInline(result, `Delete profile ${text(row.name)} at the ${text(row.level)} level?`, { danger: true });
        if (!yes || gen !== s.gen) {
          b.disabled = false;
          release();
          if (!yes && gen === s.gen) b.focus({ preventScroll: true });
          return;
        }
        const res = await UI.post('/api/config/profiles', profileRequest(s.run, { action: 'rm', name: row.name, level: row.level }));
        UI.outcome(result, res, `Deleted ${text(row.name)}.`);
        if (res.ok && gen === s.gen) {
          const key = profileId(row);
          delete s.profileEditors[key];
          delete s.profileMoved[key];
          s.profileKey = null;
          s.profileReasons = [];
          s.profileReasonFor = null;
        } else b.disabled = false;
        release();
        if (res.ok && gen === s.gen) await reload();
      });
      return b;
    }

    function profileDetail(row) {
      const offers = profileOffers(row.level);
      const art = el('article', undefined, 'set-file set-profile-detail');
      art.id = 'set-profile-detail';
      const h = el('h3', text(row.name), 'set-file-name');
      h.id = 'set-profile-detail-h';
      art.setAttribute('aria-labelledby', h.id);
      const status = el('p', undefined, 'set-file-status');
      status.append(el('span', text(row.level), 'tag'));
      if (row.default) status.append(el('span', 'default', 'tag'));
      const shadow = shadowLabel(row.shadowed);
      if (shadow) status.append(el('span', shadow, 'set-profile-meta'));
      const top = el('div', undefined, 'set-file-head');
      top.append(h, status);
      art.append(top, profileSummary(row.body));
      if (!offers.edit) art.append(el('p', 'This built-in profile is read-only. Duplicate it to change a copy.', 'muted set-file-blurb'));
      if (!s.writes) return art;
      const result = el('div', undefined, 'set-result');
      result.id = 'set-profile-result';
      if (offers.edit) art.append(profileEditor(row, result));
      const reasons = showReasons(row);
      if (reasons) art.append(reasons);
      const keys = el('div', undefined, 'set-keys');
      const dup = key_('Duplicate');
      dup.id = 'set-profile-duplicate';
      dup.setAttribute('aria-label', 'Duplicate profile ' + text(row.name));
      dup.addEventListener('click', () => {
        if (s.forms.duplicate && s.forms.duplicate.of === profileId(row)) return;
        s.forms.duplicate = { of: profileId(row), name: '', level: row.level === 'repo' ? 'repo' : 'user' };
        drawBody();
        const field = document.getElementById('set-dup-name');
        if (field) field.focus({ preventScroll: true });
      });
      keys.append(dup);
      if (offers.setDefault) keys.append(defaultButton(row, result));
      if (offers.remove) keys.append(deleteButton(row, result));
      art.append(keys);
      if (s.forms.duplicate && s.forms.duplicate.of === profileId(row)) art.append(duplicateForm(row, result));
      art.append(result);
      return art;
    }

    function newProfileForm(rows) {
      const draft = s.forms.create;
      const box = el('div', undefined, 'set-file');
      box.id = 'set-new';
      const h = el('h3', 'New profile', 'set-file-name');
      h.id = 'set-new-h';
      box.setAttribute('aria-labelledby', h.id);
      box.append(h, el('p', 'A blank body, or a copy of a profile that already exists.', 'muted set-file-blurb'));
      const name = el('input');
      name.type = 'text';
      name.id = 'set-new-name';
      name.value = draft.name;
      name.spellcheck = false;
      name.setAttribute('autocomplete', 'off');
      name.setAttribute('autocapitalize', 'off');
      const nameLab = el('label', undefined, 'filter');
      nameLab.htmlFor = name.id;
      nameLab.append(el('span', 'Name'), name);
      const level = el('select');
      level.id = 'set-new-level';
      level.setAttribute('aria-label', 'Level for the new profile');
      for (const item of WRITE_LEVELS) {
        const o = el('option', item);
        o.value = item;
        o.selected = item === draft.level;
        level.append(o);
      }
      const levelLab = el('label', undefined, 'filter');
      levelLab.htmlFor = level.id;
      levelLab.append(el('span', 'Level'), level);
      const source = el('select');
      source.id = 'set-new-source';
      source.setAttribute('aria-label', 'Body for the new profile');
      const blank = el('option', 'Blank body');
      blank.value = '';
      blank.selected = !draft.source;
      source.append(blank);
      for (const row of rows) {
        const o = el('option', text(row.name) + ' (' + text(row.level) + ')');
        o.value = text(row.name);
        o.selected = text(row.name) === draft.source;
        source.append(o);
      }
      const sourceLab = el('label', undefined, 'filter');
      sourceLab.htmlFor = source.id;
      sourceLab.append(el('span', 'Copy from'), source);
      const problem = el('p', undefined, 'warn set-problem');
      problem.hidden = true;
      problem.id = 'set-new-problem';
      const result = el('div', undefined, 'set-result');
      const go = key_('Create', 'primary');
      go.id = 'set-new-save';
      go.setAttribute('aria-label', 'Create profile');
      name.addEventListener('input', () => { draft.name = name.value; problem.hidden = true; name.removeAttribute('aria-invalid'); });
      level.addEventListener('change', () => { draft.level = level.value; });
      source.addEventListener('change', () => { draft.source = source.value; });
      go.addEventListener('click', async () => {
        const bad = profileNameProblem(draft.name);
        if (bad) {
          problem.hidden = false;
          problem.textContent = bad;
          name.setAttribute('aria-invalid', 'true');
          name.setAttribute('aria-describedby', problem.id);
          name.focus({ preventScroll: true });
          return;
        }
        const sourceRow = draft.source ? rows.find(item => text(item.name) === draft.source) : null;
        // A chosen name whose row is gone still goes as `from`. A repo profile copied to the user level
        // cannot: that write passes no --repo, so the spec carries the body instead.
        const spec = profileCopySpec(draft.name, draft.level, draft.source ? (sourceRow || { name: draft.source }) : null);
        const gen = s.gen, release = grab();
        go.disabled = true;
        name.readOnly = true;
        const from = draft.source ? `a copy of ${draft.source}` : 'a blank body';
        const yes = await UI.confirmInline(result, `Create profile ${draft.name} at the ${draft.level} level with ${from}?`);
        if (!yes || gen !== s.gen) {
          go.disabled = false;
          name.readOnly = false;
          release();
          if (!yes && gen === s.gen) go.focus({ preventScroll: true });
          return;
        }
        const created = draft.name, at = draft.level;
        const res = await UI.post('/api/config/profiles', profileRequest(s.run, spec));
        UI.outcome(result, res, `Created ${created}.`);
        name.readOnly = false;
        if (res.ok && gen === s.gen) {
          s.profileKey = created + '\0' + at;
          s.forms.create = { name: '', level: 'user', source: '' };
        } else go.disabled = false;
        release();
        if (res.ok && gen === s.gen) await reload();
      });
      const form = el('div', undefined, 'set-profile-form');
      form.append(nameLab, levelLab, sourceLab, problem, go);
      box.append(form, result);
      return box;
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

  return { render, configBody, repoKey, gateKey, targetPath, subsetOk, valueProblem, decideTextProblem, describeDecide, gateTags, replayDate, GATES, GATE_HELP, SETUP_GUIDE,
    pickDefault, groupRuns, optionLabel, typeAhead, editorOf, isDirty, dirtyState, edited, typed, shown, readFailure,
    unsavedLabels, departedRun,
    describeProfile, profileRequest, profileCopySpec, clearDefaultLevel, profileOffers, profileRowText, shadowLabel, refusalReasons, profileText, profileJsonProblem, profileNameProblem,
    FILES, GATES, MODES, VALUE_MAX, DECIDE_VALUE_MAX, ADD_COMMAND, PROFILE_LEVELS, WRITE_LEVELS, TIERS };
});
