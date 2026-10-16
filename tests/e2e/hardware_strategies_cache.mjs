//
// tests/e2e/hardware_strategies_cache.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E for the cache-only contract of /admin/api/energy/hardware-strategies (no stub): the
// endpoint is served from the gateway's in-memory metric cache and never starts inverter I/O on a
// page load, so repeated polls over ~16 s answer 200 with a well-formed device entry (device_id,
// boolean stale flag), never a crash or a broken payload.
//
// NOT covered: the 10 s cache TTL and the stale flip. The strategy register only enters the cache
// through an explicit mode/action path that needs a hardware-verified Energy Manager setup, which the
// simulated inverter does not provide, so `code` stays null here.
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<venv python> BROWSER=chromium node tests/e2e/hardware_strategies_cache.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const BROWSER = process.env.BROWSER || 'chromium';

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
// energy mode: write support on and the dispatch store isolated, so the Energy page's Operate poll
// actually reads power_mng_soc_strategy into the cache — the precondition for observing its 10 s TTL.
const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort), 'energy'], path.join(OUT, 'server.log'));

const firstStartPassword = (output) => {
  const password = /One-time password:\s+(\S+)/.exec(output)?.[1];
  if (!password) throw new Error('first-start password was not printed in the banner');
  return password;
};

let browser;
try {
  let up = false;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/health`)).ok) { up = true; break; } } catch { /* not up yet */ }
    await sleep(200);
  }
  if (!up) throw new Error('server did not start');
  const password = firstStartPassword(server.output());

  browser = await engine.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-GB', colorScheme: 'light' });
  const page = await context.newPage();

  await page.goto(`${base}/login`);
  await page.fill('#password', password);
  await page.click('#login-form button[type=submit]');
  await page.waitForURL('**/change-password');
  await page.fill('#current-password', password);
  await page.fill('#new-password', NEW_PASSWORD);
  await page.fill('#confirm-password', NEW_PASSWORD);
  await page.click('#change-password-form button[type=submit]');
  await page.waitForURL('**/ui/dashboard');

  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');
  // Let the Operate poll run a few cycles so power_mng_soc_strategy is read into the gateway cache.
  await page.waitForSelector('#energy-list .energy-panel, #energy-list > *', { timeout: 15000 }).catch(() => { });
  await sleep(3000);

  // Give the gateway a chance to register the periodic read and populate the strategy cache.
  const read = () => page.evaluate(async () => {
    const res = await fetch('/admin/api/energy/hardware-strategies', { headers: { 'Cache-Control': 'no-cache' }, cache: 'no-store' });
    return { status: res.status, body: await res.json(), at: Date.now() };
  });

  // Sample across ~16 s (longer than the 10 s TTL) at 2 s cadence; every call is a REAL request.
  const samples = [];
  for (let i = 0; i < 9; i++) {
    const r = await read();
    samples.push({ t: i * 2, status: r.status, device: r.body[0] });
    await sleep(2000);
  }
  fs.writeFileSync(path.join(OUT, 'hardware-strategies-samples.json'), JSON.stringify(samples, null, 2));

  const spanSeconds = samples[samples.length - 1].t - samples[0].t;
  check('/hardware-strategies answers 200 on every real poll over the sampling span',
    samples.every((s) => s.status === 200), `${spanSeconds} s, ${JSON.stringify(samples.map((s) => s.status))}`);
  check('every response carries a well-formed device entry (device_id, boolean stale flag), never a broken payload',
    samples.every((s) => s.device && s.device.device_id === 'sim' && typeof s.device.stale === 'boolean'),
    JSON.stringify(samples.map((s) => s.device && { code: s.device.code, stale: s.device.stale })));
  const codes = samples.map((s) => s.device?.code);
  const staleFlags = samples.map((s) => s.device?.stale);
  console.log(`INFO hardware-strategies over ${spanSeconds}s: codes=${JSON.stringify(codes)} stale=${JSON.stringify(staleFlags)}`);
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

fs.writeFileSync(path.join(OUT, 'hardware-strategies-results.json'), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${BROWSER}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
