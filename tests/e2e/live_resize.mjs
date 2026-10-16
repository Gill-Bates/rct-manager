//
// tests/e2e/live_resize.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E: a viewport resize DRIVEN MID-SESSION (no reload) must reflow the dashboard and the
// Energy page without introducing horizontal overflow and without tearing the layout down. The page
// is loaded once at desktop width, then resized live through tablet, phone and back; at every step
// the document must not scroll horizontally and the content structure (device cards, energy tabs)
// must still be present. This is the live-resize path the responsive-layout rules require, distinct
// from a fresh load at each width.
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<venv python> BROWSER=chromium|firefox|webkit \
//   COLOR_SCHEME=light|dark node tests/e2e/live_resize.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const THEME = process.env.COLOR_SCHEME === 'dark' ? 'dark' : 'light';
const BROWSER = process.env.BROWSER || 'chromium';
const problems = [];
// Desktop -> tablet landscape -> tablet portrait -> phone -> back to desktop, all without a reload.
const STEPS = [[1280, 900], [1024, 768], [820, 1180], [390, 844], [1280, 900]];

const dbDir = fs.mkdtempSync(path.join(OUT, `db-${THEME}-`));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, `fake-${THEME}.log`));
const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort), 'planning'], path.join(OUT, `server-${THEME}.log`));

const firstStartPassword = (output) => {
  const password = /One-time password:\s+(\S+)/.exec(output)?.[1];
  if (!password) throw new Error('first-start password was not printed in the banner');
  return password;
};

// Measured from the DOM, not from a screenshot: horizontal overflow and the structural inventory a
// broken rebuild would change.
const overflow = (page) => page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth));

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
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-GB', colorScheme: THEME });
  const page = await context.newPage();
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

  await page.goto(`${base}/login`);
  await page.fill('#password', password);
  await page.click('#login-form button[type=submit]');
  await page.waitForURL('**/change-password');
  await page.fill('#current-password', password);
  await page.fill('#new-password', NEW_PASSWORD);
  await page.fill('#confirm-password', NEW_PASSWORD);
  await page.click('#change-password-form button[type=submit]');
  await page.waitForURL('**/ui/dashboard');

  // --- Dashboard live resize -----------------------------------------------------------------
  await page.waitForSelector('.device-item');
  await page.waitForFunction(() => !document.getElementById('dashboard-grid')?.classList.contains('dashboard-grid--loading'), null, { timeout: 60000 }).catch(() => { });
  const baseCards = await page.locator('.device-item').count();
  check('dashboard renders at least one device card before resizing', baseCards >= 1, String(baseCards));

  for (const [w, h] of STEPS) {
    await page.setViewportSize({ width: w, height: h });
    await sleep(400); // allow the resize-driven reflow (gridstack / container queries) to settle
    const ov = await overflow(page);
    const cards = await page.locator('.device-item').count();
    await page.screenshot({ path: path.join(OUT, `resize-dashboard-${w}-${THEME}.png`), fullPage: false });
    check(`dashboard: no horizontal overflow after live resize to ${w}px (${THEME})`, ov === 0, `overflow=${ov}px`);
    check(`dashboard: the cards survive the live resize to ${w}px (${THEME})`, cards === baseCards, `cards=${cards} base=${baseCards}`);
  }

  // --- Energy page live resize (tabs must stay operable across widths) ------------------------
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');
  const baseTabs = await page.locator('.energy-tabs .nav-link').count();
  for (const [w, h] of STEPS) {
    await page.setViewportSize({ width: w, height: h });
    await sleep(400);
    const ov = await overflow(page);
    const tabs = await page.locator('.energy-tabs .nav-link').count();
    await page.screenshot({ path: path.join(OUT, `resize-energy-${w}-${THEME}.png`), fullPage: false });
    check(`energy: no horizontal overflow after live resize to ${w}px (${THEME})`, ov === 0, `overflow=${ov}px`);
    check(`energy: all tabs survive the live resize to ${w}px (${THEME})`, tabs === baseTabs, `tabs=${tabs} base=${baseTabs}`);
  }

  check('no unexpected console or page errors during the resize session',
    problems.every((p) => /style-src|Content Security Policy|status of 503|Failed to load resource/.test(p)), problems.join(' | '));
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

fs.writeFileSync(path.join(OUT, `resize-${THEME}.json`), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${THEME} ${BROWSER}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
