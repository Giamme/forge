/* forge-ui shared write helpers: window.ForgeUI, and a CommonJS module under node:test. Every page that changes a run
   uses these and none re-implements them.
     el(tag, text?, className?)            a DOM element; text enters as text
     token()                               the write token from <meta name="forge-ui-token">, or null
     controlsEnabled()                     Promise<bool>: a token AND /api/health says controls:true; asked once per page load
     get(path) / post(path, body)          Promise<{ok, status, data, error, detail, result}>; post sends the token and never retries
     outcome(host, res, okText)            an inline result line (a lamp and a sentence) in an aria-live region
     confirmInline(host, text, {danger})   Promise<bool>: an inline Confirm / Cancel strip, Escape cancels
   The write API is described in ui/docs/api.md, "Controls (write endpoints)". All API values enter the DOM as text. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeUI = api.create(root);
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TOKEN_HEADER = 'X-Forge-UI-Token';
  const TIMED_OUT = 'timed out: it may have partly completed. Reload to see the current state.';

  // The fetch options for a request. A POST carries the token and a JSON body; nothing else ever does.
  function requestInit(method, body, token) {
    const init = { method, cache: 'no-store', credentials: 'same-origin', headers: {} };
    if (method === 'POST') {
      init.headers[TOKEN_HEADER] = token;
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body === undefined ? {} : body);
    }
    return init;
  }

  const object = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;

  // An HTTP answer as {ok, status, data, error, detail, result}. `status` 0 is "no answer" (the network, or no token).
  // `result` is what the server passes through from the forge CLI's own stdout (an error body may hold it).
  function mapResponse(status, data) {
    const body = object(data);
    if (status >= 200 && status < 300) {
      if (body) return { ok: true, status, data: body, error: null, detail: null, result: object(body.result) };
      return { ok: false, status, data: null, error: 'bad response', detail: 'the server answered without a JSON object', result: null };
    }
    const error = body && typeof body.error === 'string' ? body.error : status === 0 ? 'network' : 'http ' + status;
    const detail = body && typeof body.detail === 'string' ? body.detail : status === 0 ? 'could not reach forge-ui' : null;
    return { ok: false, status, data: body, error, detail, result: body ? object(body.result) : null };
  }

  // What an inline result line says: {tone: 'ok' | 'bad', text}.
  function outcomeText(res, okText) {
    if (res && res.ok) return { tone: 'ok', text: okText || 'done' };
    const r = res || {};
    if (r.status === 409 && r.error === 'busy') return { tone: 'bad', text: 'busy, try again' };
    if (r.status === 504 || r.error === 'timeout') return { tone: 'bad', text: TIMED_OUT };
    if (r.status === 403) return { tone: 'bad', text: 'not allowed: reload the page (forge-ui may have restarted)' };
    if (r.status === 405) return { tone: 'bad', text: 'this server is read-only' };
    return { tone: 'bad', text: r.detail || r.error || 'failed' };
  }

  // The helpers over one environment ({document, fetch}); the page makes one over `window`, a test one over fakes.
  function create(env) {
    let enabled = null;                                      // Promise<bool>, kept for the page load
    const open = new WeakMap();                              // host → the cancel of the confirm strip it holds

    function el(tag, text, className) {
      const node = env.document.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }

    function token() {
      const meta = env.document && env.document.querySelector('meta[name="forge-ui-token"]');
      const value = meta && meta.getAttribute('content');
      return value ? value : null;
    }

    async function request(path, init) {
      let response;
      try { response = await env.fetch(path, init); } catch (_) { return mapResponse(0, null); }
      let data = null;
      try { data = await response.json(); } catch (_) { /* an empty or non-JSON body maps to the status alone */ }
      return mapResponse(response.status, data);
    }
    const get = path => request(path, requestInit('GET'));
    // Never retried: a write that timed out may have happened, and the caller says what to do next.
    function post(path, body) {
      const t = token();
      if (!t) return Promise.resolve(mapResponse(0, { error: 'no token', detail: 'this page is read-only: it has no write token' }));
      return request(path, requestInit('POST', body, t));
    }

    function controlsEnabled() {
      if (enabled) return enabled;
      if (!token()) return (enabled = Promise.resolve(false));
      const asked = get('/api/health').then(res => {
        if (res.status === 0) enabled = null;                // the server did not answer: ask again next time
        return res.ok && res.data.controls === true;
      });
      return (enabled = asked);
    }

    function paint(line, o, silent) {
      line.className = 'ui-outcome ' + o.tone;
      line.setAttribute('aria-live', silent ? 'off' : 'polite');
      line.replaceChildren(el('i', undefined, 'lamp st-' + (o.tone === 'ok' ? 'pass' : 'fail')), el('span', o.text));
    }
    // A live region has to exist before its text changes to be announced, so a new one is filled on the next tick.
    // `opts.silent` draws a result again without announcing it twice.
    function outcome(host, res, okText, opts) {
      const o = outcomeText(res, okText), silent = !!(opts && opts.silent);
      let line = Array.from(host.children).find(c => c.classList.contains('ui-outcome'));
      if (line) { paint(line, o, silent); return line; }
      line = el('p', undefined, 'ui-outcome');
      line.setAttribute('role', 'status');
      line.setAttribute('aria-atomic', 'true');
      line.setAttribute('aria-live', silent ? 'off' : 'polite');
      host.append(line);
      if (silent) paint(line, o, true); else setTimeout(() => paint(line, o, false), 0);
      return line;
    }

    // One strip per host: asking again cancels the first. Escape and Cancel answer false, Confirm true. Focus goes to
    // Confirm (to Cancel when `danger`, so a stray Enter cannot delete) and returns to where it was.
    function confirmInline(host, text, opts) {
      const danger = !!(opts && opts.danger);
      if (open.has(host)) open.get(host)(false);
      return new Promise(resolve => {
        const returnTo = env.document.activeElement;
        const strip = el('div', undefined, 'ui-confirm' + (danger ? ' danger' : ''));
        strip.setAttribute('role', 'group');
        strip.setAttribute('aria-label', String(text));
        const yes = el('button', 'Confirm', danger ? 'ui-danger' : 'primary'), no = el('button', 'Cancel');
        yes.type = 'button'; no.type = 'button';
        strip.append(el('span', undefined, 'ui-confirm-lamp lamp ' + (danger ? 'st-fail' : 'lamp-route')), el('span', text, 'ui-confirm-text'), yes, no);
        let done = false;
        const finish = answer => {
          if (done) return;
          done = true;
          open.delete(host);
          const inside = strip.contains(env.document.activeElement);
          strip.remove();
          if (inside && returnTo && returnTo.isConnected && returnTo.focus) returnTo.focus({ preventScroll: true });
          resolve(answer);
        };
        open.set(host, finish);
        yes.addEventListener('click', () => finish(true));
        no.addEventListener('click', () => finish(false));
        strip.addEventListener('keydown', e => {
          if (e.key !== 'Escape') return;
          e.preventDefault(); e.stopPropagation();           // the page's own Escape handlers are not for this
          finish(false);
        });
        host.append(strip);
        (danger ? no : yes).focus({ preventScroll: true });
      });
    }

    return { el, token, controlsEnabled, get, post, outcome, confirmInline, requestInit, mapResponse, outcomeText };
  }

  return { create, requestInit, mapResponse, outcomeText, TOKEN_HEADER };
});
