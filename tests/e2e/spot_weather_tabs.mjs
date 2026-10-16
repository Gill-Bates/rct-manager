//
// tests/e2e/spot_weather_tabs.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E: the Spot Market and Weather tabs of the Energy page render their real content in a
// browser (settings form, preview region, status) with no console or page errors. The day-ahead
// price preview and the Open-Meteo solar forecast are fetched from EXTERNAL providers; in this
// isolated environment there is no outbound network, so the live values are not retrievable. This
// driver therefore verifies what IS verifiable offline: the tab structure renders, the form fields
// are present and operable, the preview region shows a defined, non-broken status (e.g. Disabled /
// Unavailable rather than a crash), and switching tabs raises no runtime error. The retrieval of
// real provider values is a network-dependent gap recorded in the report.
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<venv python> BROWSER=chromium|webkit|firefox \
//   COLOR_SCHEME=light|dark node tests/e2e/spot_weather_tabs.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const THEME = process.env.COLOR_SCHEME === 'dark' ? 'dark' : 'light';
const BROWSER = process.env.BROWSER || 'chromium';
const problems = [];

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

  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');

  // --- Spot Market tab ------------------------------------------------------------------------
  await page.click('#energy-tab-spot-market');
  await page.waitForSelector('#energy-pane-spot-market.active', { timeout: 10000 });
  await page.waitForSelector('#spot-market-form', { timeout: 10000 });
  // The form fields are rendered by admin.js; wait until at least one control attaches.
  await page.waitForFunction(() => document.querySelectorAll('#spot-market-fields input, #spot-market-fields select').length > 0, null, { timeout: 10000 }).catch(() => { });
  await sleep(800); // let the preview region resolve its offline status
  await page.screenshot({ path: path.join(OUT, `spot-market-${THEME}.png`), fullPage: true });
  const spot = await page.evaluate(() => ({
    fields: document.querySelectorAll('#spot-market-fields input, #spot-market-fields select').length,
    hasSave: !!document.querySelector('#spot-market-save'),
    previewPresent: !!document.querySelector('#spot-market-preview'),
    status: document.querySelector('#spot-market-preview-status')?.textContent.trim() || '',
    previewText: (document.querySelector('#spot-market-preview')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
  }));
  fs.writeFileSync(path.join(OUT, `spot-market-${THEME}.json`), JSON.stringify(spot, null, 2));
  check(`Spot Market tab renders its settings form and preview region (${THEME})`, spot.fields > 0 && spot.hasSave && spot.previewPresent, JSON.stringify(spot));
  check(`Spot Market preview shows a defined, non-broken state (${THEME})`,
    !/undefined|NaN|\[object/.test(spot.previewText) && (spot.status.length > 0 || spot.previewText.length > 0), JSON.stringify({ status: spot.status, previewText: spot.previewText }));

  // --- Weather tab ----------------------------------------------------------------------------
  await page.click('#energy-tab-weather');
  await page.waitForSelector('#energy-pane-weather.active', { timeout: 10000 });
  await page.waitForFunction(() => document.querySelectorAll('#energy-pane-weather input, #energy-pane-weather select, #energy-pane-weather button').length > 0, null, { timeout: 10000 }).catch(() => { });
  await sleep(800);
  await page.screenshot({ path: path.join(OUT, `weather-${THEME}.png`), fullPage: true });
  const weather = await page.evaluate(() => ({
    controls: document.querySelectorAll('#energy-pane-weather input, #energy-pane-weather select, #energy-pane-weather button').length,
    previewPresent: !!document.querySelector('#weather-preview'),
    status: document.querySelector('#weather-preview-status')?.textContent.trim() || '',
    previewText: (document.querySelector('#weather-preview')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
  }));
  fs.writeFileSync(path.join(OUT, `weather-${THEME}.json`), JSON.stringify(weather, null, 2));
  check(`Weather tab renders its settings controls and preview region (${THEME})`, weather.controls > 0 && weather.previewPresent, JSON.stringify(weather));
  check(`Weather preview shows a defined, non-broken state (${THEME})`,
    !/undefined|NaN|\[object/.test(weather.previewText) && (weather.status.length > 0 || weather.previewText.length > 0), JSON.stringify({ status: weather.status, previewText: weather.previewText }));

  check(`switching between Inverter, Spot Market and Weather raised no page error (${THEME})`,
    !problems.some((p) => p.startsWith('pageerror:')), problems.filter((p) => p.startsWith('pageerror:')).join(' | '));
  check(`no unexpected console errors on the data-source tabs (${THEME})`,
    problems.every((p) => /style-src|Content Security Policy|status of 503|Failed to load resource|access control checks/.test(p)), problems.join(' | '));
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

fs.writeFileSync(path.join(OUT, `spot-weather-${THEME}.json`), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${THEME} ${BROWSER}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
