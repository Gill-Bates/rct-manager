//
// tests/e2e/admin_rate_limit.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E: several Energy-page tabs polling for 95 s stay below the admin-session rate limit
// (no 429 in the responses or the access log); a request flood still answers 429 with Retry-After
// and is admitted again once that time passed.
// Usage: PLAYWRIGHT_DIR=<dir with node_modules/playwright> node tests/e2e/admin_rate_limit.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const TABS = 3;
const SOAK_MS = 95_000;
const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
const serverLog = path.join(OUT, 'server.log');
spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1500);
const server = spawnPy(['tests.e2e.run_server', path.join(dbDir, 'e2e.db'), String(httpPort), String(devicePort), 'energy'], serverLog);
for (let i = 0; i < 100; i++) {
  try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* not up yet */ }
  await sleep(200);
}
const initial = /One-time password:\s+(\S+)/.exec(server.output())?.[1];
if (!initial) throw new Error('first-start password was not printed in the banner inside the test database');

const browser = await engine.launch();
try {
  const first = await browser.newContext({ locale: 'en-GB' });
  const login = await first.newPage();
  await login.goto(`${base}/`);
  await login.fill('#password', initial);
  await login.click('#login-form button[type=submit]');
  await login.waitForURL('**/change-password');
  await login.fill('#current-password', initial);
  await login.fill('#new-password', NEW_PASSWORD);
  await login.fill('#confirm-password', NEW_PASSWORD);
  await login.click('#change-password-form button[type=submit]');
  await login.waitForURL('**/ui/dashboard');
  try {
    await login.locator('#disclaimer-accept').waitFor({ state: 'visible', timeout: 5000 });
    await Promise.all([login.waitForNavigation(), login.click('#disclaimer-accept')]);
  } catch { /* already accepted */ }
  const storageState = await first.storageState();
  await first.close();

  // Soak: every tab shares one browser session, the strictest case for one session budget.
  const limited = [];
  const polls = { count: 0 };
  const pages = [];
  for (let i = 0; i < TABS; i++) {
    const context = await browser.newContext({ storageState, locale: 'en-GB' });
    const page = await context.newPage();
    page.on('response', (response) => {
      if (!response.url().includes('/admin/api/')) return;
      polls.count += 1;
      if (response.status() === 429) limited.push(response.url());
    });
    await page.goto(`${base}/ui/energy`);
    pages.push(page);
  }
  await sleep(SOAK_MS);
  check('tabs kept polling during the soak', polls.count > TABS * 20, `${polls.count} admin API responses`);
  check('no 429 for multi-tab Energy polling', limited.length === 0, `${limited.length} responses`);
  const noteText = await pages[0].locator('#energy-list').innerText();
  check('Energy page shows no retry note', !/Retrying/.test(noteText), noteText.slice(0, 80));

  // Flood: 700 requests in one burst exceed the default 600 per minute.
  const flood = await pages[0].evaluate(async () => {
    const codes = [];
    let retryAfter = null;
    for (let i = 0; i < 700; i++) {
      const response = await fetch('/admin/api/devices', { credentials: 'same-origin' });
      codes.push(response.status);
      if (response.status === 429 && retryAfter === null) retryAfter = response.headers.get('Retry-After');
    }
    return { limited: codes.filter((code) => code === 429).length, retryAfter };
  });
  check('flood is answered with 429', flood.limited > 0, `${flood.limited} of 700`);
  check('429 carries Retry-After', Number(flood.retryAfter) >= 1, String(flood.retryAfter));

  // The GUI polls keep backing off while the budget is empty; after Retry-After a request passes.
  await sleep((Number(flood.retryAfter) + 3) * 1000);
  const after = await pages[0].evaluate(async () => (await fetch('/admin/api/devices', { credentials: 'same-origin' })).status);
  check('client is admitted again after Retry-After', after === 200, String(after));
  await sleep(8000);
  const text = await pages[0].locator('#energy-list').innerText();
  check('Energy page recovered after the flood', !/unavailable/i.test(text), text.slice(0, 80));
} finally {
  await browser.close();
}

await sleep(500);
const log = fs.readFileSync(serverLog, 'utf8');
const logged429 = log.split('\n').filter((line) => /app\.access/.test(line) && /status=429/.test(line));
// Only the flood may be limited: the soak phase produced none, so every logged 429 belongs to the flood.
check('access log has 429 only from the flood', logged429.length > 0 && logged429.every((line) => /\/admin\/api\/devices/.test(line)), `${logged429.length} lines`);
fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
process.exit(results.every((item) => item.ok) ? 0 : 1);
