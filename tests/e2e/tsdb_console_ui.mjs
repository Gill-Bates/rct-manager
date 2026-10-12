//
// tests/e2e/tsdb_console_ui.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused browser verification for the four Status & Log console UX changes (full timestamp,
// per-level colours, terminal look, viewport-height growth). It drives the real admin GUI, feeds
// the real events.on('tsdb.log') -> appendTsdbConsoleLine render path through a mocked SSE stream,
// and reads back computed styles + geometry so the evidence is produced by the shipped code, not by
// a re-implementation. Usage:
//   PLAYWRIGHT_DIR=<dir with node_modules/playwright> PYTHON=<python> \
//     node tests/e2e/tsdb_console_ui.mjs <artifact-dir>
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

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
  viewport: { width: Number(process.env.VIEWPORT_WIDTH || 1280), height: Number(process.env.VIEWPORT_HEIGHT || 1600) }, locale: 'en-GB',
  colorScheme: process.env.COLOR_SCHEME || 'dark',
});
const page = await context.newPage();

// The log lines the console will render, one per level, with ISO-8601 UTC timestamps.
const SAMPLE = [
  { timestamp: '2026-10-11T19:46:12Z', level: 'INFO', logger: 'app.export.pusher', message: 'Export loop started' },
  { timestamp: '2026-10-11T19:46:13Z', level: 'DEBUG', logger: 'app.export.questdb_admin', message: 'Downsampling check skipped' },
  { timestamp: '2026-10-11T19:46:15Z', level: 'WARNING', logger: 'app.export.pusher', message: 'Backend slow, retrying' },
  { timestamp: '2026-10-11T19:46:17Z', level: 'ERROR', logger: 'app.api.app_factory', message: 'Metrics export failed: connection refused' },
];

// A longer burst (after the four level samples) so the console content exceeds its height and we can
// prove internal tailing: 80 extra INFO lines with increasing timestamps.
const BURST = Array.from({ length: 80 }, (_, i) => ({
  timestamp: `2026-10-11T19:${String(47 + Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}Z`,
  level: 'INFO', logger: 'app.export.pusher', message: `tick ${i} metrics flushed`,
}));
const FRAMES = [...SAMPLE, ...BURST];

// Serve a controlled SSE stream for the central dispatcher so the real tsdb.log handler renders our
// sample through appendTsdbConsoleLine. One frame per entry, then the stream is held open.
await context.route('**/admin/api/events', async (route) => {
  const body = FRAMES.map((e) => `event: tsdb.log\ndata: ${JSON.stringify(e)}\n\n`).join('')
    + ': keep-alive\n\n';
  await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }, body });
});

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

// TSDB page: enable export and pick a backend so the Status & Log console renders.
await page.goto(`${base}/ui/tsdb`);
await page.waitForSelector('#settings-sections');
// metrics_export_enabled toggle, then db_type select.
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
// Let the mocked SSE frames arrive and render (four level samples + the 80-line burst).
await page.waitForFunction((n) => document.querySelectorAll('#tsdb-mini-console .tsdb-console-line').length >= n, FRAMES.length, { timeout: 10000 });
await page.waitForTimeout(200);

const report = await page.evaluate(() => {
  const con = document.getElementById('tsdb-mini-console');
  const lines = [...con.querySelectorAll('.tsdb-console-line')];
  const styleOf = (node) => getComputedStyle(node);
  const perLine = lines.map((line) => {
    const time = line.querySelector('.tsdb-console-time');
    const logger = line.querySelector('span[data-role="logger"]');
    return {
      level: line.dataset.level,
      time: time?.textContent,
      timeColor: styleOf(time).color,
      loggerText: logger?.textContent,
      loggerColor: logger ? styleOf(logger).color : null,
      lineColor: styleOf(line).color,
      fontFamily: styleOf(line).fontFamily,
    };
  });
  // Geometry: console bottom vs viewport, and whether the page itself scrolls (double scrollbar).
  const conRect = con.getBoundingClientRect();
  const footer = document.querySelector('.admin-footer');
  const footerRect = footer ? footer.getBoundingClientRect() : null;
  const main = document.getElementById('main-content');
  return {
    perLine,
    consoleBottom: Math.round(conRect.bottom),
    consoleHeight: Math.round(conRect.height),
    viewportHeight: window.innerHeight,
    footerTop: footerRect ? Math.round(footerRect.top) : null,
    // Document scroll: the single most important signal — the page must NOT gain a scrollbar.
    pageScrolls: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
    pageOverflowPx: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    // Console internal scroll + auto-scroll pinning (the 500-line tail behaviour).
    consoleScrollable: con.scrollHeight > con.clientHeight + 1,
    lineCount: lines.length,
    pinnedToBottom: (con.scrollHeight - con.clientHeight - con.scrollTop) <= 24,
    lastMessage: lines[lines.length - 1]?.textContent || '',
    consoleOverflowY: getComputedStyle(con).overflowY,
    // The flex-column content area and whether IT (not the document) scrolls in the deficit regime.
    mainDisplay: getComputedStyle(main).display,
    mainFlexDir: getComputedStyle(main).flexDirection,
    mainScrollable: main.scrollHeight > main.clientHeight + 1,
  };
});
fs.writeFileSync(path.join(OUT, 'tsdb-report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));

// Change 1 — full date+time timestamp (ISO-8601 UTC to seconds) on every line.
const tsRe = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
check('every line shows a full date+time timestamp (YYYY-MM-DDTHH:MM:SSZ)',
  report.perLine.length >= 4 && report.perLine.every((l) => tsRe.test(l.time || '')),
  JSON.stringify(report.perLine.map((l) => l.time)));

// Change 2 — per-level colours and a visually distinct timestamp + logger.
const byLevel = Object.fromEntries(report.perLine.map((l) => [l.level, l]));
const distinctColors = new Set([byLevel.ERROR?.lineColor, byLevel.WARNING?.lineColor, byLevel.INFO?.lineColor].filter(Boolean));
check('ERROR, WARNING and INFO lines use different colours', distinctColors.size === 3,
  JSON.stringify({ error: byLevel.ERROR?.lineColor, warning: byLevel.WARNING?.lineColor, info: byLevel.INFO?.lineColor }));
check('the timestamp colour differs from the message colour (dimmed/accent)',
  report.perLine.every((l) => l.timeColor && l.timeColor !== l.lineColor),
  JSON.stringify(report.perLine.map((l) => [l.level, l.timeColor, l.lineColor])));
check('the logger name is rendered on every line and dimmed (accent colour, distinct from a non-dim message)',
  report.perLine.every((l) => l.loggerText && l.loggerColor)
  // On a dimmed DEBUG line the whole line shares the accent tone; on the others the logger is distinct.
  && report.perLine.filter((l) => l.level !== 'DEBUG').every((l) => l.loggerColor !== l.lineColor),
  JSON.stringify(report.perLine.map((l) => [l.level, l.loggerText, l.loggerColor, l.lineColor])));

// Change 3 — terminal / monospace look.
check('console lines render in a monospace font', report.perLine.every((l) => /mono/i.test(l.fontFamily)),
  report.perLine[0]?.fontFamily);

// Behaviour preserved: with 84 frames the console tails internally and auto-scroll pins to newest.
check('the console scrolls internally and auto-scroll pins to the newest line',
  report.consoleScrollable && report.pinnedToBottom && /tick 79/.test(report.lastMessage),
  JSON.stringify({ consoleScrollable: report.consoleScrollable, pinnedToBottom: report.pinnedToBottom, lastMessage: report.lastMessage, lineCount: report.lineCount }));

// Change 4 — the console fills vertical space down toward the viewport/footer with no double scrollbar.
check('#main-content becomes a flex column so the console can grow',
  report.mainDisplay === 'flex' && report.mainFlexDir === 'column',
  JSON.stringify({ display: report.mainDisplay, dir: report.mainFlexDir }));
// Two layout regimes, following the parameter-table convention:
//  - surplus regime (grid shorter than the viewport): the console expands down toward the footer and
//    the page does not scroll;
//  - deficit regime (the settings cards alone are taller than the viewport): the page scrolls once,
//    like the other admin pages, and the console keeps a usable minimum height.
// Either way there must be exactly one active scrollbar (no page scroll AND console scroll at once).
// The document itself never scrolls: the content area is pinned to the viewport and the scroll lives
// inside it. This is the "no second scrollbar on the page" guarantee.
check('the document body never gains a scrollbar (console/content area scrolls instead)',
  report.pageScrolls === false, JSON.stringify({ pageScrolls: report.pageScrolls }));

const wideDesktop = Number(process.env.VIEWPORT_WIDTH || 1280) >= 992; // the lg breakpoint (sidebar shown)
if (!report.mainScrollable) {
  // Surplus: the settings cards fit, so the console took the leftover height (a real terminal pane,
  // not the old ~88px fixed box) and the console is the single scroll region. On the desktop layout
  // (sidebar visible) the surplus is substantial; narrow/portrait layouts fill whatever is left.
  const minConsole = wideDesktop ? 180 : 110;
  check('the console grows to fill the vertical surplus (terminal pane, within the viewport)',
    report.footerTop !== null && report.consoleHeight >= minConsole && report.consoleBottom <= report.footerTop,
    JSON.stringify({ consoleHeight: report.consoleHeight, consoleBottom: report.consoleBottom, footerTop: report.footerTop, minConsole }));
  check('surplus regime: the console is the only scroll region (content area does not scroll)',
    report.mainScrollable === false && /auto|scroll/.test(report.consoleOverflowY),
    JSON.stringify({ mainScrollable: report.mainScrollable, consoleScrollable: report.consoleScrollable }));
} else {
  // Deficit (the tall settings cards alone exceed the viewport): the content area scrolls once to
  // reveal them (the document body does not), the console keeps a usable minimum height, and the
  // console is overflow:auto. The minimum is 6rem under the 0.9 content zoom (~86px rendered).
  check('deficit regime: the content area scrolls once and the console keeps a usable minimum height',
    report.consoleHeight >= 80 && /auto|scroll/.test(report.consoleOverflowY) && report.pageScrolls === false,
    JSON.stringify({ consoleHeight: report.consoleHeight, mainScrollable: report.mainScrollable, pageScrolls: report.pageScrolls }));
}

await page.screenshot({ path: path.join(OUT, 'tsdb-console.png'), fullPage: false });

// Inverters reference: the console tile must end at the same viewport-bottom boundary as the
// Inverters param-card. Compare the two tiles' bottom edges and the gap each leaves to the footer.
await page.goto(`${base}/ui/inverters`);
await page.waitForSelector('.param-card');
await page.waitForTimeout(300);
const inv = await page.evaluate(() => {
  const card = document.querySelector('.param-card');
  const footer = document.querySelector('.admin-footer');
  return {
    cardBottom: Math.round(card.getBoundingClientRect().bottom),
    footerTop: footer ? Math.round(footer.getBoundingClientRect().top) : null,
    pageScrolls: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1,
  };
});
fs.writeFileSync(path.join(OUT, 'inverters-report.json'), JSON.stringify(inv, null, 2));
await page.screenshot({ path: path.join(OUT, 'inverters-card.png'), fullPage: false });
const consoleGutter = report.footerTop - report.consoleBottom;
const invGutter = inv.footerTop - inv.cardBottom;
console.log(JSON.stringify({ consoleGutter, invGutter, inv }, null, 2));
// Same bottom-boundary BEHAVIOUR as the Inverters param-card: both tiles fill down to the footer
// region and neither makes the document scroll. The console's gutter is larger only by the TSDB-only
// apply bar (#export-actions) and the grid gap that sit below the grid — the Inverters page has no
// such bar — so the extra allowance is bounded (~70px), not an open-ended short box.
// Only meaningful in the surplus regime (the tile reaches the footer region). In the deficit regime
// the content area scrolls, so the console's bottom is measured mid-scroll and the gutter is not a
// like-for-like comparison; the "no page scroll" and "min height" guarantees above cover that case.
if (!report.mainScrollable) {
  check('the console tile ends at the Inverters full-height boundary (fills to the footer region, no page scroll)',
    inv.footerTop !== null && inv.pageScrolls === false && report.pageScrolls === false
    && consoleGutter >= 0 && (consoleGutter - invGutter) <= 80,
    JSON.stringify({ consoleGutter, invGutter, invPageScrolls: inv.pageScrolls, tsdbPageScrolls: report.pageScrolls }));
} else {
  check('deficit layout matches Inverters behaviour (neither the TSDB nor the Inverters page scrolls the document)',
    inv.footerTop !== null && inv.pageScrolls === false && report.pageScrolls === false,
    JSON.stringify({ invPageScrolls: inv.pageScrolls, tsdbPageScrolls: report.pageScrolls }));
}

await browser.close();
await new Promise((resolve) => { server.proc.once('exit', resolve); server.proc.kill('SIGTERM'); fake.proc.kill('SIGTERM'); setTimeout(resolve, 4000); });

fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) { console.error('FAILURES:', JSON.stringify(failed, null, 2)); process.exit(1); }
