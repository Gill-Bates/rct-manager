//
// tests/e2e/dashboard_resilience.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E: a failed, empty or "connecting" poll must not rebuild or shrink the Overview. Values
// stay until they are older than the next expected poll, then read n/a; the layout never changes.
// Usage: PLAYWRIGHT_DIR=<dir containing node_modules/playwright> node tests/e2e/dashboard_resilience.mjs <artifact-dir>
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(path.join(process.env.PLAYWRIGHT_DIR, 'noop.js'));
const playwright = require('playwright');
// BROWSER=chromium|webkit|firefox, VIEWPORT_WIDTH and COLOR_SCHEME=light|dark select the matrix cell.
const engine = playwright[process.env.BROWSER || 'chromium'];
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const OUT = path.resolve(process.argv[2]);
fs.mkdirSync(OUT, { recursive: true });
const PY = process.env.PYTHON || path.join(ROOT, '.venv/bin/python');
const NEW_PASSWORD = 'e2e-a-much-stronger-password';
const VALUE_MAX_AGE_MS = 20000; // keep in sync with VALUE_MAX_AGE_MS in admin.js
const results = [];
const problems = [];

const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const freePort = () => new Promise((resolve) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); }); });
const procs = [];
process.once('exit', () => { for (const proc of procs) if (proc.exitCode === null) proc.kill('SIGTERM'); });
function spawnPy(args, log) {
  const proc = spawn(PY, ['-m', ...args], { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
  let out = '';
  proc.stdout.on('data', (d) => { out += d; fs.appendFileSync(log, d); });
  proc.stderr.on('data', (d) => fs.appendFileSync(log, d));
  procs.push(proc);
  return { proc, output: () => out };
}

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1500);
const server = spawnPy(['tests.e2e.run_server', path.join(dbDir, 'e2e.db'), String(httpPort), String(devicePort)], path.join(OUT, 'server.log'));
for (let i = 0; i < 100; i++) {
  try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* not up yet */ }
  await sleep(200);
}
const announced = /Password file:\s+(\S+)\s+\(0600\)/.exec(server.output())?.[1];
if (!announced || !path.resolve(announced).startsWith(dbDir + path.sep)) throw new Error('first-start password file was not announced inside the test database');
const initial = fs.readFileSync(path.resolve(announced), 'utf8').trim();

const browser = await engine.launch();
const context = await browser.newContext({
  viewport: { width: Number(process.env.VIEWPORT_WIDTH || 1280), height: 900 }, locale: 'en-GB',
  colorScheme: process.env.COLOR_SCHEME || 'light',
});
const page = await context.newPage();
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
const shot = (name) => page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });

await page.goto(base + '/');
await page.fill('#password', initial);
await page.click('#login-form button[type=submit]');
await page.waitForURL('**/change-password');
await page.fill('#current-password', initial);
await page.fill('#new-password', NEW_PASSWORD);
await page.fill('#confirm-password', NEW_PASSWORD);
await page.click('#change-password-form button[type=submit]');
await page.waitForURL('**/ui/dashboard');

// The shape of the dashboard: everything a collapse would remove, plus the grid's vertical position.
const inventory = () => page.evaluate(() => ({
  cards: document.querySelectorAll('.device-item').length,
  subcards: [...document.querySelectorAll('.device-subcard-title')].map((e) => e.textContent.trim()),
  slices: document.querySelectorAll('.device-battery-slice').length,
  flow: Boolean(document.querySelector('.device-flow-graphic:not([hidden]) .energy-flow-svg')),
  cells: document.querySelectorAll('.device-metric-card').length,
  gridTop: Math.round(document.getElementById('dashboard-grid').getBoundingClientRect().top),
  pv: document.getElementById('pv-power').textContent.replace(/\s+/g, ' ').trim(),
  firstCell: document.querySelector('.device-metric-card dd')?.textContent.replace(/\s+/g, ' ').trim(),
  status: document.querySelector('.device-item .device-head-main')?.textContent.replace(/\s+/g, ' ').trim(),
  banner: (() => { const b = document.querySelector('.dashboard-offline-banner'); return b && !b.hidden ? b.textContent : null; })(),
}));
const forcePoll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
const poll = async (status) => {
  const response = page.waitForResponse((r) => new URL(r.url()).pathname === '/admin/api/devices' && r.status() === status, { timeout: 20000 });
  await forcePoll();
  await response;
  await sleep(400); // the render follows the response in the same task chain
};

await page.waitForFunction(() => document.querySelectorAll('.device-subcard').length >= 3
  && document.querySelector('.device-flow-graphic:not([hidden])')
  && /2,035/.test(document.getElementById('pv-power').textContent)
  && !document.getElementById('dashboard-grid').classList.contains('dashboard-grid--loading'), null, { timeout: 60000 });
await page.evaluate(() => { document.querySelector('.device-item').dataset.probe = 'kept'; });
const full = await inventory();
await shot('resilience-0-full');
check('the simulator renders the full dashboard (3 cards, two towers, flow graphic)',
  full.subcards.length === 3 && full.slices > 0 && full.flow && /2,035/.test(full.pv), JSON.stringify(full));

// 1. restart-like answer: 200, inverter connecting, nothing cached.
const emptyPayload = (route) => route.fetch().then(async (response) => {
  const body = await response.json();
  for (const device of body.devices) { device.status = 'starting'; device.metrics = []; device.batteries = []; device.energy_flow = null; }
  return route.fulfill({ response, json: body });
});
await page.route('**/admin/api/devices', emptyPayload);
await poll(200);
const connecting = await inventory();
await shot('resilience-1-connecting');
check('an empty "connecting" answer keeps the layout and the last values',
  JSON.stringify({ ...connecting, status: 0 }) === JSON.stringify({ ...full, status: 0 }) && /Connecting/.test(connecting.status), JSON.stringify(connecting));

// 2. the next expected poll brings nothing again: the values turn n/a, the layout stays.
await sleep(VALUE_MAX_AGE_MS + 1500);
await poll(200);
const expired = await inventory();
await shot('resilience-2-expired');
check('values read n/a once they are older than the next expected poll',
  expired.pv === 'n/a' && expired.firstCell === 'n/a', JSON.stringify(expired));
check('the layout is unchanged while the values are n/a',
  expired.cards === full.cards && JSON.stringify(expired.subcards) === JSON.stringify(full.subcards)
  && expired.slices === full.slices && expired.flow && expired.cells === full.cells && expired.gridTop === full.gridTop, JSON.stringify(expired));

// 3. a server error: the note replaces the subtitle, so nothing moves.
await page.unroute('**/admin/api/devices', emptyPayload);
await page.route('**/admin/api/devices', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' }));
await poll(503);
const failed = await inventory();
await shot('resilience-3-503');
check('a 503 shows its note without moving the grid or removing anything',
  /Server error \(503\)/.test(failed.banner || '') && failed.gridTop === full.gridTop && failed.subcards.length === full.subcards.length && failed.slices === full.slices, JSON.stringify(failed));

// 4. reload while the server only answers "connecting": the stored structure is restored.
await page.unroute('**/admin/api/devices');
await page.route('**/admin/api/devices', emptyPayload);
await page.reload();
await page.waitForFunction(() => !document.getElementById('dashboard-grid').classList.contains('dashboard-grid--loading'), null, { timeout: 20000 });
const reloaded = await inventory();
await shot('resilience-4-reload-connecting');
check('a reload against an empty answer restores the stored layout',
  reloaded.subcards.length === full.subcards.length && reloaded.slices === full.slices && reloaded.flow && reloaded.gridTop === full.gridTop, JSON.stringify(reloaded));

// 5. valid data returns: values update in place, no rebuild.
await page.unroute('**/admin/api/devices');
await page.evaluate(() => { document.querySelector('.device-item').dataset.probe = 'kept'; });
await poll(200);
await page.waitForFunction(() => /2,035/.test(document.getElementById('pv-power').textContent), null, { timeout: 20000 });
const back = await inventory();
check('valid data updates the values in the existing card', back.subcards.length === full.subcards.length
  && back.slices === full.slices && back.banner === null && (await page.locator('.device-item[data-probe="kept"]').count()) === 1, JSON.stringify(back));

check('no unexpected console or page errors', problems.every((p) => /status of 503|Failed to load resource/.test(p)), problems.join(' | '));
await browser.close();
fs.writeFileSync(path.join(OUT, 'resilience-results.json'), JSON.stringify(results, null, 2));
const failedChecks = results.filter((r) => !r.ok);
console.log(`${results.length - failedChecks.length}/${results.length} checks passed`);
process.exit(failedChecks.length ? 1 : 0);
