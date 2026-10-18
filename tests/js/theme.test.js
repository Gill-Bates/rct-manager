//
// tests/js/theme.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Theme preference state machine of app/admin/static/js/theme.js: Auto/Light/Dark cycle,
// persistence and migration of the old two-state value, and live reaction to the system scheme.
// Run with `node --test tests/js/*.test.js`.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../../app/admin/static/js/theme.js');

function env({ stored, dark = false, throwing = false } = {}) {
  const attrs = {};
  const events = [];
  const listeners = [];
  const store = new Map(stored === undefined ? [] : [[T.KEY, stored]]);
  const storage = {
    getItem: (k) => { if (throwing) throw new Error('blocked'); return store.has(k) ? store.get(k) : null; },
    setItem: (k, v) => { if (throwing) throw new Error('blocked'); store.set(k, v); },
  };
  const mql = {
    matches: dark,
    addEventListener: (_t, fn) => listeners.push(fn),
  };
  const win = {
    matchMedia: () => mql,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    dispatchEvent: (e) => events.push(e),
  };
  const doc = { documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } };
  const state = T.create(win, doc, storage);
  const setSystem = (v) => { mql.matches = v; listeners.forEach((fn) => fn()); };
  return { state, attrs, events, store, setSystem };
}

test('default without storage entry is auto and follows the system scheme', () => {
  assert.equal(env({ dark: true }).attrs['data-bs-theme'], 'dark');
  const e = env({ dark: false });
  assert.equal(e.state.pref, 'auto');
  assert.equal(e.attrs['data-bs-theme'], 'light');
  assert.equal(e.attrs['data-theme-pref'], 'auto');
});

test('old two-state values are kept; unknown values fall back to auto', () => {
  assert.equal(env({ stored: 'dark' }).attrs['data-bs-theme'], 'dark');
  assert.equal(env({ stored: 'light', dark: true }).attrs['data-bs-theme'], 'light');
  assert.equal(env({ stored: 'banana' }).state.pref, 'auto');
  assert.equal(env({ stored: '' }).state.pref, 'auto');
});

test('cycle runs auto -> light -> dark -> auto and persists each step', () => {
  const e = env();
  assert.deepEqual([e.state.cycle(), e.state.cycle(), e.state.cycle()], ['light', 'dark', 'auto']);
  assert.equal(e.store.get(T.KEY), 'auto');
  e.state.cycle();
  assert.equal(e.store.get(T.KEY), 'light');
  assert.equal(e.events.at(-1).detail.theme, 'light');
});

test('auto reacts live to system changes, explicit choices ignore them', () => {
  const e = env({ dark: false });
  e.setSystem(true);
  assert.equal(e.attrs['data-bs-theme'], 'dark');
  assert.equal(e.events.at(-1).detail.theme, 'dark');
  e.state.set('light');
  const count = e.events.length;
  e.setSystem(false);
  e.setSystem(true);
  assert.equal(e.attrs['data-bs-theme'], 'light');
  assert.equal(e.events.length, count);
});

test('throwing localStorage keeps the page working with auto default', () => {
  const e = env({ throwing: true, dark: true });
  assert.equal(e.state.pref, 'auto');
  assert.equal(e.attrs['data-bs-theme'], 'dark');
  assert.equal(e.state.set('light'), 'light');
  assert.equal(e.attrs['data-bs-theme'], 'light');
});

test('missing matchMedia resolves auto to light', () => {
  assert.equal(T.resolve('auto', {}), 'light');
  assert.equal(T.next('nonsense'), 'light');
});
