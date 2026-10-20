/* forge-ui: loads the 3D stage only when it is used. scene3d.js imports three.js 0.160.0 from
   cdn.jsdelivr.net through index.html's import map; without WebGL, without import maps, offline or behind
   a server that predates the page's CSP, the load fails and the page keeps its 2D views.
   Defines window.ForgeMap3D = {supported(), load(), state, reason, message()}. */
window.ForgeMap3D = (() => {
  'use strict';
  const M = { state: 'idle', reason: null, module: null };
  let loading = null;
  M.supported = () => {
    if (!(window.HTMLScriptElement && typeof HTMLScriptElement.supports === 'function' && HTMLScriptElement.supports('importmap'))) return 'importmap';
    try {
      const c = document.createElement('canvas'), gl = c.getContext('webgl2') || c.getContext('webgl');
      if (!gl) return 'webgl';
      const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();   // a probe must not hold a context
    } catch (_) { return 'webgl'; }
    return null;
  };
  M.load = () => {
    if (loading) return loading;
    const why = M.supported();
    if (why) { M.state = 'unavailable'; M.reason = why; loading = Promise.reject(new Error(why)); loading.catch(() => {}); return loading; }
    M.state = 'loading';
    let timer = null;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), 12000); });
    // an exported page carries the stage as one module text (forge_ui/export.py), imported from a blob: URL
    const bundle = document.getElementById('forge-stage');
    const target = bundle ? URL.createObjectURL(new Blob([bundle.textContent], { type: 'text/javascript' })) : './scene3d.js';
    loading = Promise.race([import(target), timeout]).then(mod => {
      clearTimeout(timer); M.state = 'ready'; M.module = mod; return mod;
    }, err => {
      clearTimeout(timer); M.state = 'unavailable'; M.reason = err && err.message === 'timeout' ? 'timeout' : 'load';
      throw err;
    });
    loading.catch(() => {});
    return loading;
  };
  M.message = () => ({
    importmap: '3D view unavailable: this browser does not support import maps.',
    webgl: '3D view unavailable: WebGL is not available in this browser.',
    timeout: '3D view unavailable: three.js took too long to load from cdn.jsdelivr.net.',
    load: '3D view unavailable: three.js could not load from cdn.jsdelivr.net (offline, blocked, or a forge-ui server older than this page: run `forge ui open` to restart it).',
    stage: '3D view unavailable: the 3D stage could not start in this browser.',
  })[M.reason] || '3D view unavailable.';
  return M;
})();
