/* forge-ui theme: system, light or dark. Loaded in <head> without defer, so the page never paints in the wrong theme.
   <html data-theme="light|dark"> overrides the system setting; no attribute follows it. The resolved theme is announced
   with a 'forge-theme' event on window ({detail: 'light'|'dark'}) whenever it changes, for the 3D stage.
   window.ForgeTheme = { get(), set('system'|'light'|'dark'), resolved() } */
window.ForgeTheme = (() => {
  'use strict';
  const KEY = 'forge-ui-theme', root = document.documentElement;
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const read = () => { try { const v = JSON.parse(localStorage.getItem(KEY)); return v === 'light' || v === 'dark' ? v : 'system'; } catch (_) { return 'system'; } };
  let choice = read(), last = null;
  const resolved = () => choice === 'system' ? (media && media.matches ? 'dark' : 'light') : choice;
  function apply() {
    if (choice === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', choice);
    const now = resolved();
    if (now !== last) { last = now; window.dispatchEvent(new CustomEvent('forge-theme', { detail: now })); }
  }
  function set(value) {
    choice = value === 'light' || value === 'dark' ? value : 'system';
    try { localStorage.setItem(KEY, JSON.stringify(choice)); } catch (_) { /* private mode: not remembered */ }
    apply();
  }
  if (media) media.addEventListener('change', apply);
  apply();
  return { get: () => choice, set, resolved };
})();
