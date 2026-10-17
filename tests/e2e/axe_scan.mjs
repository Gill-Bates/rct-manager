//
// tests/e2e/axe_scan.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E: automated accessibility scan (axe-core via @axe-core/playwright) of the main admin
// pages — the login page before authentication, the dashboard, and the Energy page's Inverter,
// Charging plan, Spot Market and Weather tabs. Reports every violation with its axe impact
// (critical/serious/moderate/minor), the rule id and the affected node count. The run passes when
// no violation of impact serious or critical is found; moderate/minor violations are reported but
// do not fail, matching how the project already treats advisory a11y findings.
// Usage: PLAYWRIGHT_DIR=<dir containing node_modules/playwright and @axe-core/playwright> \
//   PYTHON=<venv python> BROWSER=chromium COLOR_SCHEME=light|dark node tests/e2e/axe_scan.mjs <artifact-dir>
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const require = createRequire(path.join(process.env.PLAYWRIGHT_DIR, 'noop.js'));
const { AxeBuilder } = require('@axe-core/playwright');

const THEME = process.env.COLOR_SCHEME === 'dark' ? 'dark' : 'light';
const BROWSER = process.env.BROWSER || 'chromium';
const FAIL_IMPACTS = new Set(['serious', 'critical']); // moderate/minor are reported, not failed

const dbDir = fs.mkdtempSync(path.join(OUT, `db-${THEME}-`));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, `fake-${THEME}.log`));
// planning mode: write support off, dispatch/limits store isolated next to the throwaway db.
const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort), 'planning'],
  path.join(OUT, `server-${THEME}.log`));

const firstStartPassword = (output) => {
  const password = /One-time password:\s+(\S+)/.exec(output)?.[1];
  if (!password) throw new Error('first-start password was not printed in the banner');
  return password;
};

// Axe is told which engine ran it; the WCAG 2.x A/AA tags are the project's acceptance scope.
const scan = async (page, label) => {
  const out = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const summary = out.violations.map((v) => ({
    id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help, helpUrl: v.helpUrl,
    targets: v.nodes.map((n) => ({ target: n.target, summary: n.failureSummary }))
  }));
  fs.writeFileSync(path.join(OUT, `axe-${label}-${THEME}.json`), JSON.stringify(summary, null, 2));
  const blocking = summary.filter((v) => FAIL_IMPACTS.has(v.impact));
  const detail = summary.length ? summary.map((v) => `${v.impact}:${v.id}(${v.nodes})`).join(', ') : 'no violations';
  check(`axe ${label} has no serious/critical violation (${THEME})`, blocking.length === 0, detail);
  return summary;
};

const allViolations = {};
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

  // 1. Login page, before any authentication.
  await page.goto(`${base}/login`);
  await page.waitForSelector('#login-form');
  await page.screenshot({ path: path.join(OUT, `axe-login-${THEME}.png`), fullPage: true });
  allViolations.login = await scan(page, 'login');

  // Forced-password-change login flow (same as the other drivers).
  await page.fill('#password', password);
  await page.click('#login-form button[type=submit]');
  await page.waitForURL('**/change-password');
  await page.fill('#current-password', password);
  await page.fill('#new-password', NEW_PASSWORD);
  await page.fill('#confirm-password', NEW_PASSWORD);
  await page.click('#change-password-form button[type=submit]');
  await page.waitForURL('**/ui/dashboard');
  await page.locator('#disclaimer-modal.show').waitFor();
  await Promise.all([page.waitForNavigation(), page.click('#disclaimer-accept')]);

  // 2. Dashboard, including the empty-device state of a fresh admin database.
  await page.waitForSelector('#dashboard-edit-toggle');
  await page.waitForFunction(() => !document.getElementById('dashboard-grid')?.classList.contains('dashboard-grid--loading'), null, { timeout: 60000 }).catch(() => { });
  await page.screenshot({ path: path.join(OUT, `axe-dashboard-${THEME}.png`), fullPage: true });
  allViolations.dashboard = await scan(page, 'dashboard');

  // 3. Energy page tabs. The Inverter tab is active on open; the others are revealed on click.
  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');
  await page.waitForSelector('#energy-list', { timeout: 15000 }).catch(() => { });
  allViolations.energyInverter = await scan(page, 'energy-inverter');

  for (const [tab, pane] of [['spot-market', '#energy-pane-spot-market'], ['weather', '#energy-pane-weather'], ['plan', '#energy-pane-plan']]) {
    await page.click(`#energy-tab-${tab}`);
    await page.waitForSelector(`${pane}.active`, { timeout: 10000 }).catch(() => { });
    await sleep(600); // let the tab's lazy content (preview placeholder / plan controls) attach
    await page.screenshot({ path: path.join(OUT, `axe-energy-${tab}-${THEME}.png`), fullPage: true });
    allViolations[`energy-${tab}`] = await scan(page, `energy-${tab}`);
  }
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

fs.writeFileSync(path.join(OUT, `axe-all-${THEME}.json`), JSON.stringify(allViolations, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${THEME}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
