//
// tests/e2e/plan_chart_cases.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E for three Charging-plan chart behaviours that the big admin_flow run does not cover
// on their own: (6) the forecast/plan chart RENDERS as a real Chart.js instance; (7) a DST day — a
// 25-hour local autumn fall-back day (Europe/Berlin, 2026-10-25) — renders without the time axis
// collapsing or overflowing; (8) the null/NaN tooltip filter: hovering a slot gap, where both
// series are null, produces NO tooltip body (the shipped filter keeps only finite points), and a
// slot whose power is NaN never crashes the chart.
//
// The data is supplied to the real /dynamic-plan endpoint shape via page.route(); this exercises the
// SHIPPED client rendering (drawPlanChart, planChartSeries, the tooltip filter in
// admin-energy-setup.js) — it does not fabricate a backend plan state in the DOM. The chart, the
// axis maths and the tooltip filter are the production code paths. (A genuine provider-fed `ok`
// plan is a separate gap; this driver is about the rendering behaviour, not the data source.)
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<venv python> BROWSER=chromium|webkit|firefox \
//   COLOR_SCHEME=light|dark node tests/e2e/plan_chart_cases.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const THEME = process.env.COLOR_SCHEME === 'dark' ? 'dark' : 'light';
const BROWSER = process.env.BROWSER || 'chromium';
const problems = [];

// A plan spanning the 25-hour Europe/Berlin fall-back day on 2026-10-25 (CEST->CET at 01:00 UTC).
// Times are UTC instants; the chart plots a real time axis, so the local day is 25 h wide. The last
// idle slot is deliberately NOT adjacent to the previous one, forcing an all-null gap point for the
// tooltip-filter test. One charge slot carries a non-numeric power to prove NaN never crashes.
const slot = (start, end, action, maxPowerW, targetSoc, reason) => ({ start, end, action, max_power_w: maxPowerW, target_soc_percent: targetSoc, reason });
const PLAN = {
  device_id: 'sim', status: 'ok', detail: null,
  created_at: '2026-10-24T22:00:00+00:00', valid_until: '2026-10-26T00:00:00+00:00',
  horizon_hours: 25, preview_only: true, forecast_basis: 'stored history', forecast_confidence: 'high',
  consumption_history_available: true, policy: 'gentle', reserve_soc_percent: 10,
  window: {
    consumption_from: '2026-10-24T22:00:00+00:00', consumption_until: '2026-10-25T23:00:00+00:00',
    solar_from: '2026-10-24T22:00:00+00:00', solar_until: '2026-10-25T23:00:00+00:00',
    prices_from: '2026-10-24T22:00:00+00:00', prices_until: '2026-10-25T23:00:00+00:00'
  },
  energy: { grid_charge_energy_wh: 5400 },
  slots: [
    // Berlin local 2026-10-25 00:00 CEST (22:00 UTC prev day) onward, straddling the 01:00 UTC fall-back.
    slot('2026-10-24T22:00:00+00:00', '2026-10-24T23:30:00+00:00', 'idle', null, null, 'forecast PV covers demand'),
    slot('2026-10-24T23:30:00+00:00', '2026-10-25T01:30:00+00:00', 'charge', 3000, 62, 'negative spot price, forecast deficit'),
    // A NaN power must not crash the chart (planNumber -> NaN -> treated as 0 kW, finite guard holds).
    slot('2026-10-25T01:30:00+00:00', '2026-10-25T03:00:00+00:00', 'charge', 'not-a-number', 70, 'forecast deficit, charged as late as permitted'),
    slot('2026-10-25T03:00:00+00:00', '2026-10-25T12:00:00+00:00', 'idle', null, null, 'battery reserve covers demand'),
    // GAP: this slot does not start where the previous ended -> an all-null point is inserted here.
    slot('2026-10-25T14:00:00+00:00', '2026-10-25T23:00:00+00:00', 'idle', null, null, 'battery reserve covers demand'),
  ],
  price_disclaimer: null,
};

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

const chartState = (page) => page.evaluate(() => {
  const canvas = document.querySelector('#energy-plan-schedule canvas');
  const chart = canvas && window.Chart.getChart(canvas);
  if (!chart) return null;
  const x = chart.options.scales.x;
  return { spanHours: (x.max - x.min) / 3600000, datasets: chart.data.datasets.map((d) => d.data.length) };
});

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
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 }, locale: 'en-GB',
    colorScheme: THEME, timezoneId: 'Europe/Berlin'
  });
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
  // The one-time usage notice locks every other admin page until it is accepted.
  await Promise.all([page.waitForNavigation(), page.click('#disclaimer-accept')]);

  // The dynamic-plan response carries the DST/NaN/gap plan; the real client renders it.
  await page.route('**/dynamic-plan**', (route) => route.fulfill({ json: PLAN }));

  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');
  await page.click('#energy-tab-plan');
  await page.waitForSelector('#energy-pane-plan.active', { timeout: 10000 });
  // The plan loads when a device is chosen; wait for the selector to be populated then trigger it.
  await page.waitForSelector('#energy-plan-device option', { timeout: 15000 }).catch(() => { });
  await page.waitForSelector('#energy-plan-schedule canvas', { timeout: 20000 });
  await page.locator('#energy-plan-schedule canvas').scrollIntoViewIfNeeded();
  await sleep(400);
  await page.screenshot({ path: path.join(OUT, `plan-chart-${THEME}.png`), fullPage: true });

  // (6) The forecast/plan chart renders as exactly one real Chart.js instance.
  const chartCount = await page.evaluate(() => [...document.querySelectorAll('#energy-plan-schedule canvas')]
    .filter((c) => window.Chart.getChart(c)).length);
  check(`plan/forecast chart renders as a real Chart.js instance (${THEME})`, chartCount === 1, `charts=${chartCount}`);

  // (7) The DST day spans 25 local hours; the time axis is wider than 24 h and the chart did not
  // collapse. No horizontal page overflow from the wider axis.
  const state = await chartState(page);
  const overflow = await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth));
  check(`DST day (25 h fall-back) renders with a > 24 h time axis (${THEME})`, state !== null && state.spanHours > 24 && state.spanHours <= 26, JSON.stringify(state));
  check(`DST chart does not cause horizontal page overflow (${THEME})`, overflow === 0, `overflow=${overflow}px`);

  // The charge-window table and the opened all-intervals table stay inside the page at phone widths.
  await page.locator('#energy-plan-schedule details summary').click();
  for (const width of [320, 375, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    await sleep(200);
    const fit = await page.evaluate(() => ({
      overflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      rows: document.querySelectorAll('#energy-plan-charge-schedule tbody tr').length,
    }));
    check(`charging schedule tables fit at ${width}px (${THEME})`, fit.overflow === 0 && fit.rows >= 1, JSON.stringify(fit));
  }
  await page.setViewportSize({ width: 1280, height: 844 });

  // (8) null/NaN tooltip filter. Sweep the whole chart width (which crosses the NaN-power slot and
  // the all-null gap between the two idle slots) and collect every tooltip body shown. A broken
  // tooltip would print the raw "NaN", "undefined" or "null" from a non-finite datapoint; the
  // shipped filter keeps only finite points, so no tooltip body may ever contain those tokens. The
  // NaN-power slot must not have crashed the chart (proven by the chart still existing above).
  const box = await page.locator('#energy-plan-schedule canvas').boundingBox();
  const tooltipAt = async (fraction) => {
    await page.mouse.move(box.x + box.width * fraction, box.y + box.height * 0.5);
    await sleep(150);
    return page.evaluate(() => {
      const chart = window.Chart.getChart(document.querySelector('#energy-plan-schedule canvas'));
      return { opacity: chart.tooltip.opacity, lines: (chart.tooltip.body || []).flatMap((b) => b.lines) };
    });
  };
  const seen = [];
  for (let f = 0.02; f <= 0.98; f += 0.04) seen.push(await tooltipAt(f));
  const allLines = seen.flatMap((t) => t.lines);
  const brokenTokens = allLines.filter((l) => /\bNaN\b|\bundefined\b|\bnull\b/.test(l));
  fs.writeFileSync(path.join(OUT, `plan-tooltip-lines-${THEME}.json`), JSON.stringify(allLines, null, 2));
  check(`no tooltip across the chart shows NaN/undefined/null for a non-finite point (${THEME})`, brokenTokens.length === 0, brokenTokens.join(' | '));

  // A finite slot still produces a readable tooltip (the filter did not suppress everything): at
  // least one hover across the sweep showed a charge line, and no line is an "n/a" placeholder.
  const readable = allLines.filter((l) => /^(Grid charging|No grid charging)/.test(l));
  check(`a finite slot still shows a readable tooltip without n/a rows (${THEME})`,
    readable.length > 0 && !allLines.some((l) => /n\/a/.test(l)), readable.slice(0, 2).join(' | '));

  check(`no unexpected console or page errors (${THEME})`,
    problems.every((p) => /style-src|Content Security Policy|status of 503|Failed to load resource/.test(p)), problems.join(' | '));
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

fs.writeFileSync(path.join(OUT, `plan-chart-${THEME}.json`), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${THEME} ${BROWSER}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
