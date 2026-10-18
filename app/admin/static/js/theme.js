//
// app/admin/static/js/theme.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Theme preference: 'auto' (follow prefers-color-scheme), 'light' or 'dark'. Loaded in <head>
// so the resolved theme is on <html data-bs-theme> before first paint. The stored value keeps
// the former two-state format ('light'/'dark'); anything else, or no storage, means 'auto'.
(function (root) {
  'use strict';
  const KEY = 'rct-admin-theme';
  const ORDER = ['auto', 'light', 'dark'];
  const QUERY = '(prefers-color-scheme: dark)';
  const EVENT = 'rct-theme-change';

  function normalize(value) {
    return ORDER.includes(value) ? value : 'auto';
  }

  function load(storage) {
    try { return normalize(storage.getItem(KEY)); } catch (e) { return 'auto'; }
  }

  function save(storage, pref) {
    try { storage.setItem(KEY, pref); return true; } catch (e) { return false; }
  }

  function systemDark(win) {
    try { return !!win.matchMedia(QUERY).matches; } catch (e) { return false; }
  }

  function resolve(pref, win) {
    return pref === 'auto' ? (systemDark(win) ? 'dark' : 'light') : pref;
  }

  function next(pref) {
    return ORDER[(ORDER.indexOf(normalize(pref)) + 1) % ORDER.length];
  }

  function apply(doc, win, pref) {
    const theme = resolve(pref, win);
    doc.documentElement.setAttribute('data-bs-theme', theme);
    doc.documentElement.setAttribute('data-theme-pref', pref);
    return theme;
  }

  function emit(win, pref, theme) {
    try { win.dispatchEvent(new win.CustomEvent(EVENT, { detail: { pref, theme } })); } catch (e) { /* no DOM events */ }
  }

  // Wires the live state for one window; returns controls used by the shell and by tests.
  function create(win, doc, storage) {
    let pref = load(storage);
    let mql = null;
    const onSystemChange = () => {
      if (pref !== 'auto') return;
      emit(win, pref, apply(doc, win, pref));
    };
    try {
      mql = win.matchMedia(QUERY);
      if (mql.addEventListener) mql.addEventListener('change', onSystemChange);
      else if (mql.addListener) mql.addListener(onSystemChange);
    } catch (e) { mql = null; }
    apply(doc, win, pref);
    return {
      get pref() { return pref; },
      set(value) {
        pref = normalize(value);
        save(storage, pref);
        const theme = apply(doc, win, pref);
        emit(win, pref, theme);
        return pref;
      },
      cycle() { return this.set(next(pref)); },
    };
  }

  const api = { KEY, ORDER, EVENT, normalize, load, save, resolve, next, create };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root && root.document) {
    let storage = null;
    try { storage = root.localStorage; } catch (e) { storage = null; }
    root.RctTheme = api;
    root.rctThemeState = create(root, root.document, storage || { getItem() { throw new Error('no storage'); }, setItem() { throw new Error('no storage'); } });
  }
})(typeof window !== 'undefined' ? window : null);
