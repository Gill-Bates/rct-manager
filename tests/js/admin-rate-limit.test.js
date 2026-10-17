//
// tests/js/admin-rate-limit.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// HTTP 429 handling of the shared admin fetch helper: Retry-After parsing, the error contract,
// and the page-wide cooldown that keeps background pollers quiet. Run with
// `node --test tests/js/*.test.js`; admin-core.js is executed unmodified in a VM context.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-core.js'), 'utf8');

function harness(responses) {
  const calls = [];
  const queue = [...responses];
  const context = {
    document: { body: { dataset: { page: 'energy' } }, addEventListener() { }, querySelector: () => null },
    addEventListener() { },
    fetch: async (url) => { calls.push(url); return queue.shift(); },
    location: { assign() { } },
    localStorage: { removeItem() { } },
    console,
  };
  context.window = context;
  vm.runInNewContext(`${SOURCE}\nglobalThis.__t = { api, parseRetryAfter, pollDelay, noteRateLimit, rateLimitRemainingMs };`, context);
  return { ...context.__t, calls };
}

const reply = (status, { retryAfter, body = {} } = {}) => ({
  ok: status < 400,
  status,
  headers: { get: (name) => (name.toLowerCase() === 'retry-after' ? retryAfter ?? null : null) },
  json: async () => body,
});

test('parseRetryAfter reads seconds and HTTP dates, defaults and caps the rest', () => {
  const { parseRetryAfter } = harness([]);
  assert.equal(parseRetryAfter('7'), 7);
  assert.equal(parseRetryAfter('0'), 1);
  assert.equal(parseRetryAfter('99999'), 120);
  assert.equal(parseRetryAfter(null), 8);
  assert.equal(parseRetryAfter('soon'), 8);
  const now = Date.parse('2026-01-01T00:00:00Z');
  assert.equal(parseRetryAfter('Thu, 01 Jan 2026 00:00:30 GMT', now), 30);
});

test('a 429 response exposes status and retryAfter on the thrown error', async () => {
  const h = harness([reply(429, { retryAfter: '12', body: { detail: 'Too many requests' } })]);
  await assert.rejects(h.api('energy/devices'), (error) => {
    assert.equal(error.status, 429);
    assert.equal(error.retryAfter, 12);
    assert.equal(error.rateLimited, true);
    return true;
  });
});

test('after a 429 background requests are refused locally until the cooldown ended', async () => {
  const h = harness([reply(429, { retryAfter: '5' }), reply(200, { body: { ok: true } })]);
  await assert.rejects(h.api('energy/devices', { background: true }));
  assert.ok(h.rateLimitRemainingMs() > 0);
  await assert.rejects(h.api('energy/devices', { background: true }), (error) => error.rateLimited === true);
  assert.equal(h.calls.length, 1, 'the refused poll must not reach the server');
  // A user action is never blocked by the cooldown, and a success resets it.
  assert.deepEqual({ ...(await h.api('tokens')) }, { ok: true });
  assert.equal(h.rateLimitRemainingMs(), 0);
});

test('repeated 429s back off exponentially and are capped; jitter stays bounded', () => {
  const h = harness([]);
  const t0 = 1_000_000;
  h.noteRateLimit(5, t0);
  assert.equal(h.rateLimitRemainingMs(t0), 5_000);
  h.noteRateLimit(5, t0 + 5_000); // the cooldown elapsed and it failed again: doubled
  assert.equal(h.rateLimitRemainingMs(t0 + 5_000), 10_000);
  h.noteRateLimit(5, t0 + 15_000);
  assert.equal(h.rateLimitRemainingMs(t0 + 15_000), 20_000);
  for (let i = 0; i < 10; i += 1) h.noteRateLimit(100, t0 + 1_000_000 * (i + 1));
  assert.ok(h.rateLimitRemainingMs(t0 + 11_000_000) <= 120_000);
  const delay = h.pollDelay(3_000, t0 + 11_000_000);
  assert.ok(delay >= 3_000 && delay <= 120_000 + 1_000);
});

test('pollDelay keeps the normal period outside a cooldown', () => {
  const h = harness([]);
  assert.equal(h.pollDelay(3_000), 3_000);
});
