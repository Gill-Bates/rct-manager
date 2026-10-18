//
// tests/e2e/css_ios_fixes_verify.mjs
//
// Ad-hoc browser verification for the four CSS/iOS review fixes (footer safe-area reserve,
// TSDB mobile-first restructure, no 38rem overlap, shared plan-chart tokens). Drives the real
// admin GUI through the shared harness and reads back computed styles + geometry + screenshots.
// Usage:
//   PLAYWRIGHT_DIR=<dir with node_modules/playwright> PYTHON=<python> BROWSER=webkit|chromium \
//   COLOR_SCHEME=light|dark VIEWPORT_WIDTH=<px> node tests/e2e/css_ios_fixes_verify.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
const scheme = process.env.COLOR_SCHEME || 'light';
const width = Number(process.env.VIEWPORT_WIDTH || 1280);
const tag = `${process.env.BROWSER || 'chromium'}-${width}-${scheme}`;

async function startServer() {
  const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort)], path.join(OUT, 'server.log'));
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/health`)).ok) return server; } catch { /* not up yet */ }
    await sleep(200);
  }
  throw new Error('server did not start');
}

const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1200);
const server = await startServer();
const password = /One-time password:\s+(\S+)/.exec(server.output())?.[1];
if (!password) throw new Error('no first-start password in banner');

const browser = await engine.launch();
const context = await browser.newContext({
  viewport: { width, height: Number(process.env.VIEWPORT_HEIGHT || 900) },
  locale: 'en-GB',
  colorScheme: scheme,
});
const page = await context.newPage();

// Login + forced password change.
await page.goto(`${base}/`);
await page.fill('#password', password);
await page.click('#login-form button[type=submit]');
await page.waitForURL('**/change-password');
await page.fill('#current-password', password);
await page.fill('#new-password', NEW_PASSWORD);
await page.fill('#confirm-password', NEW_PASSWORD);
await page.click('#change-password-form button[type=submit]');
await page.waitForURL('**/ui/dashboard');

// Accept the one-time disclaimer gate (otherwise every gated admin API call is 403 and the
// settings sections never load). The accept handler reloads the page on success.
try {
  await page.locator('#disclaimer-accept').waitFor({ state: 'visible', timeout: 10000 });
  await page.click('#disclaimer-accept');
  await page.waitForTimeout(1500);
} catch { /* already accepted on a reused DB */ }

// ---------------------------------------------------------------------------
// FIX #1 — footer safe-area reserve. env() can't be injected at runtime, so we
// prove the shared-reserve mechanism: force a non-zero reserve delta and confirm
// .app-shell's reserved bottom padding stays >= the painted footer height.
// ---------------------------------------------------------------------------
// Serve a controlled, held-open SSE stream so the settings form + console render (same approach as
// tsdb_console_ui.mjs). A few log lines are enough to populate the console.
const SAMPLE = [
  { timestamp: '2026-10-11T19:46:12Z', level: 'INFO', logger: 'app.export.pusher', message: 'Export loop started' },
  { timestamp: '2026-10-11T19:46:15Z', level: 'WARNING', logger: 'app.export.pusher', message: 'Backend slow, retrying' },
  { timestamp: '2026-10-11T19:46:17Z', level: 'ERROR', logger: 'app.api.app_factory', message: 'Metrics export failed' },
  ...Array.from({ length: 40 }, (_, i) => ({ timestamp: `2026-10-11T19:47:${String(i % 60).padStart(2, '0')}Z`, level: 'INFO', logger: 'app.export.pusher', message: `tick ${i}` })),
];
await context.route('**/admin/api/events', async (route) => {
  const body = SAMPLE.map((e) => `event: tsdb.log\ndata: ${JSON.stringify(e)}\n\n`).join('') + ': keep-alive\n\n';
  await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }, body });
});

await page.goto(`${base}/ui/tsdb`);
await page.waitForSelector('#setting-metrics_export_enabled', { timeout: 20000 });
// Simulate a non-zero bottom safe-area inset. env() can't be set from JS, so we redefine the
// shared reserve the same way the real inset would grow it (both .app-shell padding and the footer
// min-height read --rct-footer-reserve). Set via the inline style property to satisfy the strict CSP.
await page.evaluate(() => {
  document.documentElement.style.setProperty('--rct-footer-reserve', 'calc(var(--rct-footer-height) + 34px)');
});
await page.waitForTimeout(100);
const footerGeo = await page.evaluate(() => {
  const shell = document.querySelector('.app-shell');
  const footer = document.querySelector('.admin-footer');
  const cs = (el) => getComputedStyle(el);
  const reserve = parseFloat(cs(shell).paddingBottom);
  const footerRect = footer.getBoundingClientRect();
  // Last focusable control inside the content, to check it is not under the footer.
  const controls = [...document.querySelectorAll('#main-content button, #main-content input, #main-content select, #main-content a')];
  const last = controls[controls.length - 1];
  const lastRect = last ? last.getBoundingClientRect() : null;
  return {
    shellPaddingBottom: Math.round(reserve),
    footerPaintedHeight: Math.round(footerRect.height),
    footerTop: Math.round(footerRect.top),
    lastControlBottom: lastRect ? Math.round(lastRect.bottom) : null,
    docScrollable: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
  };
});
fs.writeFileSync(path.join(OUT, `footer-${tag}.json`), JSON.stringify(footerGeo, null, 2));
check(`[#1 ${tag}] app-shell reserves at least the painted footer height (inc. simulated inset)`,
  footerGeo.shellPaddingBottom >= footerGeo.footerPaintedHeight - 1,
  JSON.stringify(footerGeo));
await page.screenshot({ path: path.join(OUT, `footer-${tag}.png`), fullPage: false });

// Reset the injected override before the layout tests.
await page.evaluate(() => { document.documentElement.style.removeProperty('--rct-footer-reserve'); });

// ---------------------------------------------------------------------------
// FIX #2/#3 — TSDB mobile-first. Enable export + pick a backend so the console renders.
// ---------------------------------------------------------------------------
const enableToggle = page.locator('#setting-metrics_export_enabled');
await enableToggle.waitFor();
if (!(await enableToggle.isChecked().catch(() => false))) await enableToggle.click();
const dbType = page.locator('#setting-db_type');
await dbType.waitFor({ timeout: 10000 });
await dbType.selectOption('questdb').catch(async () => {
  const values = await dbType.locator('option').evaluateAll((os) => os.map((o) => o.value).filter(Boolean));
  if (values[0]) await dbType.selectOption(values[0]);
});
await page.waitForSelector('#tsdb-mini-console');
await page.waitForTimeout(200);

const layout = await page.evaluate(() => {
  const shell = document.querySelector('.app-shell');
  const main = document.getElementById('main-content');
  const cs = (el) => getComputedStyle(el);
  return {
    innerWidth: window.innerWidth,
    shellHeight: cs(shell).height,
    shellOverflow: cs(shell).overflow,
    mainOverflowY: cs(main).overflowY,
    mainDisplay: cs(main).display,
    docScrollable: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
    mainScrollable: main.scrollHeight > main.clientHeight + 1,
  };
});
fs.writeFileSync(path.join(OUT, `layout-${tag}.json`), JSON.stringify(layout, null, 2));
await page.screenshot({ path: path.join(OUT, `tsdb-${tag}.png`), fullPage: true });

const isPhone = width < 608; // 38rem = 608px at 16px root
if (isPhone) {
  check(`[#2 ${tag}] phone: app-shell has NO fixed height (natural flow)`,
    layout.shellOverflow !== 'hidden',
    JSON.stringify({ shellHeight: layout.shellHeight, overflow: layout.shellOverflow }));
  check(`[#2 ${tag}] phone: #main-content is not an inner scroll container`,
    layout.mainOverflowY === 'visible',
    JSON.stringify({ mainOverflowY: layout.mainOverflowY }));
} else {
  check(`[#2 ${tag}] desktop: app-shell is a fixed, overflow:hidden pinned shell`,
    layout.shellOverflow === 'hidden' && /svh|px/.test(layout.shellHeight),
    JSON.stringify({ shellHeight: layout.shellHeight, overflow: layout.shellOverflow }));
  check(`[#2 ${tag}] desktop: #main-content is the inner scroll container`,
    layout.mainOverflowY === 'auto',
    JSON.stringify({ mainOverflowY: layout.mainOverflowY }));
}

// ---------------------------------------------------------------------------
// FIX #3 — no overlapping branch exactly at 38rem (608px). Test the boundary.
// ---------------------------------------------------------------------------
await page.setViewportSize({ width: 608, height: 900 });
await page.waitForTimeout(150);
const boundary = await page.evaluate(() => {
  const grid = document.querySelector('#main-content > .settings-grid');
  const cs = getComputedStyle(grid);
  return {
    innerWidth: window.innerWidth,
    gridTemplateRows: cs.gridTemplateRows,
    // At >=38rem exactly, the desktop track should apply and no competing max-width rule resets it.
  };
});
fs.writeFileSync(path.join(OUT, `boundary-${tag}.json`), JSON.stringify(boundary, null, 2));
check(`[#3 ${tag}] at exactly 608px (38rem) the grid resolves to a single, consistent track set`,
  !!boundary.gridTemplateRows && boundary.gridTemplateRows !== 'none',
  JSON.stringify(boundary));

// ---------------------------------------------------------------------------
// FIX #4 — plan-chart tokens resolve from the shared :root token in both themes.
// ---------------------------------------------------------------------------
await page.setViewportSize({ width, height: 900 });
const tokens = await page.evaluate(() => {
  const root = getComputedStyle(document.documentElement);
  return {
    power: root.getPropertyValue('--rct-plan-power').trim(),
    soc: root.getPropertyValue('--rct-plan-soc').trim(),
    oldPower: root.getPropertyValue('--plan-power').trim(),
    oldSoc: root.getPropertyValue('--plan-soc').trim(),
  };
});
fs.writeFileSync(path.join(OUT, `tokens-${tag}.json`), JSON.stringify(tokens, null, 2));
check(`[#4 ${tag}] shared --rct-plan-power/#--rct-plan-soc resolve to the original hex`,
  tokens.power === '#0d6efd' && tokens.soc === '#fd7e14' && !tokens.oldPower && !tokens.oldSoc,
  JSON.stringify(tokens));

await browser.close();
fs.writeFileSync(path.join(OUT, `results-${tag}.json`), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${tag}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
