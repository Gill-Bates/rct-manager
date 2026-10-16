//
// tests/js/admin-logout-snapshot.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Logout must not leave user-scoped dashboard state in localStorage. Run with Node's built-in
// runner: `node --test tests/js/*.test.js`. The behavior is exercised from the shipped source so a
// regression in either the snapshot owner or the logout wiring fails here.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DATA_SOURCE = fs.readFileSync(
  path.join(__dirname, '../../app/admin/static/js/admin-dashboard-data.js'),
  'utf8',
);
const CORE_SOURCE = fs.readFileSync(
  path.join(__dirname, '../../app/admin/static/js/admin-core.js'),
  'utf8',
);
const CONNECTION_SOURCE = fs.readFileSync(
  path.join(__dirname, '../../app/admin/static/js/connection.js'),
  'utf8',
);

function sourceBetween(source, start, end) {
  const from = source.indexOf(start);
  const until = source.indexOf(end, from);
  assert.ok(from >= 0 && until > from, `${start} must stay discoverable`);
  return source.slice(from, until);
}

function clearSnapshotHelper() {
  // Pull SNAPSHOT_KEY and clearDashboardSnapshot() verbatim from the shipped module and run them
  // against a fake localStorage, so the test exercises the real implementation, not a copy.
  const keyLine = DATA_SOURCE.match(/const SNAPSHOT_KEY = '[^']+';/);
  assert.ok(keyLine, 'SNAPSHOT_KEY must stay discoverable in admin-dashboard-data.js');
  const versionLine = DATA_SOURCE.match(/const SNAPSHOT_VERSION = \d+;/);
  assert.ok(versionLine, 'SNAPSHOT_VERSION must stay discoverable in admin-dashboard-data.js');
  const fnStart = DATA_SOURCE.indexOf('function clearDashboardSnapshot()');
  assert.ok(fnStart >= 0, 'clearDashboardSnapshot() must exist in admin-dashboard-data.js');
  // The function closes at its own 2-space indent level ("\n  }"); slice through that.
  const fnEnd = DATA_SOURCE.indexOf('\n  }', fnStart) + '\n  }'.length;
  const store = new Map();
  const context = {
    dashboardSnapshot: { at: Date.now() }, dashboardTsdb: {}, dashboardView: new Map(),
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
  };
  const save = sourceBetween(DATA_SOURCE, '  function saveSnapshot()', '  // Clear user-scoped');
  const source = `${keyLine[0]}\n${versionLine[0]}\nlet dashboardSnapshotInvalidated = false;\n${save}\n${DATA_SOURCE.slice(fnStart, fnEnd)}\n`
    + 'globalThis.__key = SNAPSHOT_KEY; globalThis.__clear = clearDashboardSnapshot; globalThis.__save = saveSnapshot;';
  vm.runInNewContext(source, context);
  return { key: context.__key, clear: context.__clear, save: context.__save, store };
}

test('clearDashboardSnapshot removes the stored dashboard snapshot', () => {
  const { key, clear, store } = clearSnapshotHelper();
  store.set(key, JSON.stringify({ v: 2, at: Date.now(), devices: [] }));
  assert.ok(store.has(key), 'precondition: a snapshot is stored');
  clear();
  assert.ok(!store.has(key), 'the snapshot must be gone after clearDashboardSnapshot()');
});

test('clearDashboardSnapshot tolerates a blocked localStorage without throwing', () => {
  const keyLine = DATA_SOURCE.match(/const SNAPSHOT_KEY = '[^']+';/);
  const fnStart = DATA_SOURCE.indexOf('function clearDashboardSnapshot()');
  const fnEnd = DATA_SOURCE.indexOf('\n  }', fnStart) + '\n  }'.length;
  const context = {
    localStorage: { removeItem() { throw new Error('storage blocked'); } },
  };
  const source = `${keyLine[0]}\nlet dashboardSnapshotInvalidated = false;\n${DATA_SOURCE.slice(fnStart, fnEnd)}\nglobalThis.__clear = clearDashboardSnapshot;`;
  vm.runInNewContext(source, context);
  assert.doesNotThrow(() => context.__clear());
});

test('a completed poll cannot recreate a snapshot after an auth boundary', () => {
  const { key, clear, save, store } = clearSnapshotHelper();
  save();
  assert.ok(store.has(key));
  clear();
  save();
  assert.ok(!store.has(key));
});

test('the logout handler clears the dashboard snapshot', () => {
  // Wiring guard: the logout path must call clearDashboardSnapshot(), otherwise a logged-out
  // browser keeps the previous user's device telemetry in localStorage.
  const logoutStart = CORE_SOURCE.indexOf("$('logout-button')");
  assert.ok(logoutStart >= 0, 'the logout-button handler must stay discoverable');
  // The handler ends at the catch clause; slice through it so the whole body is covered.
  const handlerEnd = CORE_SOURCE.indexOf('catch (error)', logoutStart);
  assert.ok(handlerEnd > logoutStart, 'the logout handler must keep its catch clause');
  const handler = CORE_SOURCE.slice(logoutStart, handlerEnd);
  assert.match(handler, /clearDashboardSnapshot\(\)/, 'logout must call clearDashboardSnapshot()');
});

test('an API 401 clears the snapshot before redirecting to login', async () => {
  const actions = [];
  const context = {
    page: 'dashboard', csrfToken: '',
    fetch: async () => ({ ok: false, status: 401, json: async () => ({}) }),
    window: { RCTReconnect: null },
    clearDashboardSnapshot: () => actions.push('clear'),
    location: { assign: (url) => actions.push(url) },
  };
  vm.runInNewContext(`${sourceBetween(CORE_SOURCE, 'async function api(', 'async function session()')}globalThis.__api = api;`, context);
  await assert.rejects(context.__api('devices'), /Request failed \(401\)/);
  assert.deepEqual(actions, ['clear', '/login']);
});

test('a heartbeat 401 clears the snapshot before redirecting to login', () => {
  const actions = [];
  const context = {
    loginRedirected: false,
    window: { location: { pathname: '/ui/dashboard', replace: (url) => actions.push(url) } },
    clearDashboardSnapshot: () => actions.push('clear'),
  };
  const redirect = sourceBetween(CONNECTION_SOURCE, '  function redirectToLoginIfNeeded()', '  function safeHideModal(');
  vm.runInNewContext(`${redirect}globalThis.__redirect = redirectToLoginIfNeeded;`, context);
  context.__redirect();
  context.__redirect();
  assert.deepEqual(actions, ['clear', '/login']);
});

test('successful login clears the old snapshot before entering the dashboard', async () => {
  const actions = [];
  const context = {
    clearFormError() {}, submitState() {},
    api: async () => ({ must_change_password: false }),
    $: (id) => ({ value: id === 'username' ? 'admin' : 'password' }),
    clearDashboardSnapshot: () => actions.push('clear'),
    location: { assign: (url) => actions.push(url) },
    formError() {}, messageFrom: (error) => error.message,
  };
  vm.runInNewContext(`${sourceBetween(CORE_SOURCE, 'async function submitLogin(', 'async function submitPasswordChange(')}globalThis.__submit = submitLogin;`, context);
  await context.__submit({});
  assert.deepEqual(actions, ['clear', '/ui/dashboard']);
});

test('failed login leaves the snapshot untouched', async () => {
  const actions = [];
  const context = {
    clearFormError() {}, submitState() {},
    api: async () => { throw new Error('Invalid credentials'); },
    $: () => ({ value: 'user' }),
    clearDashboardSnapshot: () => actions.push('clear'),
    location: { assign: (url) => actions.push(url) },
    formError() {}, messageFrom: (error) => error.message,
  };
  vm.runInNewContext(`${sourceBetween(CORE_SOURCE, 'async function submitLogin(', 'async function submitPasswordChange(')}globalThis.__submit = submitLogin;`, context);
  await context.__submit({});
  assert.deepEqual(actions, []);
});
