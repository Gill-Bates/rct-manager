//
// tests/e2e/admin_flow.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E for the administration GUI against an isolated database and a simulated inverter.
// Usage: PLAYWRIGHT_DIR=<dir containing node_modules/playwright> node tests/e2e/admin_flow.mjs <artifact-dir>
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
const results = [];
const problems = [];

const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
// Remove only errors produced by the current deliberate fault, never earlier failures.
const scrubExpected = (start, matches, sink = problems) => {
  const kept = sink.slice(start).filter((problem) => !matches(problem));
  sink.splice(start, sink.length - start, ...kept);
};
// One collector for every page: console errors/warnings, script errors, failed requests, HTTP >= 400.
function trackProblems(pg, sink) {
  pg.on('console', (m) => { if (['error', 'warning'].includes(m.type())) sink.push(`console ${m.type()}: ${m.text()} @ ${m.location().url}`); });
  pg.on('pageerror', (e) => sink.push(`pageerror: ${e.message}`));
  // WebKit reports in-flight loads dropped by a navigation as failed; Chromium does not, so real aborts still show.
  pg.on('requestfailed', (r) => { if (r.failure()?.errorText === 'Load request cancelled') return; sink.push(`requestfailed: ${r.method()} ${r.url()} (${r.failure()?.errorText || 'unknown'})`); });
  pg.on('response', (r) => { if (r.status() >= 400) sink.push(`http ${r.status()}: ${r.request().method()} ${r.url()}`); });
}
// A deliberate HTTP error shows up twice: as the response and as the browser's console line.
const expectedStatus = (url, ...statuses) => (p) => statuses.some((st) =>
  (p.startsWith(`http ${st}: `) || (p.startsWith('console ') && p.includes(`status of ${st}`))) && p.includes(url));
const waitForPort = async (port) => {
  for (let i = 0; i < 50; i++) {
    const up = await new Promise((resolve) => { const c = net.connect(port, '127.0.0.1', () => { c.destroy(); resolve(true); }); c.on('error', () => resolve(false)); });
    if (up) return;
    await sleep(100);
  }
  throw new Error(`nothing listens on port ${port}`);
};
const expectedRestartProblem = (problem, baseUrl) => problem.includes(`${baseUrl}/health`) && (
  /^requestfailed: GET .*ERR_CONNECTION_REFUSED/.test(problem)
  || /^console error: Failed to load resource: net::ERR_CONNECTION_REFUSED/.test(problem)
  || problem.startsWith('http 503: GET ')
  || /^console error: Failed to load resource: the server responded with a status of 503/.test(problem)
);
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

async function startServer(db, httpPort, devicePort, tag) {
  const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort)], path.join(OUT, `server-${tag}.log`));
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`http://127.0.0.1:${httpPort}/health`)).ok) return server; } catch { /* not up yet */ }
    await sleep(200);
  }
  throw new Error('server did not start');
}
async function stop(server) {
  const proc = server.proc;
  if (proc.exitCode !== null || proc.signalCode !== null) return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`server process ${proc.pid} did not stop within 5 seconds`));
    }, 5000);
    proc.once('exit', () => { clearTimeout(timeout); resolve(); });
    proc.kill('SIGTERM');
  });
}

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const db = path.join(dbDir, 'e2e.db');
function firstStartPassword(output) {
  const announced = /Password file:\s+(\S+)\s+\(0600\)/.exec(output)?.[1];
  if (!announced) throw new Error('first-start password file was not announced');
  const file = path.resolve(announced);
  if (!file.startsWith(dbDir + path.sep) || path.basename(file) !== 'initial-admin-password') {
    throw new Error('first-start password file is outside the isolated test database');
  }
  return fs.readFileSync(file, 'utf8').trim();
}
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1500);
let server = await startServer(db, httpPort, devicePort, 'first');
// The banner names a private password file; the test reads only its own isolated fixture.
const initial = firstStartPassword(server.output());
check('initial password read from the private file', Boolean(initial));
check('no admin token printed at first start', !/Initial admin token/.test(server.output()));

const browser = await engine.launch();
const context = await browser.newContext({
  viewport: { width: Number(process.env.VIEWPORT_WIDTH || 1280), height: 800 }, locale: 'en-GB',
  colorScheme: process.env.COLOR_SCHEME || 'light',
});
const page = await context.newPage();
trackProblems(page, problems);
const external = new Set();
page.on('request', (r) => { if (!r.url().startsWith(base) && !r.url().startsWith('data:')) external.add(r.url()); });
// confirm() answers are scripted: the device Apply asks before re-addressing or removing an inverter.
const dialogs = [];
let dialogAnswer = true;
page.on('dialog', (d) => { dialogs.push(d.message()); return dialogAnswer ? d.accept() : d.dismiss(); });
// Every request that sends the device list; the Apply-only contract is asserted against this.
const devicePuts = [];
page.on('request', (r) => {
  if (r.method() !== 'PUT' || !r.url().endsWith('/admin/api/settings')) return;
  try { const body = JSON.parse(r.postData() || '{}'); if (Object.hasOwn(body, 'devices')) devicePuts.push(body); } catch { /* not JSON */ }
});
const shot = (name) => page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
const toastText = async () => (await page.locator('#toast-region').innerText()).trim();
const overflow = async () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

// 1. root -> login -> forced password change -> dashboard
await page.goto(base + '/');
check('/ redirects to /login', page.url().endsWith('/login'));
await shot('01-login');
await page.fill('#password', initial);
await page.click('#login-form button[type=submit]');
await page.waitForURL('**/change-password');
check('first login forces password change', true);
await page.goto(base + '/ui/dashboard');
check('dashboard blocked while change is pending', page.url().endsWith('/change-password'));
await shot('02-change-password');
await page.fill('#current-password', initial);
await page.fill('#new-password', NEW_PASSWORD);
await page.fill('#confirm-password', NEW_PASSWORD);
await page.click('#change-password-form button[type=submit]');
await page.waitForURL('**/ui/dashboard');
check('password change lands on dashboard', true);

// 2. dashboard with cached values only
await page.waitForFunction(() => document.querySelector('.device-item .status-badge-success, .device-item .status-dot.online'), null, { timeout: 20000 }).catch(() => { });
const badgeInfo = await page.evaluate(() => {
  const card = document.querySelector('.device-item');
  const badge = card?.querySelector('.status-badge-success');
  if (!badge) return null;
  const cardRect = card.getBoundingClientRect();
  const badgeRect = badge.getBoundingClientRect();
  return {
    text: badge.textContent.trim(),
    nearTopRight: badgeRect.top - cardRect.top < 40 && cardRect.right - badgeRect.right < 40,
    hasDot: getComputedStyle(badge, '::before').content !== 'none',
  };
});
check('connected device shows a top-right status badge with a dot', Boolean(badgeInfo && badgeInfo.text === 'Connected' && badgeInfo.nearTopRight && badgeInfo.hasDot), JSON.stringify(badgeInfo));

try {
  await page.waitForFunction(() => document.getElementById('grid-power')?.textContent.includes('250') && document.getElementById('pv-power')?.textContent.includes('2,035'), null, { timeout: 40000 });
} catch { /* reported below */ }
const kpi = Object.fromEntries(await Promise.all(['pv-power', 'grid-power', 'battery-soc', 'device-count', 'connected-count'].map(async (id) => [id, (await page.locator('#' + id).innerText()).trim()])));
console.log(JSON.stringify(kpi));
// Dashboard values are whole numbers; the separator follows the browser locale (en-GB context here).
check('PV value from cache (1234.5 + 800 W) is rounded to a whole number', /^2,035\s*W$/.test(kpi['pv-power']), kpi['pv-power']);
const gridTile = await page.evaluate(() => ({
  text: document.getElementById('grid-power').textContent.trim(),
  label: document.querySelector('#grid-power .device-flow')?.getAttribute('aria-label'),
  card: document.querySelector('.device-item .device-flow')?.getAttribute('aria-label'),
  cardText: [...document.querySelectorAll('.device-item dt')].find((dt) => dt.textContent === 'Grid power')?.nextElementSibling.textContent.trim(),
}));
check('grid value (-250 W feed-in) is shown as magnitude with a feed-in indicator',
  /^arrow_upward\s*250\s*W$/.test(gridTile.text) && !/[-−]/.test(gridTile.text) && gridTile.label === 'Feeding into the grid'
  && gridTile.card === 'Feeding into the grid' && /^arrow_upward\s*250\s*W$/.test(gridTile.cardText), JSON.stringify(gridTile));
check('battery ratio 0.55 shown as 55 %', /^55\s*%/.test(kpi['battery-soc']), kpi['battery-soc']);

// Dashboard flow graphic (stage 1): fed from device.energy_flow on /admin/api/devices, no second
// energy/devices fetch, present in every card above the inverter/battery subcards, and animated
// because the simulator's PV/grid/battery readings above are all non-zero.
await page.waitForSelector('.device-item .device-flow-graphic:not([hidden]) .energy-flow-svg');
const flowInfo = await page.evaluate(() => {
  const card = document.querySelector('.device-item');
  const flowBox = card.querySelector('.device-flow-graphic');
  const cards = card.querySelector('.device-visual-cards');
  return {
    present: Boolean(flowBox?.querySelector('.energy-flow-svg')),
    hidden: flowBox?.hidden,
    aboveCards: Boolean(flowBox && cards && flowBox.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING),
    activeLines: [...flowBox.querySelectorAll('.flow-line')].filter((line) => !line.classList.contains('is-idle')).length,
  };
});
check('dashboard device card shows the flow graphic above the inverter/battery subcards',
  flowInfo.present && !flowInfo.hidden && flowInfo.aboveCards, JSON.stringify(flowInfo));
check('the dashboard flow graphic animates from non-zero energy_flow data', flowInfo.activeLines > 0, JSON.stringify(flowInfo));
// Label and animation must agree: a Charging/Discharging/Import/Export label means an active line.
const flowConsistency = await page.evaluate(() => {
  const svg = document.querySelector('.device-item .energy-flow-svg');
  const lines = [...svg.querySelectorAll('.flow-line')];
  const moving = (index) => !lines[index].classList.contains('is-idle');
  const badge = (name) => [...document.querySelectorAll('.device-item .device-flow-graphic .flow-badge')]
    .find((b) => b.querySelector('.flow-badge-caption').textContent === name)?.querySelector('.flow-badge-state').textContent;
  const batteryWord = badge('Battery');
  const gridWord = badge('Grid');
  return {
    battery: { word: batteryWord, moving: moving(2), charge: lines[2].classList.contains('flow-charge'), discharge: lines[2].classList.contains('flow-discharge') },
    grid: { word: gridWord, moving: moving(1) },
  };
});
const batteryActive = ['Charging', 'Discharging'].includes(flowConsistency.battery.word);
check('battery line animates exactly when the battery label says Charging/Discharging, with matching direction',
  flowConsistency.battery.moving === batteryActive
  && (flowConsistency.battery.word !== 'Charging' || flowConsistency.battery.charge)
  && (flowConsistency.battery.word !== 'Discharging' || flowConsistency.battery.discharge), JSON.stringify(flowConsistency));
check('grid line animates exactly when the grid label says Import/Export',
  flowConsistency.grid.moving === ['Import', 'Feed-in'].includes(flowConsistency.grid.word), JSON.stringify(flowConsistency));
// Status badges under the graphic must agree with the node labels (same readings and threshold).
const statusBadges = await page.evaluate(() => {
  const badges = [...document.querySelectorAll('.device-item .device-flow-graphic .flow-badge:not([hidden])')];
  return {
    count: badges.length,
    states: badges.map((b) => b.querySelector('.flow-badge-state').textContent),
    colored: badges.every((b) => b.querySelector('.flow-badge-state').matches('.status-badge-success, .status-badge-neutral')),
  };
});
check('dashboard flow graphic shows status badges, each success or neutral', statusBadges.count > 0 && statusBadges.colored, JSON.stringify(statusBadges));
// Final colour semantics: PV/export/discharge green, import red/orange, charge blue/neutral.
// Probed on disposable elements, not the live graphic — the simulator's battery leg can be idle,
// so flow-charge/flow-discharge may not be applied to any current DOM node.
const flowColors = await page.evaluate(() => {
  const read = (cls) => {
    const probe = document.createElement('div');
    probe.className = `flow-line ${cls}`;
    document.body.append(probe);
    const value = getComputedStyle(probe).getPropertyValue('--flow-color').trim();
    probe.remove();
    return value;
  };
  return { pv: read('flow-pv'), discharge: read('flow-discharge'), import: read('flow-import'), charge: read('flow-charge') };
});
check('discharge and PV/export share the same (green) flow colour', flowColors.discharge === flowColors.pv, JSON.stringify(flowColors));
check('import and charge use distinct, non-green flow colours', flowColors.import !== flowColors.pv && flowColors.charge !== flowColors.pv && flowColors.import !== flowColors.charge, JSON.stringify(flowColors));
const dashboardNetworkLegs = [];
page.on('request', (r) => { if (r.url().includes('/admin/api/energy/devices')) dashboardNetworkLegs.push(r.url()); });
await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
await page.waitForTimeout(300);
check('the dashboard poll makes no energy/devices request', dashboardNetworkLegs.length === 0, dashboardNetworkLegs.join(','));

// A stored layout with overlapping boxes (tiles at y=3 under the inverter widget at y=4) must reload
// with the tiles where they were placed and the inverter widget moved below them, never the reverse.
const overlapping = [
  ['device-count', 0, 0, 2, 2], ['connected-count', 2, 0, 2, 2], ['pv-power', 4, 0, 2, 2], ['metric-count', 6, 0, 2, 2],
  ['grid-power', 8, 0, 2, 2], ['tsdb-status', 10, 0, 2, 2], ['battery-soc', 0, 3, 2, 2], ['house-power', 2, 3, 2, 2], ['devices', 0, 4, 12, 8],
].map(([id, x, y, w, h]) => ({ id, x, y, w, h, visible: true }));
await page.evaluate((widgets) => window.RCTAdmin.api('dashboard-layout', { method: 'PUT', body: JSON.stringify({ version: 1, widgets }) }), overlapping);
await page.reload();
const layoutWidgets = ['battery-soc', 'house-power', 'devices'];
await page.waitForFunction((ids) => ids.every((id) =>
  document.querySelector(`#dashboard-grid .grid-stack-item[data-widget-id="${id}"]`)?.gridstackNode), layoutWidgets);
const reloadedGrid = await page.evaluate((ids) => Object.fromEntries(ids.map((id) => {
  const node = document.querySelector(`#dashboard-grid .grid-stack-item[data-widget-id="${id}"]`).gridstackNode;
  return [id, { x: node.x, y: node.y, h: node.h }];
})), layoutWidgets);
check('reload keeps the Battery level and House consumption tiles above the inverter widget',
  ['battery-soc', 'house-power'].every((id) => reloadedGrid[id].y + reloadedGrid[id].h <= reloadedGrid.devices.y)
  && reloadedGrid['battery-soc'].x === 0 && reloadedGrid['house-power'].x === 2, JSON.stringify(reloadedGrid));
await page.evaluate(() => window.RCTAdmin.api('dashboard-layout', { method: 'DELETE' }));
await page.reload();

// The inverter image sits beside the power card's readings; the battery tower is now a stack of
// top + N middle + bottom slices (app/admin/static/img/battery_{top,middle,bottom}.svg) beside the
// battery card's readings, so the device no longer carries exactly 2 images.
await page.waitForFunction(() => {
  const images = [...document.querySelectorAll('.device-visual img')];
  return images.length >= 3 && images.every((image) => image.complete);
});
const deviceImages = await page.locator('.device-visual img').evaluateAll((images) => images.map((image) => ({
  src: new URL(image.src).pathname,
  alt: image.alt,
  loaded: image.complete && image.naturalWidth > 0,
  leftOfReadings: image.closest('.device-subcard-body').lastElementChild.getBoundingClientRect().left
    >= image.getBoundingClientRect().right,
})));
const inverterImages = deviceImages.filter((image) => image.src === '/admin/static/img/rct-inverter.svg');
const batterySliceImages = deviceImages.filter((image) => /\/admin\/static\/img\/battery_(top|middle|bottom)\.svg$/.test(image.src));
// The simulated device now carries two battery towers (tests/e2e/fake_inverter.py: primary +
// battery_placeholder_0), so there are two independent top+middle(s)+bottom stacks, each with
// its own top/bottom slice, not globally exactly one of each.
const towerCount = await page.locator('.device-subcard-battery').count();
check('device card shows the inverter SVG and one battery slice stack per tower, all left of their readings',
  inverterImages.length === 1
  && batterySliceImages.length >= 2 * towerCount  // at least top + bottom per tower
  && batterySliceImages.filter((image) => image.src.endsWith('battery_top.svg')).length === towerCount
  && batterySliceImages.filter((image) => image.src.endsWith('battery_bottom.svg')).length === towerCount
  && deviceImages.every((image) => image.alt === '' && image.loaded && image.leftOfReadings),
  JSON.stringify({ towerCount, deviceImages }));
// DOM order within the stack must be top, then middles, then bottom (visual stack order top-down).
const stackOrder = await page.locator('.device-battery-stack').first().evaluate((stack) =>
  [...stack.querySelectorAll('img')].map((image) => new URL(image.src).pathname.split('/').pop()));
check('battery slice stack is ordered top, middle(s), bottom',
  stackOrder[0] === 'battery_top.svg' && stackOrder[stackOrder.length - 1] === 'battery_bottom.svg'
  && stackOrder.slice(1, -1).every((name) => name === 'battery_middle.svg'),
  JSON.stringify(stackOrder));
await page.waitForFunction(() => [...document.querySelectorAll('.device-subcard-head .status-badge')].some((b) => b.textContent.trim().length > 0), null, { timeout: 20000 }).catch(() => { });
const badgeTexts = await page.$$eval('.device-subcard-head .status-badge', (list) => list.map((b) => b.textContent.trim()));
check('inverter status badge is fully readable (feed_in test value)', badgeTexts.some((t) => /feed in/i.test(t)), JSON.stringify(badgeTexts));
check('battery status badge is fully readable (balancing active test value)', badgeTexts.some((t) => /balancing active/i.test(t)), JSON.stringify(badgeTexts));
// Every status badge shares one component: identical metrics, only the tone colours differ, and each
// tone keeps WCAG AA text contrast (4.5:1) against the badge background composited on the page.
const badgeMetrics = await page.evaluate(() => {
  const parse = (c) => (c.match(/[\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => { const v = [r, g, b].map((x) => { x /= 255; return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; }); return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
  const under = (n) => { for (let e = n.parentElement; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.length < 4 || c[3] > 0) return c; } return [255, 255, 255, 1]; };
  return [...document.querySelectorAll('.device-item .status-badge')].map((n) => {
    const cs = getComputedStyle(n), dot = getComputedStyle(n, '::before');
    const fg = parse(cs.color), bg = parse(cs.backgroundColor), page = under(n);
    const a = bg.length > 3 ? bg[3] : 1;
    const mix = [0, 1, 2].map((k) => bg[k] * a + page[k] * (1 - a));
    const [hi, lo] = [lum(fg), lum(mix)].sort((x, y) => y - x);
    return {
      text: n.textContent.trim(), size: [cs.fontSize, cs.fontWeight, cs.padding, cs.borderRadius, cs.lineHeight, dot.width, dot.height].join('|'),
      height: Math.round(n.getBoundingClientRect().height * 10) / 10, opacity: getComputedStyle(n.closest('.flow-badge') || n).opacity, contrast: (hi + .05) / (lo + .05)
    };
  });
});
check('status badges share size, padding, radius, typography and dot (colour aside)',
  badgeMetrics.length >= 3 && new Set(badgeMetrics.map((b) => b.size)).size === 1 && new Set(badgeMetrics.map((b) => b.height)).size === 1,
  JSON.stringify(badgeMetrics.map((b) => [b.text, b.size, b.height])));
check('status badges are never dimmed and keep text contrast of at least 4.5:1',
  badgeMetrics.every((b) => b.opacity === '1' && b.contrast >= 4.5), JSON.stringify(badgeMetrics.map((b) => [b.text, b.opacity, +b.contrast.toFixed(2)])));
// Forced stale readings (route rewrite) must not dim or flicker the flow badges across several polls.
await page.route('**/admin/api/devices*', async (route) => {
  const response = await route.fetch();
  const mark = (node) => { if (node && typeof node === 'object') { if ('stale' in node) node.stale = true; Object.values(node).forEach(mark); } return node; };
  await route.fulfill({ response, json: mark(await response.json()) });
});
const staleSeen = new Set();
for (let i = 0; i < 90; i++) {
  (await page.$$eval('.device-item .flow-badge:not([hidden])', (list) => list.map((n) => getComputedStyle(n).opacity + '|' + getComputedStyle(n.querySelector('.flow-badge-state')).color)))
    .forEach((v) => staleSeen.add(v));
  await page.waitForTimeout(250);
}
await page.unroute('**/admin/api/devices*');
check('flow badges keep full opacity and one colour per badge while readings are forced stale',
  staleSeen.size > 0 && [...staleSeen].every((v) => v.startsWith('1|')) && staleSeen.size <= 4, JSON.stringify([...staleSeen]));
check('no external requests', external.size === 0, [...external].join(','));

// Two simulated battery towers (primary + battery_placeholder_0, tests/e2e/fake_inverter.py) must
// render as two distinct battery subcards with their own soc/temperature, not two copies of the
// same reading (the duplicate-values regression this harness exists to catch).
const towerInfo = await page.evaluate(() => {
  const batteries = [...document.querySelectorAll('.device-subcard-battery')];
  return batteries.map((card) => ({
    charge: card.querySelector('.device-charge-value')?.textContent.trim(),
    temperature: [...card.querySelectorAll('.device-metric-card')]
      .find((cell) => cell.querySelector('dt')?.textContent.trim() === 'Temperature')
      ?.querySelector('dd')?.textContent.trim(),
    middleSlices: card.querySelectorAll('.device-battery-stack img[src$="battery_middle.svg"]').length,
  }));
});
check('two simulated battery towers render as two distinct subcards with their own readings',
  towerInfo.length === 2 && towerInfo[0].charge !== towerInfo[1].charge
  && towerInfo[0].temperature !== towerInfo[1].temperature
  && towerInfo[0].middleSlices !== towerInfo[1].middleSlices,
  JSON.stringify(towerInfo));

let staleReadings = 0;
await page.route('**/admin/api/devices', async (route) => {
  const response = await route.fetch();
  const data = await response.json();
  for (const device of data.devices) {
    for (const metric of device.metrics) {
      metric.stale = true;
      staleReadings += 1;
    }
  }
  await route.fulfill({ response, json: data });
});
await page.reload();
await page.waitForFunction(() => document.getElementById('grid-power')?.textContent.includes('250')
  && [...document.querySelectorAll('.device-subcard-head .status-badge')].some((badge) => badge.textContent.trim().length > 0));
const staleUi = await page.locator('#dashboard-grid, .device-visual').evaluateAll((nodes) =>
  nodes.map((node) => `${node.textContent} ${[...node.querySelectorAll('[title]')].map((child) => child.title).join(' ')}`).join(' '));
check('stale API readings display values without stale labels or titles', staleReadings > 0 && !/\bstale\b/i.test(staleUi), `stale readings: ${staleReadings}; UI: ${staleUi}`);
await page.unroute('**/admin/api/devices');

// 2a. Device metadata stays in the header; the readings live in one power subcard plus one subcard
// per battery tower. The previous .device-half-* / .device-divider / .device-metrics structure no
// longer exists in the DOM, so the checks below are written against the current
// .device-subcard-power / .device-subcard-battery layout - querying the old classes returned null
// and asserted nothing at all.
// Module detection needs several consecutive stable reads (the pending state renders no tower), so
// the second tower appears a few polls after the first paint; wait for it instead of sampling once.
await page.waitForFunction(() => document.querySelectorAll('.device-subcard-battery').length >= 2, null, { timeout: 30000 }).catch(() => { /* reported by the checks below */ });
const readLayout = () => page.evaluate(() => {
  const text = (node) => (node ? node.textContent.trim() : null);
  const box = (node) => {
    const r = node.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: Math.round(r.width), height: Math.round(r.height) };
  };
  const labels = (node) => [...node.querySelectorAll('dt')].map((dt) => dt.textContent.trim());
  const title = (card) => text(card.querySelector('.device-subcard-title span:last-of-type'));
  const chip = (card) => {
    const node = card.querySelector('.device-subcard-head .status-badge');
    return node && !node.hidden ? text(node) : null;
  };
  const card = document.querySelector('.device-item');
  const visual = card.querySelector('.device-visual');
  const power = card.querySelector('.device-subcard-power');
  const powerImage = power.querySelector('.device-subcard-image');
  const readTower = (tower) => {
    const stack = tower.querySelector('.device-battery-stack');
    const slices = [...stack.querySelectorAll('img')];
    return {
      title: title(tower), chip: chip(tower),
      charge: text(tower.querySelector('.device-charge-value')),
      labels: labels(tower),
      cells: tower.querySelectorAll('.device-metric-card').length,
      middles: slices.filter((image) => image.src.endsWith('battery_middle.svg')).length,
      slices: slices.length,
      stack: box(stack),
      // Seams: consecutive slices must touch exactly, with no transparent strip between them.
      seams: slices.slice(1).map((image, index) => Math.round((image.getBoundingClientRect().top
        - slices[index].getBoundingClientRect().bottom) * 100) / 100),
      sliceWidths: [...new Set(slices.map((image) => Math.round(image.getBoundingClientRect().width)))],
      // One shadow for the whole tower, none on the individual slices.
      stackShadow: getComputedStyle(stack).filter,
      noteHidden: tower.querySelector('.device-battery-note').hidden,
      noteText: text(tower.querySelector('.device-battery-note')),
      box: box(tower),
      bodyTracks: getComputedStyle(tower.querySelector('.device-subcard-body')).gridTemplateColumns
        .split(' ').map((track) => Math.round(parseFloat(track))),
    };
  };
  return {
    visualColumns: getComputedStyle(visual).gridTemplateColumns.split(' ').length,
    batteryCountVar: visual.style.getPropertyValue('--battery-count').trim(),
    headHeight: Math.round(card.querySelector('.device-head').getBoundingClientRect().height),
    power: {
      title: title(power), chip: chip(power), labels: labels(power),
      cells: power.querySelectorAll('.device-metric-card').length,
      columns: getComputedStyle(power.querySelector('.device-metric-grid')).gridTemplateColumns.split(' ').length,
      image: box(powerImage),
      bodyTracks: getComputedStyle(power.querySelector('.device-subcard-body')).gridTemplateColumns
        .split(' ').map((track) => Math.round(parseFloat(track))),
      box: box(power),
      valueSize: parseFloat(getComputedStyle(power.querySelector('.device-metric-card dd')).fontSize),
    },
    towers: [...card.querySelectorAll('.device-subcard-battery')].map(readTower),
    chargeSize: parseFloat(getComputedStyle(card.querySelector('.device-charge-value')).fontSize),
  };
});
const layout = await readLayout();
console.log(JSON.stringify(layout, null, 1));
check('card header is compact', layout.headHeight <= 60, JSON.stringify({ headHeight: layout.headHeight }));
check('the power subcard is labelled and carries the inverter facts in two columns',
  layout.power.title === 'Inverter / Power' && layout.power.columns === 2
  && ['PV A', 'PV B', 'Grid power', 'AC power'].every((label) => layout.power.labels.includes(label)),
  JSON.stringify(layout.power.labels));
check('the inverter status is shown on the power subcard chip', /feed in/i.test(layout.power.chip || ''), String(layout.power.chip));
// ITEM 3/4: the simulated device has two towers (5 and 4 modules) with deliberately different SoC
// and status values, so shared-metric rendering would be visible as two identical towers.
check('one subcard per battery tower, titled Battery 1 and Battery 2',
  layout.towers.length === 2 && layout.towers.map((tower) => tower.title).join(',') === 'Battery 1,Battery 2'
  && layout.towers.length === 2,
  JSON.stringify({ titles: layout.towers.map((t) => t.title) }));
check('each tower shows its own charge level (55 % and 42 %), not a shared one',
  /^55\s*%$/.test(layout.towers[0].charge) && /^42\s*%$/.test(layout.towers[1].charge),
  JSON.stringify(layout.towers.map((tower) => tower.charge)));
check('each tower shows its own status (balancing active vs synchronizing), not a shared one',
  /balancing active/i.test(layout.towers[0].chip || '') && /synchroniz/i.test(layout.towers[1].chip || '')
  && layout.towers[0].chip !== layout.towers[1].chip,
  JSON.stringify(layout.towers.map((tower) => tower.chip)));
check('each tower renders its own module count (5 and 4 middle slices)',
  layout.towers[0].middles === 5 && layout.towers[1].middles === 4
  && layout.towers.every((tower) => tower.slices === tower.middles + 2),
  JSON.stringify(layout.towers.map((tower) => ({ middles: tower.middles, slices: tower.slices }))));
// ITEM 3, device-wide values: battery_cycles, battery_soc_target and power_mng_bat_next_calib_date
// have no battery_placeholder_0_* equivalent in the catalog, so they are shown once (first tower)
// instead of being duplicated or invented for the second.
check('temperature is per tower while device-wide battery facts appear only once',
  layout.towers.every((tower) => tower.labels.includes('Temperature'))
  && ['Charge cycles', 'SOC target'].every((label) => layout.towers[0].labels.includes(label))
  && !['Charge cycles', 'SOC target', 'Next calibration'].some((label) => layout.towers[1].labels.includes(label)),
  JSON.stringify(layout.towers.map((tower) => tower.labels)));
// ITEM 1: the slices are flush (no transparent trailing space inside them, so no gap at gap:0) and
// the shadow sits on the assembled tower, not on every module.
check('battery slices butt together with no visible seam',
  layout.towers.every((tower) => tower.seams.every((seam) => Math.abs(seam) <= 0.5)),
  JSON.stringify(layout.towers.map((tower) => tower.seams)));
check('all slices of a tower share one width', layout.towers.every((tower) => tower.sliceWidths.length === 1),
  JSON.stringify(layout.towers.map((tower) => tower.sliceWidths)));
check('one drop shadow is applied to the assembled tower',
  layout.towers.every((tower) => /drop-shadow/.test(tower.stackShadow)),
  JSON.stringify(layout.towers.map((tower) => tower.stackShadow)));
// ITEM 2: the two subcards no longer share one 4rem image column.
check('the inverter image column is wider than the battery tower column',
  layout.power.bodyTracks[0] > layout.towers[0].bodyTracks[0]
  && layout.power.bodyTracks[0] >= 120 && layout.power.image.width >= 110 && layout.power.image.height >= 120,
  JSON.stringify({ power: layout.power.bodyTracks, battery: layout.towers[0].bodyTracks, image: layout.power.image }));
check('charge level and the power values keep comparable sizes', layout.chargeSize >= layout.power.valueSize,
  JSON.stringify({ charge: layout.chargeSize, power: layout.power.valueSize }));
check('battery cards share their row evenly', Math.abs(layout.towers[0].box.width - layout.towers[1].box.width) <= 1,
  JSON.stringify([layout.power.box.width, ...layout.towers.map((tower) => tower.box.width)]));
// The inverter state is shown exactly once per card, not repeated in the header.
const stateMentions = await page.evaluate(() => [...document.querySelectorAll('.device-item *')]
  .filter((n) => !n.children.length && /feed in/i.test(n.textContent)).map((n) => `${n.tagName}.${n.className}|${n.parentElement?.className}|${n.textContent.trim()}`));
check('inverter state is shown exactly once per card', stateMentions.length === 1, JSON.stringify(stateMentions));

// 2a-2. ITEM 5 and ITEM 0 rendered: module counts 2..6 must stay inside the height cap, and a slot
// pattern that cannot describe a documented tower (7 populated slots, or a gap) must not render a
// tower at all. Both are driven through the admin payload, because the simulated inverter reports
// one fixed pair of towers.
const TOWER_HEIGHT_CAP = 207;
const patchBatteries = (mutate) => page.route('**/admin/api/devices', async (route) => {
  const response = await route.fetch();
  const data = await response.json();
  for (const device of data.devices) mutate(device.batteries || []);
  await route.fulfill({ response, json: data });
});
// A dashboard poll without waiting out the 10s timer; the dashboard listens for visibilitychange.
const forceDashboardPoll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
const towerGeometry = async () => {
  await forceDashboardPoll();
  await page.waitForResponse((response) => response.url().endsWith('/admin/api/devices'), { timeout: 20000 }).catch(() => { });
  await page.waitForTimeout(250);
  return (await readLayout()).towers;
};
// The dashboard keeps a tower's last trusted module count when a later answer is not 'ok' (resilience
// by design), so a first-time anomaly/pending answer needs a view without that memory and a stored snapshot.
const freshDashboardView = async () => {
  await page.evaluate(() => localStorage.removeItem('rct.dashboard.snapshot'));
  await page.reload();
  await page.waitForSelector('.device-item .device-battery-stack, .device-item .device-subcard');
};
// Graphic contract (owner-mandated): the drawn tower is 1 top cap + 1..BATTERY_TOWER_MAX_SEGMENTS
// (5) battery segments + 1 bottom cap. 2..5 modules render every module; the hardware maximum of
// 6 modules (RCT_MAX_MODULES_PER_TOWER) is drawn with 5 segments and the note keeps the true count.
for (const modules of [2, 3, 4, 5]) {
  await patchBatteries((batteries) => batteries.forEach((battery) => {
    battery.module_count = modules;
    battery.module_count_status = 'ok';
    battery.populated_module_slots = [...Array(modules).keys()];
  }));
  const towers = await towerGeometry();
  await page.unroute('**/admin/api/devices');
  check(`a ${modules}-module tower stays within the ${TOWER_HEIGHT_CAP}px height cap and shows all its modules`,
    towers.length > 0 && towers.every((tower) => tower.middles === modules
      && tower.stack.height <= TOWER_HEIGHT_CAP + 1 && tower.stack.height > 0
      && tower.stack.width > 0 && tower.stack.width <= 80),
    JSON.stringify(towers.map((tower) => ({ middles: tower.middles, height: tower.stack.height, width: tower.stack.width }))));
}
await patchBatteries((batteries) => batteries.forEach((battery) => {
  battery.module_count = 6;
  battery.module_count_status = 'ok';
  battery.populated_module_slots = [...Array(6).keys()];
}));
const sixModuleTowers = await towerGeometry();
await page.unroute('**/admin/api/devices');
check('a trusted 6-module tower is drawn with 5 middle segments and still says "6 modules"',
  sixModuleTowers.length > 0 && sixModuleTowers.every((tower) => tower.middles === 5 && tower.slices === 7
    && tower.noteHidden === false && /\b6 modules\b/.test(tower.noteText || '')
    && tower.stack.height <= TOWER_HEIGHT_CAP + 1),
  JSON.stringify(sixModuleTowers.map((tower) => ({ middles: tower.middles, slices: tower.slices, noteText: tower.noteText, height: tower.stack.height }))));
// Taller towers must get narrower rather than taller: that is what keeps the cap without dropping
// modules from the drawing. Both widths are within the graphic's 5-segment ceiling.
const widthAt = async (modules) => {
  await patchBatteries((batteries) => batteries.forEach((battery) => {
    battery.module_count = modules;
    battery.module_count_status = 'ok';
    battery.populated_module_slots = [...Array(modules).keys()];
  }));
  const towers = await towerGeometry();
  await page.unroute('**/admin/api/devices');
  return towers[0].stack.width;
};
const widthTwo = await widthAt(2);
const widthFive = await widthAt(5);
check('a five-module tower is drawn narrower than a two-module one instead of taller', widthFive < widthTwo,
  JSON.stringify({ widthTwo, widthFive }));
// ITEM 0: seven populated slots are a data anomaly (the catalog array has seven elements, the
// documented hardware takes six modules), and a gap in the middle is one too. Neither may render a
// seven-storey tower; the card keeps its readings and says so instead.
for (const [name, slots] of [['seven populated slots', [0, 1, 2, 3, 4, 5, 6]], ['a gap in the middle', [0, 1, 4]]]) {
  await patchBatteries((batteries) => batteries.forEach((battery) => {
    battery.module_count = null;
    battery.module_count_status = 'anomaly';
    battery.populated_module_slots = slots;
  }));
  await freshDashboardView();
  const towers = await towerGeometry();
  await page.unroute('**/admin/api/devices');
  check(`${name} renders a diagnostic note instead of a tower`,
    towers.length > 0 && towers.every((tower) => tower.slices === 0 && tower.noteHidden === false
      && /^55\s*%$|^42\s*%$/.test(tower.charge)),
    JSON.stringify(towers.map((tower) => ({ slices: tower.slices, noteHidden: tower.noteHidden, charge: tower.charge }))));
}
// A scan still in progress (module_count_status 'pending', no trusted count yet) must render a
// neutral "detecting modules" placeholder, never the old fabricated one-module tower nor the
// "layout unclear" anomaly text - the three no-tower branches must stay textually distinct.
await patchBatteries((batteries) => batteries.forEach((battery) => {
  battery.module_count = null;
  battery.module_count_status = 'pending';
  battery.populated_module_slots = [];
}));
await freshDashboardView();
const pendingTowers = await towerGeometry();
await page.unroute('**/admin/api/devices');
check('a mid-scan tower (module_count_status pending) renders a neutral "detecting modules" placeholder, not a one-module tower',
  pendingTowers.length > 0 && pendingTowers.every((tower) => tower.slices === 0 && tower.noteHidden === false
    && /detecting modules/i.test(tower.noteText || '')),
  JSON.stringify(pendingTowers.map((tower) => ({ slices: tower.slices, noteHidden: tower.noteHidden, noteText: tower.noteText }))));
await forceDashboardPoll();
await page.waitForTimeout(300);

await page.setViewportSize({ width: 1000, height: 800 });
await page.waitForTimeout(400);
const twoCards = await page.evaluate(() => {
  const grid = document.querySelector('.device-grid');
  const copy = grid.firstElementChild.cloneNode(true);
  grid.append(copy);
  const result = [...grid.querySelectorAll('.device-item')].map((card) => {
    const subcards = [...card.querySelectorAll('.device-subcard')].map((node) => node.getBoundingClientRect());
    return {
      width: Math.round(card.getBoundingClientRect().width),
      subcards: subcards.length,
      // Below the 56rem container width the subcards reflow to two columns, so the last one wraps
      // onto a second row instead of all three sharing one.
      wrapped: subcards.some((rect) => rect.top > subcards[0].bottom - 1),
    };
  });
  copy.remove();
  return result;
});
check('two narrow cards reflow their subcards onto more than one row at 1000px',
  twoCards.length === 2 && twoCards.every((card) => card.subcards === 3 && card.wrapped), JSON.stringify(twoCards));
await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(400);
const narrow = { overflow: await overflow(), cardHeight: await page.evaluate(() => Math.round(document.querySelector('.device-item').getBoundingClientRect().height)) };
await shot('02b-dashboard-390');
check('no horizontal overflow at 390px', narrow.overflow === 0, JSON.stringify(narrow));
// Mobile layout of a subcard, measured in the browser rather than read off the stylesheet: below the
// 19rem container width (@container on .device-subcard) .device-subcard-body collapses to a single track, so the illustration moves
// above the readings, stays centred and never widens the card. Both the inverter image and the
// battery tower are checked, since they are sized by different rules now.
const measureMobileImages = () => page.evaluate(() => {
  const columns = (node) => getComputedStyle(node).gridTemplateColumns.split(' ').length;
  return [...document.querySelectorAll('.device-subcard')].map((card) => {
    const body = card.querySelector('.device-subcard-body');
    const visualNode = card.querySelector('.device-subcard-image, .device-battery-stack');
    const readings = card.querySelector('.device-metric-grid, .device-subcard-readings');
    const visual = visualNode.getBoundingClientRect();
    const cardBox = card.getBoundingClientRect();
    return {
      kind: card.classList.contains('device-subcard-power') ? 'power' : 'battery',
      tracks: columns(body),
      width: Math.round(visual.width), height: Math.round(visual.height),
      // The stacked layout is keyed to the subcard's own content width (@container, 19rem).
      stacked: card.clientWidth - parseFloat(getComputedStyle(card).paddingLeft) - parseFloat(getComputedStyle(card).paddingRight) <= 19 * 16,
      inside: visual.left >= cardBox.left - 1 && visual.right <= cardBox.right + 1,
      aboveReadings: visual.bottom <= readings.getBoundingClientRect().top + 1,
      centred: Math.abs((visual.left - cardBox.left) - (cardBox.right - visual.right)) <= 2,
    };
  });
});
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await page.waitForTimeout(400);
  const images = await measureMobileImages();
  check(`subcard illustrations follow the container layout and stay inside the card at ${width}px`,
    images.length === 3 && images.every((image) => image.tracks === (image.stacked ? 1 : 2) && image.width > 0 && image.height > 0
      && image.inside && (!image.stacked || (image.aboveReadings && image.centred))), JSON.stringify(images));
  if (width === 320) check('subcards are stacked (illustration above readings) at 320px', images.every((image) => image.stacked), JSON.stringify(images));
}
// The footer is fixed to the viewport bottom while .app-shell reserves --rct-footer-height (2.75rem)
// there, so the last card must not merely touch the footer edge but keep a visible gap. Measured
// gap between the last card's bottom and the footer top after scrolling to the end: 23.9px at 390px
// and 12.7px at 320px, where the footer text wraps to two lines and the footer grows to 55.4px. A
// sweep of 430..280px put the wrap between 344px (one line, 44px) and 330px, and the wrapped gap is
// a constant 12.6875px from 330px down to 280px.
// `reserveShortfall` is asserted, not merely recorded. Before the fix the footer was 11.375px taller
// at 320px than .app-shell reserved, so the gap above survived on the last card's incidental height
// rather than on the reserve: a taller final element would have slid under the footer. The CSS now
// raises --rct-footer-height to 3.5rem below 420px, which feeds both the reserve and the footer's
// min-height, so the reserve covers the wrapped footer by construction and the shortfall must be
// <= 0 at every width measured here.
const MIN_FOOTER_GAP = 8;
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await page.waitForTimeout(400);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const bottom = await page.evaluate(() => {
    const content = document.querySelector('#devices-list .device-item:last-child').getBoundingClientRect();
    const footer = document.querySelector('.admin-footer').getBoundingClientRect();
    const reserved = parseFloat(getComputedStyle(document.querySelector('.app-shell')).paddingBottom);
    return {
      contentBottom: content.bottom, footerTop: footer.top, footerHeight: footer.height, footerBottom: footer.bottom,
      gap: footer.top - content.bottom, reserved, innerHeight: window.innerHeight,
      reserveShortfall: footer.height - reserved,
      clipped: footer.top < 0 || footer.bottom > window.innerHeight + 1,
    };
  });
  check(`dashboard content clears the fixed footer at ${width}px`,
    bottom.gap >= MIN_FOOTER_GAP && Math.abs(bottom.footerBottom - bottom.innerHeight) <= 1
    && !bottom.clipped && bottom.reserved > 0 && bottom.reserveShortfall <= 0,
    JSON.stringify(bottom));
}
await page.setViewportSize({ width: 1280, height: 800 });
await page.waitForTimeout(400);

// 2a-3. The serial number sits in the second header line between address and last connection, and
// is omitted without a stray separator or "undefined" while the device has not reported one.
const metaLine = () => page.evaluate(() => {
  const meta = document.querySelector('.device-item .device-head-meta');
  return { items: [...meta.children].map((node) => node.textContent.trim()), text: meta.textContent, scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth };
});
await forceDashboardPoll();
await page.waitForFunction(() => /Serial number: SIM1234567/.test(document.querySelector('.device-item .device-head-meta')?.textContent || ''), null, { timeout: 20000 }).catch(() => { /* reported below */ });
const withSerial = await metaLine();
check('the device header shows the serial number between address and last connection',
  withSerial.items.length === 3 && /:\d+$/.test(withSerial.items[0]) && withSerial.items[1] === 'Serial number: SIM1234567'
  && withSerial.items[2].startsWith('Last connection: '), JSON.stringify(withSerial.items));
await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(300);
const narrowSerial = await metaLine();
check('the serial number line wraps at 390px without horizontal scroll', narrowSerial.scrollW <= narrowSerial.clientW, JSON.stringify(narrowSerial));
await page.setViewportSize({ width: 1280, height: 800 });
await page.route('**/admin/api/devices', async (route) => {
  const response = await route.fetch();
  const data = await response.json();
  for (const device of data.devices) device.serial_number = null;
  await route.fulfill({ response, json: data });
});
await forceDashboardPoll();
await page.waitForFunction(() => !/Serial number/.test(document.querySelector('.device-item .device-head-meta')?.textContent || ''), null, { timeout: 20000 }).catch(() => { /* reported below */ });
const withoutSerial = await metaLine();
check('an unknown serial number is omitted cleanly from the device header',
  withoutSerial.items.length === 2 && !/undefined|null|Serial/.test(withoutSerial.text) && withoutSerial.items[0].length > 0
  && withoutSerial.items[1].startsWith('Last connection: '), JSON.stringify(withoutSerial.items));
await page.unroute('**/admin/api/devices');

// 2b. dashboard polling: a forced poll keeps the card nodes, and parameters are not part of the
// loop. There is no manual refresh button anymore (removed per user request, auto-polling already
// covers it); a visibilitychange dispatch forces the same loadDashboard({ automatic: true }) path
// the real 10s timer and tab-focus handler use, without waiting out the interval.
await page.evaluate(() => { document.querySelector('.device-item').dataset.probe = 'kept'; });
const forcePoll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
const waitForSuccessfulPoll = async () => {
  const before = await page.evaluate(() => JSON.parse(localStorage.getItem('rct.dashboard.snapshot') || 'null')?.at || 0);
  const response = page.waitForResponse((r) => new URL(r.url()).pathname === '/admin/api/devices' && r.status() === 200, { timeout: 20000 });
  await forcePoll();
  await response;
  await page.waitForFunction((previous) => {
    const snapshot = JSON.parse(localStorage.getItem('rct.dashboard.snapshot') || 'null');
    return snapshot?.at > previous;
  }, before, { timeout: 20000 });
};
const polledPaths = [];
const onRequest = (request) => polledPaths.push(new URL(request.url()).pathname);
page.on('request', onRequest);
await waitForSuccessfulPoll();
page.off('request', onRequest);
check('forced refresh patches the inverter card in place', (await page.locator('.device-item[data-probe="kept"]').count()) === 1);
check('periodic refresh does not request /admin/api/parameters', !polledPaths.includes('/admin/api/parameters'), polledPaths.join(','));
check('no manual refresh button remains on the dashboard', (await page.locator('#refresh-devices').count()) === 0);

// 2b-2. collapsible device card: header stays, body hides, state survives poll and reload.
const toggleState = () => page.evaluate(() => {
  const card = document.querySelector('.device-item');
  const button = card.querySelector('.device-toggle');
  const body = document.getElementById(button.getAttribute('aria-controls'));
  const visible = (node) => node.getBoundingClientRect().height > 0;
  return {
    expanded: button.getAttribute('aria-expanded'), bodyVisible: visible(body),
    headVisible: visible(card.querySelector('.device-head')), titleVisible: visible(card.querySelector('.device-head h3')),
    stored: localStorage.getItem('rct-admin.collapsedDevices'),
  };
});
const expandedState = await toggleState();
check('device card is expanded by default', expandedState.expanded === 'true' && expandedState.bodyVisible, JSON.stringify(expandedState));
await page.locator('.device-item .device-toggle').first().focus();
await page.keyboard.press('Enter');
const collapsedState = await toggleState();
check('collapsing hides the body but keeps the header, and flips aria-expanded',
  collapsedState.expanded === 'false' && !collapsedState.bodyVisible && collapsedState.headVisible && collapsedState.titleVisible, JSON.stringify(collapsedState));
check('collapsed state is persisted in localStorage', /\[".+"\]/.test(collapsedState.stored || ''), String(collapsedState.stored));
await waitForSuccessfulPoll();
check('a poll cycle does not reset the collapse', (await toggleState()).expanded === 'false');
await page.reload();
await page.waitForSelector('.device-item .device-toggle');
const reloadedState = await toggleState();
check('collapsed state survives a page reload', reloadedState.expanded === 'false' && !reloadedState.bodyVisible && reloadedState.headVisible, JSON.stringify(reloadedState));
await page.locator('.device-item .device-toggle').first().click();
const restoredState = await toggleState();
check('expanding restores the body and clears the stored id',
  restoredState.expanded === 'true' && restoredState.bodyVisible && restoredState.stored === '[]', JSON.stringify(restoredState));
const csp = (await (await context.request.get(base + '/ui/dashboard')).headers())['content-security-policy'];
check('CSP header present', Boolean(csp && csp.includes("script-src 'self'")));

// 2c. toast de-duplication and slide animation, and suppression of a repeated poll failure: with
// no manual refresh button and no stale-data banner on the page, automatic polling is the only
// loadDashboard() trigger left in the UI; a failing poll must still toast once (with the slide-in
// animation), and a second consecutive automatic failure must not stack another identical toast.
check('no "Updated" timestamp line on the dashboard', (await page.locator('#dashboard-updated').count()) === 0);
const rateFailureStart = problems.length;
await page.route('**/admin/api/devices', (route) => route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: 'The request rate or the failed-authentication limit was exceeded.' }) }));
await forcePoll();
await page.waitForFunction(() => document.querySelector('#toast-region .alert-danger'), null, { timeout: 8000 });
check('no stale-data banner element exists on the page', (await page.locator('#dashboard-staleness').count()) === 0);
// 350ms, not 150ms: the slide-in transition itself takes 300ms (admin.css), and the transform
// assertion below must land after it settles, not mid-transition.
await page.waitForTimeout(350);
const dupToasts = await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('rate or the failed-authentication limit')).length);
check('a failed poll produces exactly one toast', dupToasts === 1, String(dupToasts));
const toastAnim = await page.evaluate(() => {
  const node = [...document.querySelectorAll('#toast-region .alert-danger')].find((n) => n.textContent.includes('rate or the failed-authentication limit'));
  if (!node) return null;
  const style = getComputedStyle(node);
  return { visible: node.classList.contains('is-visible'), visibility: style.visibility, transform: style.transform };
});
// Slide-in: every toast enters from the right edge, so the settled transform must be a zero X offset.
const settledX = (matrix) => (matrix === 'none' ? 0 : Number(matrix.match(/matrix\(([^)]*)\)/)?.[1].split(',')[4] ?? NaN));
check('surviving toast slid in from the right and is visible',
  Boolean(toastAnim?.visible && toastAnim.visibility === 'visible' && Math.abs(settledX(toastAnim.transform)) < 1), JSON.stringify(toastAnim));
await forcePoll();
await page.waitForTimeout(350);
const dupToastsAfter = await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('rate or the failed-authentication limit')).length);
check('a second consecutive automatic failure does not stack another toast', dupToastsAfter === 1, String(dupToastsAfter));
// Slide-out: dismissing the toast marks it leaving, moves it back to the right edge, then removes it.
const leavingToast = page.locator('#toast-region .alert-danger', { hasText: 'rate or the failed-authentication limit' }).first();
await leavingToast.locator('.btn-close').click();
// The transform only starts moving on the next frame, so wait for the real condition instead of sampling at click time.
const leaveState = await leavingToast.evaluate((node) => new Promise((resolve) => {
  const read = () => ({ leaving: node.classList.contains('is-leaving'), transform: getComputedStyle(node).transform });
  const deadline = performance.now() + 1500;
  const poll = () => {
    const state = read();
    const x = Number(state.transform.match(/matrix\(([^)]*)\)/)?.[1].split(',')[4] ?? 0);
    if (x > 0 || performance.now() > deadline) resolve(state); else requestAnimationFrame(poll);
  };
  poll();
})).catch(() => null);
check('dismissed toast is marked leaving and slides out to the right', Boolean(leaveState?.leaving && settledX(leaveState.transform) > 0), JSON.stringify(leaveState));
await page.waitForFunction(() => ![...document.querySelectorAll('#toast-region .alert-danger')].some((n) => n.textContent.includes('rate or the failed-authentication limit')), null, { timeout: 2000 });
await page.unroute('**/admin/api/devices');
await forcePoll();
await page.waitForTimeout(300);
scrubExpected(rateFailureStart, (p) => p.includes(`${base}/admin/api/devices`) &&
  (p.startsWith('http 429: GET ') || (p.startsWith('console ') && p.includes('429'))));

// 2d. a failed load keeps the last known KPI values (they are the last truth we had) and shows the
// inline offline banner with the time of the last good snapshot.
const kpiBefore = await page.locator('#pv-power').innerText();
const unavailableStart = problems.length;
await page.route('**/admin/api/devices', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Administration is unavailable' }) }));
await forcePoll();
await page.waitForFunction(() => document.querySelector('.dashboard-offline-banner:not([hidden])'), null, { timeout: 20000 });
check('the offline banner is inline, names the last good data time and the server error',
  await page.evaluate(() => { const b = document.querySelector('.dashboard-offline-banner'); return getComputedStyle(b).position !== 'fixed' && /Server error \(503\).*\d\d:\d\d/.test(b.textContent); }));
const pvAfter = await page.locator('#pv-power').innerText();
check('the last known KPI values are kept, not blanked', pvAfter === kpiBefore, `${pvAfter} vs ${kpiBefore}`);
await shot('02d-dashboard-failed-poll');
await page.unroute('**/admin/api/devices');
await forcePoll();
await page.waitForFunction(() => !document.querySelector('#devices-list .text-danger'), null, { timeout: 20000 });
scrubExpected(unavailableStart, (p) => p.includes(`${base}/admin/api/devices`) &&
  (p.startsWith('http 503: GET ') || (p.startsWith('console ') && p.includes('503'))));

// 2e. connection loss: going offline opens the blocking modal (toasts cleared, page inert), and the
// return of the connection is detected by the /health probe, which closes the modal again.
const offlineStart = problems.length;
await context.setOffline(true);
await page.waitForSelector('#reconnect-modal.show', { timeout: 8000 });
check('connection loss opens the reconnect modal', await page.evaluate(() => document.body.classList.contains('is-reconnecting')));
check('reconnect modal is a labelled dialog', await page.evaluate(() => {
  const el = document.getElementById('reconnect-modal');
  return el.getAttribute('aria-modal') === 'true' && el.getAttribute('role') === 'dialog' && document.getElementById(el.getAttribute('aria-labelledby')).textContent.length > 0;
}));
await shot('02e-connection-lost');
await context.setOffline(false);
await page.waitForFunction(() => !document.querySelector('#reconnect-modal.show') && !document.body.classList.contains('is-reconnecting'), null, { timeout: 15000 });
check('reconnect modal closes once the server answers again', true);
scrubExpected(offlineStart, (p) => p.startsWith('requestfailed: GET ') &&
  (p.includes(`${base}/health`) || p.includes(`${base}/admin/api/devices`)) &&
  p.includes('ERR_INTERNET_DISCONNECTED'));

// 2e-2. a probe that fails while the tab is hidden schedules no retry; returning to the foreground
// must resume probing so the modal closes without any page-level polling.
const setVisibility = (state) => page.evaluate((value) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value });
  document.dispatchEvent(new Event('visibilitychange'));
}, state);
const probeFailureStart = problems.length;
await page.route('**/health', (route) => route.abort());
await sleep(800); // Bootstrap ignores show() while the previous hide transition still runs
await page.evaluate(() => window.RCTReconnect.start());
await page.waitForSelector('#reconnect-modal.show', { timeout: 8000 });
await setVisibility('hidden');
await sleep(3500); // the scheduled retry fires and fails while hidden, leaving no further timer
await page.unroute('**/health');
await setVisibility('visible');
await page.waitForFunction(() => !document.querySelector('#reconnect-modal.show') && !document.body.classList.contains('is-reconnecting'), null, { timeout: 15000 });
check('reconnect modal closes after returning to the foreground once the server is back', true);
await page.evaluate(() => { delete document.visibilityState; });
scrubExpected(probeFailureStart, (p) => p.includes(`${base}/health`) && (
  p.startsWith('requestfailed: GET ') && p.includes('ERR_FAILED')
  || p.startsWith('console error: Failed to load resource: net::ERR_FAILED')
));

// 3. layouts
async function layouts(label, urls) {
  for (const [w, h] of [[1280, 800], [390, 844], [320, 640]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const url of urls) {
      await page.goto(base + url);
      await page.waitForLoadState('networkidle');
      await sleep(300);
      const extra = await overflow();
      check(`no horizontal overflow ${label} ${url} @${w}`, extra <= 0, `overflow=${extra}`);
      await shot(`${label}-${url.replace(/\W+/g, '_')}-${w}`);
    }
  }
  await page.setViewportSize({ width: 1280, height: 800 });
}
const pages = ['/ui/dashboard', '/ui/inverters', '/ui/tsdb', '/ui/prometheus', '/ui/tokens', '/ui/settings', '/ui/about'];
await layouts('light', pages);
const beforeDarkRestart = problems.length;
await stop(server);
server = await startServer(db, httpPort, devicePort, 'before-dark-layouts');
scrubExpected(beforeDarkRestart, (p) => expectedRestartProblem(p, base));
await page.evaluate(() => { localStorage.setItem('theme', 'dark'); });
await page.goto(base + '/ui/dashboard');
await page.click('#theme-toggle');
const theme = await page.evaluate(() => document.documentElement.getAttribute('data-bs-theme'));
check('theme toggle switches data-bs-theme', ['dark', 'light'].includes(theme), theme);
if (theme !== 'dark') await page.click('#theme-toggle');
check('dark mode active', (await page.evaluate(() => document.documentElement.getAttribute('data-bs-theme'))) === 'dark');
await layouts('dark', pages);
await page.click('#theme-toggle');

check('header stays sticky', (await page.evaluate(() => getComputedStyle(document.querySelector('.admin-navbar')).position)) === 'sticky');

// 3c. navbar: enlarged logo, shortened label, smaller header buttons with usable touch targets
await page.goto(base + '/ui/dashboard');
const navbar = async () => page.evaluate(() => {
  const box = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { w: r.width, h: r.height }; };
  return {
    logo: box('.nav-logo'),
    brand: document.querySelector('.navbar-brand span').textContent.trim(),
    title: document.title,
    header: box('.admin-navbar'),
    theme: box('#theme-toggle'),
    logout: box('#logout-button'),
    menu: box('#nav-toggle'),
  };
});
let nav = await navbar();
check('navbar label shortened to Administration', nav.brand === 'Administration', nav.brand);
check('browser tab title still says RCT Administration', /RCT Administration/.test(nav.title), nav.title);
// 3.1rem x 1.8rem before, 5.1rem x 2.78rem now (1.64x) at the SVG's own 599:326 ratio.
check('logo enlarged by about 1.5-1.8x', Math.abs(nav.logo.w - 81.6) < 2 && Math.abs(nav.logo.h - 44.5) < 2, JSON.stringify(nav.logo));
check('logo keeps the SVG aspect ratio', Math.abs(nav.logo.w / nav.logo.h - 599 / 326) < 0.05, String(nav.logo.w / nav.logo.h));
check('enlarged logo fits inside the header', nav.logo.h <= nav.header.h, `${nav.logo.h} <= ${nav.header.h}`);
// 2.8rem (44.8px) before; 2.3rem (36.8px) now, still above the 2.2rem floor.
check('header buttons shrunk but keep a 2.2rem touch target', [nav.theme, nav.logout].every((b) => b.h >= 35 && b.h < 40), JSON.stringify([nav.theme, nav.logout]));
check('theme toggle is square at the smaller size', Math.abs(nav.theme.w - nav.theme.h) < 1, JSON.stringify(nav.theme));
await shot('navbar-desktop');
await page.setViewportSize({ width: 390, height: 844 });
await sleep(200);
nav = await navbar();
check('hamburger shares the smaller header button height', nav.menu && Math.abs(nav.menu.h - nav.theme.h) < 1, JSON.stringify([nav.menu, nav.theme]));
check('logo still fits the header at mobile width', nav.logo.h <= nav.header.h, `${nav.logo.h} <= ${nav.header.h}`);
await shot('navbar-mobile');
await page.setViewportSize({ width: 1280, height: 800 });

// 3d. Layout contract: one card gap token, shared Prometheus/Inverters geometry, no horizontal scroll.
// #main-content is zoomed (--content-zoom), so rect distances are divided by that zoom to get CSS px.
const GAP_TOLERANCE = 1;
const STACKED_PAGES = [
  ['/ui/prometheus', '#exposed-list tr'], ['/ui/inverters', '#settings-sections .card, #writable-list tr'],
  ['/ui/tsdb', '#settings-sections .card'], ['/ui/tokens', '#tokens-title'],
  ['/ui/settings', '#settings-sections .card'], ['/ui/energy', '#energy-list > *'], ['/ui/about', '.about-top-row .card'],
];
async function stackedGeometry() {
  return page.evaluate(() => {
    const main = document.querySelector('#main-content');
    const zoom = parseFloat(getComputedStyle(main).zoom) || 1;
    // Resolve the token through a probe outside the zoomed content, so the unit never matters.
    const probe = document.body.appendChild(document.createElement('div'));
    probe.style.cssText = 'position:absolute;visibility:hidden;width:var(--rct-card-gap)';
    const token = parseFloat(getComputedStyle(probe).width);
    probe.remove();
    const visible = (node) => { const r = node.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const heading = main.querySelector('.page-heading');
    const container = heading.parentElement;
    const blocks = [...container.children].filter((node) => node.matches('.card, .settings-grid, .energy-grid, #export-actions, .about-top-row, .prometheus-dependent-settings') && visible(node));
    // The About row is spaced from its heading by its own mt-1, not by the sibling rule.
    const chain = blocks[0]?.matches('.about-top-row') ? blocks : [heading, ...blocks];
    const gaps = [];
    chain.slice(1).forEach((node, i) => gaps.push({ where: `${chain[i].className || chain[i].id} -> ${node.className || node.id}`, gap: (node.getBoundingClientRect().top - chain[i].getBoundingClientRect().bottom) / zoom }));
    // Tiles inside a grid or the About row: the nearest neighbour below and to the right.
    const tiles = [...container.querySelectorAll(':scope > .settings-grid > *, :scope > .energy-grid > *, :scope > .about-top-row .card')].filter(visible);
    const rects = tiles.map((node) => node.getBoundingClientRect());
    const overlap = (a0, a1, b0, b1) => Math.min(a1, b1) - Math.max(a0, b0);
    rects.forEach((a, i) => {
      const below = rects.filter((b, j) => j !== i && b.top >= a.bottom - 0.5 && overlap(a.left, a.right, b.left, b.right) > 1);
      if (below.length) gaps.push({ where: `tile ${i} below`, gap: (Math.min(...below.map((b) => b.top)) - a.bottom) / zoom });
      const beside = rects.filter((b, j) => j !== i && b.left >= a.right - 0.5 && overlap(a.top, a.bottom, b.top, b.bottom) > 1);
      if (beside.length) gaps.push({ where: `tile ${i} beside`, gap: (Math.min(...beside.map((b) => b.left)) - a.right) / zoom });
    });
    const grid = container.querySelector(':scope > .settings-grid, :scope > .energy-grid');
    const gridGap = grid ? { row: parseFloat(getComputedStyle(grid).rowGap), column: parseFloat(getComputedStyle(grid).columnGap) } : null;
    return { zoom, token, gaps, gridGap, blocks: blocks.length, tiles: tiles.length, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  });
}
const stackedLoopStart = problems.length;
for (const scheme of ['light', 'dark']) {
  await page.evaluate((value) => localStorage.setItem('rct-admin-theme', value), scheme);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    for (const [url, ready] of STACKED_PAGES) {
      const label = `${url} @${width} ${scheme}`;
      await page.goto(base + url);
      await page.waitForSelector(ready, { state: 'attached' });
      await page.waitForLoadState('networkidle');
      await sleep(300);
      const geometry = await stackedGeometry();
      if (url === '/ui/prometheus' && width === 1280) {
        check(`theme ${scheme} is active for the layout contract`, (await page.evaluate(() => document.documentElement.getAttribute('data-bs-theme'))) === scheme);
        check('card gap token is 20px', geometry.token === 20, String(geometry.token));
      }
      check(`content zoom is applied to ${label}`, geometry.zoom > 0 && geometry.zoom < 1, String(geometry.zoom));
      check(`stacked blocks are present on ${label}`, geometry.blocks >= 1, String(geometry.blocks));
      const off = geometry.gaps.filter((g) => Math.abs(g.gap - geometry.token) > GAP_TOLERANCE);
      check(`every card gap equals the token on ${label}`, geometry.gaps.length > 0 && off.length === 0,
        JSON.stringify(off.length ? off : geometry.gaps.map((g) => Math.round(g.gap * 10) / 10)));
      if (geometry.gridGap) {
        check(`grid row and column gap equal the token on ${label}`,
          Math.abs(geometry.gridGap.row - geometry.token) <= 0.5 && Math.abs(geometry.gridGap.column - geometry.token) <= 0.5, JSON.stringify(geometry.gridGap));
      }
      check(`no horizontal page scroll on ${label}`, geometry.overflow <= 0, `overflow=${geometry.overflow}`);
    }
    // Prometheus and Inverters are one layout: same container edges, same card width, same Add action corner.
    const frames = {};
    for (const [url, prefix] of [['/ui/prometheus', 'exposed'], ['/ui/inverters', 'writable']]) {
      await page.goto(base + url);
      await page.waitForSelector(`#${prefix}-list tr`, { state: 'attached' });
      await page.waitForLoadState('networkidle');
      await sleep(300);
      frames[prefix] = await page.evaluate((pfx) => {
        const edges = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
        const card = document.getElementById(`${pfx}-list`).closest('.card');
        const container = document.querySelector('.page-heading').parentElement;
        const add = card.querySelector('.btn-primary');
        const pager = document.getElementById(`${pfx}-range`).parentElement;
        return {
          container: edges(container), heading: edges(document.querySelector('.page-heading')), card: edges(card),
          wrap: edges(card.querySelector('.param-table-wrap')), search: edges(document.getElementById(`${pfx}-search`)),
          pager: edges(pager), footer: edges(document.querySelector('.admin-footer')),
          rows: document.querySelectorAll(`#${pfx}-list .metric-row`).length,
          pageOverflowY: document.documentElement.scrollHeight - document.documentElement.clientHeight,
          addFromRight: card.getBoundingClientRect().right - add.getBoundingClientRect().right, addFromLeft: add.getBoundingClientRect().left - card.getBoundingClientRect().left,
          addFromTop: add.getBoundingClientRect().top - card.getBoundingClientRect().top, cardHeight: card.getBoundingClientRect().height,
          zoom: parseFloat(getComputedStyle(document.querySelector('#main-content')).zoom),
        };
      }, prefix);
    }
    const [pm, inv] = [frames.exposed, frames.writable];
    const label = `@${width} ${scheme}`;
    const near = (a, b) => Math.abs(a - b) <= 1;
    check(`Prometheus and Inverters share the container edges ${label}`, near(pm.container.left, inv.container.left) && near(pm.container.right, inv.container.right), JSON.stringify([pm.container, inv.container]));
    check(`Prometheus and Inverters headings align ${label}`, near(pm.heading.left, inv.heading.left) && near(pm.heading.right, inv.heading.right));
    check(`Prometheus and Inverters table cards have the same edges and width ${label}`,
      near(pm.card.left, inv.card.left) && near(pm.card.right, inv.card.right) && near(pm.card.width, inv.card.width), JSON.stringify([pm.card, inv.card]));
    check(`table wrapper and search share their edges ${label}`, near(pm.wrap.left, inv.wrap.left) && near(pm.wrap.right, inv.wrap.right) && near(pm.search.left, inv.search.left) && near(pm.search.width, inv.search.width));
    if (width === 1280) check(`sparse Inverters table fills the available viewport and keeps its pager at the bottom ${label}`,
      inv.rows <= 1 && inv.footer.top - inv.card.bottom >= 0 && inv.footer.top - inv.card.bottom <= 40
      && inv.card.bottom - inv.pager.bottom >= 0 && inv.card.bottom - inv.pager.bottom <= 40
      && inv.pageOverflowY <= 1,
      JSON.stringify({ rows: inv.rows, cardBottom: inv.card.bottom, pagerBottom: inv.pager.bottom, footerTop: inv.footer.top, pageOverflowY: inv.pageOverflowY }));
    // Centered against a title block of different height, so only the horizontal corner and the header band are compared.
    const addInside = (f) => f.addFromRight >= 0 && f.addFromLeft >= 0 && f.addFromTop >= 0 && f.addFromTop < f.cardHeight / 2;
    check(`the primary Add action sits in the card header band ${label}`, addInside(pm) && addInside(inv), JSON.stringify([pm, inv].map((f) => [f.addFromLeft, f.addFromRight, f.addFromTop])));
    if (width >= 768) check(`the primary Add action is top right on both pages ${label}`, near(pm.addFromRight, inv.addFromRight) && pm.addFromRight < 40, JSON.stringify([pm.addFromRight, inv.addFromRight]));
  }
}
// Viewport height contract: the Inverters page is the reference. Every param_table page ends its card right above
// the footer once scrolled to the bottom, and a short page fills exactly the viewport (no scroll caused by the card).
{
  const savedViewport = page.viewportSize();
  const heightProbe = () => page.evaluate((pfx) => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' });
    const card = document.getElementById(`${pfx}-list`).closest('.card');
    const footer = document.querySelector('.admin-footer').getBoundingClientRect();
    const de = document.documentElement;
    return { cardToFooter: footer.top - card.getBoundingClientRect().bottom, docOverflowY: de.scrollHeight - de.clientHeight, footerTop: footer.top, cardBottom: card.getBoundingClientRect().bottom, rows: document.querySelectorAll(`#${pfx}-list .metric-row`).length };
  }, pfx);
  let pfx;
  for (const [width, height] of [[1280, 720], [1280, 900], [1536, 864], [1280, 1200], [1280, 2200], [390, 844]]) {
    await page.setViewportSize({ width, height });
    const probes = {};
    for (const [url, prefix] of [['/ui/prometheus', 'exposed'], ['/ui/inverters', 'writable']]) {
      await page.goto(base + url);
      await page.waitForSelector(`#${prefix}-list tr`, { state: 'attached' });
      await page.waitForLoadState('networkidle');
      await sleep(300);
      pfx = prefix;
      probes[prefix] = await heightProbe();
    }
    const [pm, inv] = [probes.exposed, probes.writable];
    const label = `@${width}x${height}`;
    check(`Prometheus card ends above the footer exactly like Inverters when scrolled to the bottom ${label}`,
      Math.abs(pm.cardToFooter - inv.cardToFooter) <= 1 && pm.cardToFooter >= 0, JSON.stringify([pm, inv]));
    if (height === 2200) check(`short Prometheus page fills the viewport like Inverters ${label}`,
      pm.docOverflowY <= 1 && inv.docOverflowY <= 1 && Math.abs(pm.cardBottom - inv.cardBottom) <= 1, JSON.stringify([pm, inv]));
  }
  await page.setViewportSize(savedViewport);
}
// This server runs without the Energy Manager, so the /ui/energy visits above deliberately get its 503.
scrubExpected(stackedLoopStart, expectedStatus(`${base}/admin/api/energy/devices`, 503));
// Prove this measurement catches a single-card spacing regression, then restore the page.
await page.goto(base + '/ui/prometheus');
await page.waitForSelector('#exposed-list tr', { state: 'attached' });
// A constructable stylesheet is not blocked by the page's style-src CSP, unlike an injected <style> tag.
await page.evaluate(() => {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync('#main-content > .card { margin-top: calc(var(--rct-card-gap) + 7px) !important; }');
  window.__gapMutation = sheet;
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
});
const mutatedGap = await stackedGeometry();
check('card gap measurement rejects a 7px margin mutation',
  mutatedGap.gaps.some(({ gap }) => Math.abs(gap - mutatedGap.token) > GAP_TOLERANCE), JSON.stringify(mutatedGap.gaps));
await page.evaluate(() => { document.adoptedStyleSheets = document.adoptedStyleSheets.filter((sheet) => sheet !== window.__gapMutation); });
const restoredGap = await stackedGeometry();
check('card gap measurement passes after the mutation is removed',
  restoredGap.gaps.length > 0 && restoredGap.gaps.every(({ gap }) => Math.abs(gap - restoredGap.token) <= GAP_TOLERANCE), JSON.stringify(restoredGap.gaps));
await page.evaluate(() => localStorage.setItem('rct-admin-theme', 'light'));
await page.setViewportSize({ width: 1280, height: 800 });

// 4. PAT create / use / revoke
const afterLayoutsRestart = problems.length;
await stop(server);
server = await startServer(db, httpPort, devicePort, 'after-layouts');
scrubExpected(afterLayoutsRestart, (p) => expectedRestartProblem(p, base));
await page.goto(base + '/ui/tokens');
await page.waitForSelector('#tokens-list tr');
check('token table has the six columns', JSON.stringify(await page.locator('.token-table thead th').evaluateAll((n) => n.map((e) => e.textContent.trim()))) === JSON.stringify(['Name', 'Permission', 'Created', 'Last used', 'Expires', 'Actions']));
check('Add token button is shown and the form is not permanently on the page', (await page.locator('#open-add-token').isVisible()) && !(await page.locator('#token-form').isVisible()));
await page.click('#open-add-token');
await page.waitForSelector('#add-token-modal.show #token-name', { state: 'visible' });
await sleep(250);
check('expiry defaults to 90 days', (await page.inputValue('#token-expires')) === '90d');
await page.fill('#token-name', 'e2e-monitor');
await page.click('#token-form button[type=submit]');
await page.waitForSelector('#new-token-result:not([hidden])');
const token = (await page.locator('#new-token-value').innerText()).trim();
check('result state shows the PAT once', token.length > 0 && (await page.locator('#new-token-result').innerText()).includes('will not be shown again'));
check('new row appears without reload', (await page.locator('#tokens-list tr', { hasText: 'e2e-monitor' }).count()) === 1);
const call = async () => (await fetch(`${base}/api/v1/devices/sim/metrics/grid_power`, { headers: { Authorization: `Bearer ${token}` } })).status;
check('new PAT is accepted by the API', (await call()) === 200);
await page.click('#copy-token');
await page.waitForFunction(() => !document.querySelector('#token-done').disabled);
check('copying the PAT enables Done', true);
await page.click('#token-done');
await page.waitForSelector('#add-token-modal', { state: 'hidden' });
await page.waitForSelector('.modal-backdrop', { state: 'detached' });
check('closing the modal removes the PAT from the DOM', await page.evaluate((t) => !document.documentElement.outerHTML.includes(t) && !document.body.innerText.includes(t), token));
await page.click('button[aria-label="Revoke token e2e-monitor"]');
await page.waitForSelector('#confirm-modal.show');
await page.click('#confirm-accept');
await page.waitForFunction(() => !document.getElementById('tokens-list').textContent.includes('e2e-monitor'));
check('revoked PAT is rejected', (await call()) === 401);
check('empty state returns after the last token is revoked', (await page.locator('#tokens-list td[colspan="6"]').count()) === 1);

// 5. settings toggle + invalid autosave
await page.goto(base + '/ui/settings');
await page.waitForSelector('#setting-docs_public');

// 5b. cards in the same .settings-grid row share one height
const gridRows = async (selector) => page.evaluate((sel) => {
  const rows = new Map();
  for (const card of document.querySelectorAll(sel)) {
    const box = card.getBoundingClientRect();
    const key = Math.round(box.top);
    if (!rows.has(key)) rows.set(key, []);
    rows.get(key).push(Math.round(box.height));
  }
  return [...rows.values()];
}, selector);
let rows = await gridRows('.settings-grid > .card');
check('settings page renders more than one card per row', rows.some((r) => r.length > 1), JSON.stringify(rows));
check('settings cards in one row share the same height', rows.every((r) => Math.max(...r) - Math.min(...r) <= 1), JSON.stringify(rows));
const before = await page.isChecked('#setting-docs_public');
await page.locator('#setting-docs_public').setChecked(!before);
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('saved'), null, { timeout: 8000 });
check('docs toggle autosaves live', true, await toastText());
check('docs toggle active immediately', ((await fetch(`${base}/docs`)).status === 200) === !before);
await shot('settings-saved');
// Trust settings ask for confirmation before they autosave.
const invalidAutosaveStart = problems.length;
await page.locator('#setting-trusted_proxies').fill('not-an-ip');
await page.locator('#setting-trusted_proxies').dispatchEvent('change');
await page.waitForSelector('#confirm-modal.show');
await page.click('#confirm-accept');
await page.waitForFunction(() => document.querySelector('#toast-region .alert-danger'), null, { timeout: 8000 });
// fill() already fires a change event, so two saves may be in flight; wait for the final revert
await page.waitForFunction(() => document.querySelector('#setting-trusted_proxies')?.value === '', null, { timeout: 8000 }).catch(() => { });
check('invalid autosave shows an error and reverts', (await page.inputValue('#setting-trusted_proxies')) === '', JSON.stringify(await page.inputValue('#setting-trusted_proxies')) + ' ' + await toastText());
// The toast is the visibility floor; the persistent alert is what survives it. No Retry here: the
// per-key rollback already restored the committed value, so there is nothing left to resend.
await page.waitForSelector('#save-error-general', { timeout: 8000 });
const generalAlert = await page.locator('#save-error-general').innerText();
check('a failed scalar save leaves a persistent alert at the section', /was restored/.test(generalAlert), generalAlert);
check('the general alert offers no Retry', (await page.locator('#save-error-general button:has-text("Retry")').count()) === 0);
check('#save-state reads "Save failed" after a rejected save', (await page.locator('#save-state').innerText()).includes('Save failed'), await page.locator('#save-state').innerText());
// The rejected trusted_proxies save(s) answered 400 on purpose (one or two in flight).
scrubExpected(invalidAutosaveStart, expectedStatus(`${base}/admin/api/settings`, 400));

// 5c. a11y: an out-of-range number field marks itself and references a non-empty error message.
// An invalid value is never written to the draft, so nothing is saved and nothing has to be undone
// beyond putting the original value back into the input.
const bindPortBefore = await page.inputValue('#setting-bind_port');
await page.locator('#setting-bind_port').fill('10');
await page.locator('#setting-bind_port').dispatchEvent('change');
await sleep(200);
const fieldError = await page.evaluate(() => {
  const control = document.getElementById('setting-bind_port');
  const ids = (control.getAttribute('aria-describedby') || '').split(' ').filter(Boolean);
  return {
    invalid: control.getAttribute('aria-invalid'),
    ids,
    texts: ids.map((id) => document.getElementById(id)?.textContent.trim() || ''),
  };
});
check('an out-of-range number field sets aria-invalid', fieldError.invalid === 'true', JSON.stringify(fieldError));
check('the field error is referenced and non-empty', fieldError.ids.includes('setting-bind_port-error') && fieldError.texts.every(Boolean), JSON.stringify(fieldError));
// A later valid value clears the marking again.
await page.locator('#setting-bind_port').fill(bindPortBefore);
await page.locator('#setting-bind_port').dispatchEvent('change');
await sleep(300);
check('a valid value clears aria-invalid again', (await page.getAttribute('#setting-bind_port', 'aria-invalid')) === null);
await page.locator('#setting-log_level').selectOption('DEBUG');
await page.waitForSelector('#restart-notice:not([hidden])', { timeout: 8000 });
check('restart-required setting is reported', (await page.locator('#restart-notice').innerText()).includes('Log level'));
await shot('settings-restart-notice');
// 6. Prometheus page: remove, add, keyboard sort, drag and drop, persistence
await page.goto(base + '/ui/prometheus');
await page.waitForSelector('#exposed-list .metric-row');
const names = async () => page.$$eval('#exposed-list .metric-row', (rows) => rows.map((r) => r.dataset.name));
const initialNames = await names();
const initialCount = Number((await page.locator('#prometheus-count').innerText()).split(' ')[0]);
check('exposed table shows at most 15 defaults', initialNames.length >= 4 && initialNames.length <= 15, String(initialNames.length));
check('Prometheus summary shows the endpoint and count', (await page.locator('.prometheus-summary').innerText()).includes('/metrics') && (await page.locator('#prometheus-count').innerText()).includes('metrics'));
const removed = initialNames[0];
await page.click(`#exposed-list .metric-row[data-name="${removed}"] .metric-menu-toggle`);
await page.click(`#exposed-list button[aria-label="Remove: ${removed}"]`);
check('remove drops the item', !(await names()).includes(removed));
await page.waitForFunction(async (name) => !(await (await fetch('/admin/api/parameters', { cache: 'no-store' })).json()).exposed_names.includes(name), removed, { timeout: 8000 });
await page.click('#open-add-metrics');
await page.waitForSelector('#add-metrics-modal.show');
await page.fill('#parameter-search', removed);
await page.locator(`#available-list input[aria-label="Add ${removed}"]`).check();
await page.click('#confirm-add-metrics');
await page.waitForSelector('#add-metrics-modal', { state: 'hidden' });
check('add restores the metric in the editor', (await page.locator('#prometheus-count').innerText()) === `${initialCount} metrics`);
const second = (await names())[1];
await page.click(`#exposed-list .metric-row[data-name="${second}"] .metric-menu-toggle`);
await page.focus(`#exposed-list button[aria-label="Move up: ${second}"]`);
await page.keyboard.press('Enter');
check('keyboard move up reorders', (await names())[0] === second);
const focusedKept = await page.evaluate(() => document.activeElement?.getAttribute('aria-label') || '');
check('focus stays on the moved item button', focusedKept.includes(second), focusedKept);
const list = await names();
await page.evaluate(([src, dst]) => { // synthetic HTML5 drag events: headless Chromium does not start native drags reliably
  const dt = new DataTransfer();
  const row = (n) => document.querySelector(`#exposed-list .metric-row[data-name="${n}"]`);
  row(src).querySelector('.metric-drag-handle').dispatchEvent(new DragEvent('dragstart', { dataTransfer: dt, bubbles: true }));
  row(dst).dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true }));
  row(dst).dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}, [list.at(-1), list[0]]);
const afterDrag = await names();
check('drag and drop reorders', afterDrag[0] === list.at(-1), afterDrag.slice(0, 2).join(','));
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Parameters saved'), null, { timeout: 8000 });
check('parameter change reports restart need', (await toastText()).includes('restart'), await toastText());
await shot('prometheus-saved');
// #save-state keeps data-state="saved" after a successful save (it shows no text, the toast does),
// so wait for that state like the other save paths in this file do.
await page.waitForFunction(() => document.getElementById('save-state').dataset.state === 'saved', null, { timeout: 8000 });
check('added metric is persisted', (await page.evaluate(async () => (await (await fetch('/admin/api/parameters', { cache: 'no-store' })).json()).exposed_names)).includes(removed));
await page.reload();
await page.waitForSelector('#exposed-list .metric-row');
check('order persists after reload', JSON.stringify(await names()) === JSON.stringify(afterDrag));

// 6b. dashboard inverter modal and write access, TSDB: backend fields
await page.goto(base + '/ui/dashboard');
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
check('plus opens the inverter editor modal', (await page.locator('#inverters-modal.show').count()) === 1);
await page.waitForFunction(() => document.getElementById('inverters-modal').contains(document.activeElement));
check('focus stays in the modal', await page.evaluate(() => document.getElementById('inverters-modal').contains(document.activeElement)));
const deviceRows = () => page.locator('.device-settings-item').count();
const deviceField = (index, field) => page.locator('.device-settings-item').nth(index).locator(`input[data-field="${field}"]`);
const modalOpen = () => page.locator('#inverters-modal.show').count();
const rowState = (index) => page.evaluate((i) => {
  const row = document.querySelectorAll('.device-settings-item')[i];
  return {
    invalid: [...row.querySelectorAll('input.is-invalid')].map((input) => input.dataset.field),
    feedback: row.querySelector('.invalid-feedback').textContent,
    flag: row.querySelector('.device-row-flag').hidden ? '' : row.querySelector('.device-row-flag').textContent,
  };
}, index);
// A toast can sit over the bottom-right controls; closing it first keeps the click deterministic.
const clearToasts = () => page.evaluate(() => document.querySelectorAll('#toast-region .alert').forEach((node) => node.remove()));
const settleDevicePuts = async () => { await sleep(900); return devicePuts.length; }; // > the old 450 ms debounce
const barState = () => page.evaluate(() => ({ apply: !document.getElementById('device-apply').disabled }));

const serverDevices = async () => (await (await context.request.get(base + '/admin/api/settings')).json()).settings.devices;
const unloadWarns = () => page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; });
check('the dialog offers one empty row below the saved inverter', (await deviceRows()) === 2, String(await deviceRows()));
check('trash icon has an accessible label', (await page.locator('.device-settings-item').nth(0).locator('button[aria-label^="Remove inverter"] .material-icons').innerText()) === 'delete_outline');
check('the empty row has no remove button', (await page.locator('.device-settings-item').nth(1).locator('button').count()) === 0);
check('no id or name fields', (await page.locator('.device-settings-item input[data-field="device_id"], .device-settings-item input[data-field="display_name"]').count()) === 0);
let bar = await barState();
check('Apply is disabled while the draft is clean', !bar.apply, JSON.stringify(bar));
check('the action bar is the Apply changes button alone',
  (await page.locator('#device-apply').innerText()).trim() === 'Apply changes'
  && (await page.locator('.apply-bar button').count()) === 1
  && (await page.locator('.apply-bar .badge, .apply-bar p, #device-discard, #device-reset-warning').count()) === 0);
check('a clean draft shows no browser warning', !(await unloadWarns()));

// (b)-(d) invalid rows: field-level errors as before, Apply stays disabled, nothing is sent
const putsBefore = devicePuts.length;
await deviceField(1, 'host').fill('http://nope/path');
let state = await rowState(1);
check('invalid host is reported on the host field', state.invalid.join(',') === 'host' && state.feedback.includes('without scheme'), JSON.stringify(state));
bar = await barState();
check('an invalid draft keeps Apply disabled; the marked field says why', !bar.apply && (await rowState(1)).feedback.length > 0, JSON.stringify(bar));
await deviceField(1, 'host').fill('192.0.2.10');
await deviceField(1, 'port').fill('70000');
state = await rowState(1);
check('out-of-range port is reported on the port field, not the host', state.invalid.join(',') === 'port' && state.feedback.includes('65535'), JSON.stringify(state));
const savedHost = await deviceField(0, 'host').inputValue();
const savedPort = await deviceField(0, 'port').inputValue();
await deviceField(1, 'host').fill(savedHost);
await deviceField(1, 'port').fill(savedPort);
state = await rowState(1);
check('duplicate inverter is reported on the host field', state.invalid.join(',') === 'host' && state.feedback.includes('already listed'), JSON.stringify(state));
check('invalid edits sent no request', (await settleDevicePuts()) === putsBefore && (await modalOpen()) === 1);

// (e) a valid new row: draft only, marked, counted, no request even past the old debounce window
await deviceField(1, 'host').fill('192.0.2.10');
await deviceField(1, 'port').fill('18899');
await deviceField(1, 'port').dispatchEvent('change');
await deviceField(1, 'port').focus();
await page.keyboard.press('ArrowUp');
await page.keyboard.press('ArrowUp');
await page.keyboard.press('ArrowDown');
check('a changed field sends no request, not even after change events and spinner steps', (await settleDevicePuts()) === putsBefore);
state = await rowState(1);
bar = await barState();
check('the changed row is marked as unsaved', state.flag.includes('New') && state.flag.includes('unsaved'), JSON.stringify(state));
check('Apply is enabled for the unsaved row', bar.apply, JSON.stringify(bar));
check('leaving with an unsaved draft shows no browser warning', !(await unloadWarns()));
await deviceField(1, 'port').fill('18899');
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Inverters applied'), null, { timeout: 8000 });
check('Apply sends exactly one PUT with the new list', devicePuts.length === putsBefore + 1
  && devicePuts.at(-1).devices.length === 2 && devicePuts.at(-1).devices[1].host === '192.0.2.10' && devicePuts.at(-1).devices[1].port === 18899,
  JSON.stringify(devicePuts.slice(putsBefore)));
check('the PUT carries nothing but the device list', Object.keys(devicePuts.at(-1)).join(',') === 'devices');
check('Apply keeps the dialog open and the server has the new inverter', (await modalOpen()) === 1 && (await serverDevices()).some((d) => d.host === '192.0.2.10' && Number(d.port) === 18899));
bar = await barState();
check('after a successful apply the draft is the server state', !bar.apply && (await deviceRows()) === 3, JSON.stringify(bar));
check('no browser warning after apply', !(await unloadWarns()));
await shot('inverter-added');

// (f) Reloading the page drops an unsaved draft silently and sends nothing
const putsAtDiscard = devicePuts.length;
await deviceField(0, 'port').fill(String(Number(savedPort) + 1));
check('editing a saved row marks it as changed', (await rowState(0)).flag.includes('Changed'), JSON.stringify(await rowState(0)));
await page.reload();
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
check('reloading restores the server state and sends nothing', (await deviceField(0, 'port').inputValue()) === savedPort && (await settleDevicePuts()) === putsAtDiscard);

// (g) re-addressing needs a confirmation that names the reset; cancelling sends nothing
const extraRow = 1;
await deviceField(extraRow, 'port').fill('18898');
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
const confirmText = await page.locator('#confirm-modal .modal-body').innerText();
check('re-addressing asks for confirmation that names the reset', confirmText.includes('Verification evidence, Engineering Mode and operating mode') && confirmText.includes('192.0.2.10:18899'), confirmText);
check('the confirmation focuses the safe button', await page.waitForFunction(() => document.activeElement?.id === 'confirm-cancel', null, { timeout: 3000 }).then(() => true).catch(() => false));
await page.click('#confirm-cancel');
await page.waitForSelector('#confirm-modal.show', { state: 'detached', timeout: 5000 }).catch(() => { });
await sleep(300);
check('cancelling the confirmation sends nothing and keeps the draft', devicePuts.length === putsAtDiscard && (await barState()).apply);

// (h) a rejected apply keeps the draft, toasts once, shows the error and moves focus to it
const injectedApplyStart = problems.length;
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Injected failure' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
await page.click('#confirm-accept');
await page.waitForFunction(() => [...document.querySelectorAll('#toast-region .alert-danger')].some((n) => n.textContent.includes('Injected failure')), null, { timeout: 8000 });
bar = await barState();
check('a failed apply names the reason and keeps the draft', bar.apply && (await page.locator('#toast-region .alert-danger').first().innerText()).includes('kept'), JSON.stringify(bar));
check('a failed apply raises the danger toast exactly once',
  (await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('Injected failure')).length)) === 1);
check('focus returns to the Apply button after a failed apply', await page.evaluate(() => document.activeElement?.id === 'device-apply'));
check('the draft value survives the failed apply', (await deviceField(extraRow, 'port').inputValue()) === '18898');
await page.unroute('**/admin/api/settings');
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Device graph build failed' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
await page.click('#confirm-accept');
await page.waitForFunction(() => [...document.querySelectorAll('#toast-region .alert-danger')].some((n) => n.textContent.includes('rolled back')), null, { timeout: 8000 });
check('a 409 reports the rollback and keeps the draft', (await page.locator('#toast-region .alert-danger').first().innerText()).includes('Device graph build failed') && (await barState()).apply);
await page.unroute('**/admin/api/settings');
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 504, contentType: 'application/json', body: JSON.stringify({ detail: 'Reconfiguration timed out' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
await page.click('#confirm-accept');
await page.waitForFunction(() => [...document.querySelectorAll('#toast-region .alert-danger')].some((n) => n.textContent.includes('timed out')), null, { timeout: 8000 });
check('a 504 reports the timeout and keeps the draft', (await barState()).apply);
await page.unroute('**/admin/api/settings');
scrubExpected(injectedApplyStart, (p) => p.includes(`${base}/admin/api/settings`) &&
  (p.startsWith('http 500: PUT ') || p.startsWith('http 409: PUT ') || p.startsWith('http 504: PUT ')
    || (p.startsWith('console ') && /\b(500|409|504)\b/.test(p))));

// (i) the Apply button is locked while the request runs: no double submit
const putsAtApply = devicePuts.length;
await page.route('**/admin/api/settings', async (route) => {
  if (route.request().method() === 'PUT') await sleep(700);
  await route.continue();
});
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
await page.click('#confirm-accept');
await page.waitForFunction(() => document.getElementById('device-apply').disabled, null, { timeout: 3000 });
const lockedWhileSending = await page.evaluate(() => document.getElementById('device-apply').disabled && document.getElementById('device-apply').getAttribute('aria-busy') === 'true');
await page.locator('#device-apply').click({ force: true, timeout: 500 }).catch(() => { });
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Inverters applied'), null, { timeout: 8000 });
await page.unroute('**/admin/api/settings');
check('Apply is locked during the request and sends exactly one PUT', lockedWhileSending && devicePuts.length === putsAtApply + 1, String(devicePuts.length - putsAtApply));
check('the re-addressed inverter is on the server', (await serverDevices()).some((d) => d.host === '192.0.2.10' && Number(d.port) === 18898));

// (j) removing an inverter is a draft edit until Apply; it carries the same reset warning
await page.locator('.device-settings-item').nth(extraRow).locator('button').click();
check('Remove drops the row from the draft only', (await deviceRows()) === 2 && (await serverDevices()).some((d) => d.host === '192.0.2.10'));
bar = await barState();
check('a pending removal enables Apply', bar.apply, JSON.stringify(bar));
const putsAtRemove = devicePuts.length;
await clearToasts();
await page.click('#device-apply');
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
check('the confirmation names the reset and the removed inverter', /192\.0\.2\.10:18898/.test(await page.locator('#confirm-modal').innerText()) && /Engineering Mode/.test(await page.locator('#confirm-modal').innerText()));
await page.click('#confirm-accept');
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Inverters applied'), null, { timeout: 8000 });
check('Apply removes the inverter with exactly one PUT', devicePuts.length === putsAtRemove + 1 && devicePuts.at(-1).devices.length === 1
  && !(await serverDevices()).some((d) => d.host === '192.0.2.10'));

// (k) a draft survives closing and reopening the dialog
await deviceField(1, 'host').fill('192.0.2.55');
await page.click('#inverters-modal .btn-close');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
check('an unsaved draft survives closing the dialog', (await deviceField(1, 'host').inputValue()) === '192.0.2.55' && (await barState()).apply);
await page.reload();
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
check('reloading clears the leftover draft', (await deviceField(1, 'host').inputValue()) === '' && !(await barState()).apply);
// "Add another inverter" reuses the empty row and focuses it
await page.click('#device-add-row');
check('Add another inverter focuses the empty row without sending anything', await deviceField(1, 'host').evaluate((input) => document.activeElement === input) && (await deviceRows()) === 2);
await page.click('#inverters-modal .btn-close');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
// (g) no stray bottom-right Close button
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
await sleep(400); // let the Bootstrap fade transform finish before measuring the dialog's position below
check('the redundant modal-footer Close button is gone', (await page.locator('#inverters-modal .modal-footer .btn-secondary').count()) === 0);
// (h) centering: the dialog's bounding rect sits with near-equal left/right margins in the viewport.
// The outer #inverters-modal is itself a scroll container (Bootstrap's default), and an earlier
// invalid-field focus in this same test run can leave it scrolled; reset before measuring so the
// check reflects layout, not leftover scroll position from an unrelated prior step.
const centering = async () => page.evaluate(() => {
  document.getElementById('inverters-modal').scrollTop = 0;
  const rect = document.querySelector('#inverters-modal .modal-dialog').getBoundingClientRect();
  return { left: rect.left, right: window.innerWidth - rect.right, top: rect.top, bottom: window.innerHeight - rect.bottom };
});
let center = await centering();
check('the modal is horizontally centered at the default viewport', Math.abs(center.left - center.right) <= 2, JSON.stringify(center));
check('the modal is vertically centered at the default viewport', Math.abs(center.top - center.bottom) <= 2, JSON.stringify(center));
// (i) at a short viewport, .modal-body scrolls internally and shrinking the viewport while the
// modal is open does not make the page itself need any extra scroll height (the dashboard behind
// it may already be taller than 400px, which is unrelated and expected; the real regression this
// guards is the dialog's own box pushing the page even taller, which is what min-height:100%
// without accounting for the margin used to do).
const pageHeightAtDefaultViewport = await page.evaluate(() => document.documentElement.scrollHeight);
await page.setViewportSize({ width: 1280, height: 400 });
await sleep(200);
center = await centering();
check('the modal stays horizontally centered at a short viewport', Math.abs(center.left - center.right) <= 2, JSON.stringify(center));
const shortViewport = await page.evaluate(() => {
  const body = document.querySelector('#inverters-modal .modal-body');
  return { bodyScrolls: body.scrollHeight > body.clientHeight, pageHeight: document.documentElement.scrollHeight };
});
check('.modal-body scrolls internally at a short viewport', shortViewport.bodyScrolls, JSON.stringify(shortViewport));
check('shrinking the viewport does not grow the page past its default-viewport height', shortViewport.pageHeight <= pageHeightAtDefaultViewport, JSON.stringify({ ...shortViewport, pageHeightAtDefaultViewport }));
await page.setViewportSize({ width: 1280, height: 800 });
await sleep(200);
await page.click('#inverters-modal .btn-close');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show .device-settings-item input[data-field="host"]');
await page.waitForFunction(() => document.getElementById('inverters-modal').contains(document.activeElement));
await page.keyboard.press('Escape');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
await page.waitForFunction(() => document.activeElement?.id === 'add-inverter');
check('closing returns focus to the plus button', await page.evaluate(() => document.activeElement?.id === 'add-inverter'));
await page.goto(base + '/ui/inverters');
await page.waitForSelector('#setting-enable_write_support');
check('inverters page retains write access', (await page.locator('#setting-enable_write_support').count()) === 1);
check('inverter editor moved to the dashboard', (await page.locator('.device-settings').count()) === 0);
check('inverters page has the writable list', (await page.locator('#writable-list').count()) === 1);
check('exposed list is not on the inverters page', (await page.locator('#exposed-list').count()) === 0);
// The writable parameters use the same table, search, pager and row menu as the Prometheus page.
await page.waitForSelector('#writable-list .metric-row, #writable-list td');
check('writable table has the Prometheus pager wording', /^(\d+–\d+ of \d+|0 parameters)$/.test((await page.locator('#writable-range').innerText()).trim()));
check('writable table shows at most 15 rows', (await page.locator('#writable-list .metric-row').count()) <= 15);
await page.click('#open-add-writable');
await page.waitForSelector('#add-writable-modal.show #writable-available-list input[type="checkbox"]');
const grantee = await page.locator('#writable-available-list input[type="checkbox"]').first().getAttribute('value');
await page.locator('#writable-available-list input[type="checkbox"]').first().check();
await page.click('#confirm-add-writable');
await page.waitForSelector('#add-writable-modal', { state: 'hidden' });
await page.fill('#writable-search', grantee);
await page.waitForSelector(`#writable-list .metric-row[data-name="${grantee}"]`);
check('added parameter appears in the writable table', true);
await page.click(`#writable-list .metric-row[data-name="${grantee}"] .metric-menu-toggle`);
await page.click(`#writable-list button[aria-label="Remove write access: ${grantee}"]`);
await page.waitForFunction((name) => !document.querySelector(`#writable-list .metric-row[data-name="${name}"]`), grantee);
check('row menu removes write access', (await page.locator('#writable-list td.text-secondary').count()) === 1);
// Pager parity: Prometheus and Inverters share one table, so the same walk must hold on both
// (15 rows per page, "x–y of N", Previous disabled on the first and Next on the last page).
const PAGE_SIZE = 15;
async function pagerWalk(prefix) {
  const read = async () => ({
    range: (await page.locator(`#${prefix}-range`).innerText()).trim(),
    rows: await page.locator(`#${prefix}-list .metric-row`).count(),
    prevDisabled: await page.locator(`#${prefix}-prev`).isDisabled(),
    nextDisabled: await page.locator(`#${prefix}-next`).isDisabled(),
  });
  const forward = [await read()];
  while (!forward.at(-1).nextDisabled && forward.length < 100) { await page.click(`#${prefix}-next`); forward.push(await read()); }
  const backward = [forward.at(-1)];
  while (!backward.at(-1).prevDisabled && backward.length < 100) { await page.click(`#${prefix}-prev`); backward.push(await read()); }
  const total = Number(/ of (\d+)$/.exec(forward[0].range)?.[1]);
  const pages = Math.ceil(total / PAGE_SIZE);
  const expected = (k) => ({
    range: `${k * PAGE_SIZE + 1}–${Math.min((k + 1) * PAGE_SIZE, total)} of ${total}`, rows: Math.min(PAGE_SIZE, total - k * PAGE_SIZE),
    prevDisabled: k === 0, nextDisabled: k === pages - 1,
  });
  const walked = (states, order) => states.length === pages && states.every((s, i) => JSON.stringify(s) === JSON.stringify(expected(order(i))));
  return { total, pages, forwardOk: walked(forward, (i) => i), backwardOk: walked(backward, (i) => pages - 1 - i), forward };
}
await page.goto(base + '/ui/prometheus');
await page.waitForSelector('#exposed-list .metric-row');
const exposedPager = await pagerWalk('exposed');
check('Prometheus has more than one page of metrics', exposedPager.pages > 1, String(exposedPager.total));
check('Prometheus pager walks forward with the x–y of N wording and disabled ends', exposedPager.forwardOk, JSON.stringify(exposedPager.forward));
check('Prometheus pager walks back to the first page', exposedPager.backwardOk);
await page.goto(base + '/ui/inverters');
await page.waitForSelector('#writable-list td');
const initialGrants = await page.evaluate(async () => (await (await fetch('/admin/api/parameters', { credentials: 'same-origin' })).json()).write_names);
const initialRange = (await page.locator('#writable-range').innerText()).trim();
await page.click('#open-add-writable');
await page.waitForSelector('#add-writable-modal.show #writable-available-list input[type="checkbox"]');
const grantable = await page.locator('#writable-available-list input[type="checkbox"]').count();
const wanted = Math.min(grantable, 2 * PAGE_SIZE + 2);
for (let i = 0; i < wanted; i++) await page.locator('#writable-available-list input[type="checkbox"]').nth(i).check();
const grantSaved = page.waitForResponse((r) => r.url().endsWith('/admin/api/parameters') && r.request().method() === 'PUT');
await page.click('#confirm-add-writable');
await page.waitForSelector('#add-writable-modal', { state: 'hidden' });
await grantSaved;
// The "Parameters saved" toast sits over the pager until it leaves.
await page.waitForFunction(() => !document.getElementById('toast-region').children.length, null, { timeout: 15000 });
const writablePager = await pagerWalk('writable');
check('Inverters pager has enough parameters for three pages', writablePager.pages >= 3, `${writablePager.total} parameters`);
check('Inverters pager walks forward with the same wording and disabled ends as Prometheus', writablePager.forwardOk, JSON.stringify(writablePager.forward));
check('Inverters pager walks back to the first page', writablePager.backwardOk);
check('Prometheus and Inverters use the same page size', exposedPager.forward[0].rows === PAGE_SIZE && writablePager.forward[0].rows === PAGE_SIZE);
// Restore the grants this walk started with so later checks see the state they expect.
const restored = await page.evaluate(async (grants) => {
  const { csrf_token: csrf } = await (await fetch('/admin/api/session', { credentials: 'same-origin' })).json();
  const current = await (await fetch('/admin/api/parameters', { credentials: 'same-origin' })).json();
  return (await fetch('/admin/api/parameters', {
    method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify({ exposed_names: current.exposed_names, write_names: grants }),
  })).status;
}, initialGrants);
check('write grants restored after the pager walk', restored === 200, String(restored));
await page.goto(base + '/ui/inverters');
await page.waitForFunction(() => document.getElementById('writable-range')?.textContent.trim());
check('Inverters pager shows its original range after the restore', (await page.locator('#writable-range').innerText()).trim() === initialRange, initialRange);
await page.goto(base + '/ui/tsdb');
await page.waitForSelector('#setting-db_type');
await page.locator('#setting-metrics_export_enabled').click({ force: true });
check('disabled export hides every dependent TSDB section',
  (await page.locator('.export-card').count()) === 1
  && (await page.locator('#setting-db_type, #setting-metrics_export_interval_seconds, #tsdb-mini-console').count()) === 0);
await sleep(900);
check('the enable switch is not saved without Apply',
  (await (await context.request.get(base + '/admin/api/settings')).json()).settings.metrics_export_enabled === true
  && (await page.isEnabled('#export-apply')));
await page.locator('#setting-metrics_export_enabled').click({ force: true });
await page.waitForSelector('#setting-db_type');
check('switching back to the saved state leaves nothing to apply', await page.isDisabled('#export-apply'));
await page.locator('#setting-db_type').selectOption('questdb');
check('tsdb page shows backend fields', (await page.locator('#setting-questdb_hostname').count()) === 1);
check('docs toggle lives on settings only', (await page.locator('#setting-docs_public').count()) === 0);

// A success toast sits bottom right, over the Apply button, and a pointer resting on it pauses its countdown;
// so release the pointer and let earlier toasts leave before clicking, as a user would.
const clickExportApply = async () => {
  await page.mouse.move(0, 0);
  await page.waitForFunction(() => document.querySelectorAll('#toast-region .alert').length === 0, null, { timeout: 15000 });
  await page.click('#export-apply');
};
// 6c. TSDB export cards: all cards visible in the shared grid; changes are sent by Apply only
const tsdbPuts = [];
page.on('request', (r) => { if (r.method() === 'PUT' && r.url().endsWith('/admin/api/settings')) tsdbPuts.push(r.postData()); });
await sleep(200);
check('questdb backend shows all five export cards', (await page.locator('#settings-sections > .export-card').count()) === 5);
check('retention lives in Export settings', (await page.locator('.export-card-export #setting-questdb_downsampling').count()) === 1);
check('TSDB page has a runtime mini console', (await page.locator('#tsdb-mini-console').count()) === 1);
check('TSDB status lives in the Status & Log console, which announces it politely',
  (await page.locator('#tsdb-mini-console').innerText()).trim().length > 0
  && (await page.locator('#tsdb-mini-console').getAttribute('role')) === 'log'
  && (await page.locator('#tsdb-status-badge').count()) === 0);
check('no explicit Save button on the TSDB page', (await page.locator('.export-card button, #settings-sections button:has-text("Save")').count()) === 0);
// The cards sit in the shared settings grid: cards of one row end at the same height.
const exportRows = await page.evaluate(() => [...document.querySelectorAll('#settings-sections > .export-card')].map((card) => {
  const box = card.getBoundingClientRect();
  return { top: Math.round(box.top), bottom: Math.round(box.bottom) };
}));
check('export cards of one grid row share top and bottom edge',
  exportRows.length === 5 && exportRows.every((row) => row.top !== exportRows[0].top || row.bottom === exportRows[0].bottom),
  JSON.stringify(exportRows));
await page.locator('#setting-questdb_downsampling').selectOption('manual');
check('manual retention shows raw days but hides total days',
  (await page.locator('#setting-questdb_raw_retention_days').count()) === 1
  && (await page.locator('#setting-questdb_retention_days').count()) === 0);
await page.locator('#setting-questdb_downsampling').selectOption('off');
check('off retention shows total days but hides raw days',
  (await page.locator('#setting-questdb_retention_days').count()) === 1
  && (await page.locator('#setting-questdb_raw_retention_days').count()) === 0);

// Nothing is sent while typing; Apply is disabled until the backend's required fields are complete
// (questdb needs only the hostname).
await sleep(500);
check('incomplete backend config is not sent', (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type !== 'questdb' && tsdbPuts.length === 0);
check('Apply stays disabled while a required field is missing', await page.isDisabled('#export-apply'));
await page.locator('#setting-questdb_hostname').fill('questdb.example.org');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await sleep(900);
let settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('a complete form is still not saved automatically', settingsNow.questdb_hostname !== 'questdb.example.org' && tsdbPuts.length === 0, JSON.stringify(settingsNow.questdb_hostname));
check('Apply is enabled and is the only element of the action bar',
  (await page.isEnabled('#export-apply'))
  && (await page.locator('#export-actions button').count()) === 1
  && (await page.locator('#export-apply').innerText()).trim() === 'Apply changes'
  && (await page.locator('#export-actions .badge, #export-actions p, #export-discard').count()) === 0);
await clickExportApply();
await page.waitForFunction(() => document.getElementById('save-state').dataset.state === 'saved', null, { timeout: 8000 });
settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('Apply sends the whole group exactly once', tsdbPuts.length === 1 && settingsNow.db_type === 'questdb' && settingsNow.questdb_hostname === 'questdb.example.org', JSON.stringify(settingsNow.questdb_hostname) + ` puts=${tsdbPuts.length}`);
check('Apply is disabled again once the draft equals the server state', await page.isDisabled('#export-apply'));
await page.locator('#setting-questdb_tls_enabled').click({ force: true });
await sleep(900);
settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('a toggle is not saved until Apply', settingsNow.questdb_tls_enabled !== true && tsdbPuts.length === 1);
await clickExportApply();
await page.waitForFunction(() => document.getElementById('save-state').dataset.state === 'saved', null, { timeout: 8000 });
settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('Apply saves the toggle', settingsNow.questdb_tls_enabled === true);
await page.locator('#setting-questdb_downsampling').selectOption('low');
await clickExportApply();
await page.waitForFunction(() => document.getElementById('save-state').dataset.state === 'saved', null, { timeout: 8000 });
if ((await page.locator('html').getAttribute('data-bs-theme')) !== 'dark') await page.locator('#theme-toggle').click();
for (const [width, height] of [[1920, 1080], [1536, 864]]) {
  await page.setViewportSize({ width, height });
  const fit = await page.evaluate(() => ({
    vertical: document.documentElement.scrollHeight - window.innerHeight,
    horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  check(`TSDB configuration fits ${width}x${height} without page scroll`, fit.vertical <= 2 && fit.horizontal <= 0, JSON.stringify(fit));
}
await page.setViewportSize({ width: 1280, height: 800 });

// 6c-2. a configuration that becomes incomplete again can neither be applied nor leak out: Apply is
// disabled, the hint names what is missing, and the saved value stays untouched.
const dbTypeBefore = (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type;
const putsBeforeIncomplete = tsdbPuts.length;
await page.locator('#setting-questdb_hostname').fill('');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await sleep(900);
let exportState = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('an export config that became incomplete again is not sent', exportState.questdb_hostname === 'questdb.example.org' && tsdbPuts.length === putsBeforeIncomplete, JSON.stringify(exportState.questdb_hostname));
check('Apply is disabled for an incomplete export group', await page.isDisabled('#export-apply'));
check('#save-state reports the incomplete export group', (await page.locator('#save-state').innerText()).includes('Incomplete'), await page.locator('#save-state').innerText());
check('a warning toast names what is missing, once', /Not saved yet: Hostname or URL is required/.test(await page.locator('#toast-region').innerText())
  && (await page.locator('#toast-region .alert-warning').count()) === 1);
check('the emptied required field is flagged on the field', (await page.getAttribute('#setting-questdb_hostname', 'aria-invalid')) === 'true'
  && (await page.locator('#save-hint-export').count()) === 0);
// The pairing branch: every required key is present, so the message has to name the pair instead.
await page.locator('#setting-questdb_hostname').fill('questdb.example.org');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await page.locator('#setting-questdb_username').fill('metrics');
await page.locator('#setting-questdb_username').dispatchEvent('change');
await sleep(900);
exportState = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('an unpaired username is withheld instead of producing a server 400', !exportState.questdb_username && await page.isDisabled('#export-apply'), JSON.stringify(exportState.questdb_username));
check('the incomplete hint names the pair, not a missing required field', /must be set together or left empty/.test(await page.locator('#toast-region').innerText()), await page.locator('#toast-region').innerText());
await page.locator('#setting-questdb_username').fill('');
await page.locator('#setting-questdb_username').dispatchEvent('change');
await sleep(400);
check('Apply has nothing to send once the draft is back at the saved state', await page.isDisabled('#export-apply'));
check('db_type survived the withheld export changes', (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type === dbTypeBefore, dbTypeBefore);
await page.locator('#setting-db_type').selectOption('influxdb_v2');
await sleep(200);
check('influxdb backend shows all five export cards', (await page.locator('#settings-sections > .export-card').count()) === 5);
check('still no Save button after switching backend', (await page.locator('.export-card button').count()) === 0);

// 6c-3. a value typed while the apply is in flight must survive the response, which clears only the
// secret revision it actually confirmed.
await page.fill('#setting-influxdb_hostname', 'influx.example.org');
await page.locator('#setting-influxdb_hostname').dispatchEvent('change');
await page.fill('#setting-influxdb_organization', 'rct');
await page.locator('#setting-influxdb_organization').dispatchEvent('change');
await page.fill('#setting-influxdb_bucket', 'metrics');
await page.locator('#setting-influxdb_bucket').dispatchEvent('change');
// Settings._check_export() counts an InfluxDB token as credentials that are always present, so
// _guard_plaintext() rejects the entire save for a non-loopback host without TLS unless plaintext
// credentials are explicitly allowed.
await page.locator('#setting-influxdb_allow_plaintext_credentials').check();
await page.waitForSelector('#confirm-modal.show');
await page.click('#confirm-accept');
// Only the first PUT is held, so a second value can be typed while its response is outstanding.
let settingsPuts = 0;
await page.route('**/admin/api/settings', async (route) => {
  if (route.request().method() !== 'PUT') return route.continue();
  settingsPuts += 1;
  if (settingsPuts === 1) await sleep(1200);
  return route.continue();
});
await page.fill('#setting-influxdb_token', 'token-one');
await page.locator('#setting-influxdb_token').dispatchEvent('change');
check('an incomplete-free group with a token can be applied', await page.isEnabled('#export-apply'));
await clickExportApply();
await sleep(600);                          // the request is in flight, its response is still held
await page.fill('#setting-influxdb_token', 'token-two');
await page.locator('#setting-influxdb_token').dispatchEvent('change');
await page.waitForFunction(() => document.getElementById('export-apply').getAttribute('aria-busy') === 'false', null, { timeout: 15000 });
check('a token typed during the apply is not cleared by the older response',
  (await page.inputValue('#setting-influxdb_token')) === 'token-two' && await page.isEnabled('#export-apply'),
  `puts=${settingsPuts} value=${await page.inputValue('#setting-influxdb_token')}`);
await clickExportApply();
const tokenCleared = await page.waitForFunction(() => {
  const field = document.getElementById('setting-influxdb_token');
  return field.value === '' && field.placeholder.endsWith('(stored)');
}, null, { timeout: 8000 }).then(() => true).catch(() => false);
check('the newer token is sent by the second apply and then cleared', tokenCleared && settingsPuts === 2
  && (await page.getAttribute('#setting-influxdb_token', 'placeholder')).endsWith('(stored)'), `puts=${settingsPuts}`);
check('a blank token keeps the stored one: nothing left to apply', await page.isDisabled('#export-apply'));
await page.unroute('**/admin/api/settings');

// Measured geometry: on a phone the shared settings grid collapses to one column and every card
// is as wide as the grid.
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await sleep(200);
  const mobileGrid = await page.evaluate(() => {
    const grid = document.querySelector('#settings-sections');
    const gridBox = grid.getBoundingClientRect();
    const cards = [...grid.querySelectorAll(':scope > .export-card')].map((card) => card.getBoundingClientRect());
    return {
      cardCount: cards.length,
      columns: new Set(cards.map((card) => Math.round(card.left))).size,
      widestDelta: Math.max(...cards.map((card) => Math.abs(card.width - gridBox.width))),
    };
  });
  check(`export cards collapse to one column on small screens at ${width}px`,
    mobileGrid.cardCount >= 2 && mobileGrid.columns === 1 && mobileGrid.widestDelta <= 1, JSON.stringify(mobileGrid));
}
await page.setViewportSize({ width: 1280, height: 800 });

await page.goto(base + '/ui/prometheus');
// The Scrape settings collapsible is gone: the master toggle is present directly, no summary to expand.
await page.waitForSelector('#setting-enable_metrics_endpoint');
check('no Scrape settings collapsible on the Prometheus page', (await page.locator('.prometheus-settings, summary:has-text("Scrape settings")').count()) === 0);

// 6d. The master toggle is the first element inside the top status card, above the status grid.
const toggleFirstInTopCard = await page.evaluate(() => {
  const card = document.querySelector('section.card[aria-label="Prometheus status"] > .card-body');
  const toggle = document.getElementById('setting-enable_metrics_endpoint');
  if (!card || !toggle) return false;
  const first = card.firstElementChild;
  // The toggle lives in the first child of the card body, which precedes the status grid.
  return first.contains(toggle) && !first.classList.contains('prometheus-summary')
    && Boolean(card.querySelector('.prometheus-summary'))
    && (first.compareDocumentPosition(card.querySelector('.prometheus-summary')) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
});
check('Enable Metrics Endpoint toggle is first inside the top status card', toggleFirstInTopCard);

// 6e. Prometheus master toggle gates the dependent fields live, without a reload
const DEPENDENT = ['metrics_require_token', 'metrics_trusted_sources', 'metrics_rate_limit_requests', 'metrics_rate_limit_window_seconds'];
const dependentCount = () => page.locator(DEPENDENT.map((key) => `#setting-${key}`).join(', ')).count();
check('master toggle is labelled "Enable Metrics Endpoint"', (await page.locator('label[for="setting-enable_metrics_endpoint"]').innerText()).trim() === 'Enable Metrics Endpoint', await page.locator('label[for="setting-enable_metrics_endpoint"]').innerText());
if (!(await page.isChecked('#setting-enable_metrics_endpoint'))) {
  await page.locator('#setting-enable_metrics_endpoint').setChecked(true);
  await sleep(600);
}
check('dependent metrics fields visible while the toggle is on', (await dependentCount()) === 4, String(await dependentCount()));
await page.locator('#setting-enable_metrics_endpoint').setChecked(false);
await sleep(600);
check('dependent metrics fields hidden when the toggle is off (no reload)', (await dependentCount()) === 0, String(await dependentCount()));
// The exposed selection also drives the periodic reads and the TSDB push, so that card stays.
check('exposed metrics card stays visible with the endpoint off', (await page.locator('#exposed-list').count()) === 1);
await page.locator('#setting-enable_metrics_endpoint').setChecked(true);
await sleep(600);
check('dependent metrics fields come back when the toggle is on', (await dependentCount()) === 4, String(await dependentCount()));
await shot('prometheus-master-toggle-on');

// 6e. Energy Manager smoke on its own server: write support and the dispatch store are enabled there,
// the main server above runs without them. Only behaviour is asserted, not timing, colours or paths.
{
  const energyDb = path.join(dbDir, 'energy', 'e2e.db');
  fs.mkdirSync(path.dirname(energyDb), { recursive: true });
  const energyPort = await freePort();
  // Its own simulated inverter: two app instances on one device would be competing clients.
  const energyDevicePort = await freePort();
  const energyFake = spawnPy(['tests.e2e.fake_inverter', String(energyDevicePort)], path.join(OUT, 'fake-energy.log'));
  const spawned = spawnPy(['tests.e2e.run_server', energyDb, String(energyPort), String(energyDevicePort), 'energy'], path.join(OUT, 'server-energy.log'));
  try {
    await waitForPort(energyDevicePort);
    const energyBase = `http://127.0.0.1:${energyPort}`;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(`${energyBase}/health`)).ok) break; } catch { /* not up yet */ }
      await sleep(200);
    }
    const energyPassword = firstStartPassword(spawned.output());
    const energyContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-GB' });
    const ep = await energyContext.newPage();
    const energyProblems = [];
    trackProblems(ep, energyProblems);
    await ep.goto(`${energyBase}/login`);
    await ep.fill('#password', energyPassword);
    await ep.click('#login-form button[type=submit]');
    await ep.waitForURL('**/change-password');
    await ep.fill('#current-password', energyPassword);
    await ep.fill('#new-password', NEW_PASSWORD);
    await ep.fill('#confirm-password', NEW_PASSWORD);
    await ep.click('#change-password-form button[type=submit]');
    await ep.waitForURL('**/ui/dashboard');

    // Measured values for the panel's own readings-derived text (e.g. the battery state sentence);
    // the simulator's own readings are not part of this contract.
    const reading = (value) => ({ value, age_seconds: 1, stale: false });
    let mockNoLimits = false;
    await ep.route('**/admin/api/energy/devices', async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch();
      const list = await response.json();
      for (const device of list) {
        if (mockNoLimits) device.limits = null;
        device.readings = {
          battery_soc_percent: reading(54), grid_power_w: reading(1200), pv_power_w: reading(1500),
          house_load_w: reading(800), battery_power_w: reading(-900),
        };
      }
      await route.fulfill({ response, json: list });
    });

    // Before any verification: the device ships unverified, so Operate must show the Setup checklist
    // and its single "Complete setup"/"Set up manual control" CTA — and must NOT leak raw capability
    // names (write_path_convention etc.) onto the Operate surface (requirement 5, design §9/§12).
    await ep.goto(`${energyBase}/ui/energy`);
    await ep.waitForSelector('.energy-panel');
    await ep.waitForSelector('.energy-setup:not([hidden])', { timeout: 15000 }).catch(() => { });
    check('Setup checklist is shown while hardware is unverified', await ep.locator('.energy-setup:not([hidden])').count() >= 1);
    // Wait until the inverter has connected, so the §9 branch order (connection first) advances from
    // "Not connected." to the hardware-unverified "needs setup" banner.
    await ep.waitForFunction(() => {
      const list = document.querySelector('.energy-setup-list');
      const connectedRow = list && list.querySelector('.energy-setup-item.is-met');
      return connectedRow && /Inverter connected/.test(connectedRow.textContent);
    }, null, { timeout: 20000 }).catch(() => { });
    const setupText = await ep.locator('.energy-panel').first().innerText();
    check('a single guided setup block is shown, without the old duplicated "needs setup" line',
      /Manual battery control setup/.test(setupText) && !/Manual control needs setup/.test(setupText) && !/Complete setup/.test(setupText), setupText.slice(0, 500));
    check('Expert mode exists and is OFF on load',
      (await ep.locator('#energy-expert-mode').isChecked()) === false && (await ep.locator('.energy-expert').count()) === 0);
    check('Basic mode has no diagnostics, engineering mode or SoC policy',
      (await ep.locator('.energy-diagnostics').count()) === 0 && !/Engineering mode|SoC target policy|Revoke verification/.test(setupText), setupText.slice(0, 500));
    const shownStep = await ep.locator('.energy-setup-step').first().innerText();
    check('only the first open setup step is shown, without engineering mode',
      /Write access|Power limits|Hardware verification/.test(shownStep) && !/Engineering mode/.test(shownStep), shownStep.slice(0, 300));
    check('no raw capability names leak onto the Operate surface',
      !/write_path_convention|battery_power_sign_convention|grid_power_sign_convention/.test(setupText), setupText.slice(0, 500));

    // Manual may be selected while the setup is open, but it must read as pending, not as active.
    const manualAnswered = ep.waitForResponse((r) => r.request().method() === 'PUT' && r.url().endsWith('/mode'), { timeout: 15000 }).catch(() => null);
    await ep.locator('.energy-mode-switch [role=radio]').nth(1).click();
    const manualResponse = await manualAnswered;
    if (manualResponse && manualResponse.ok()) {
      await ep.waitForSelector('.energy-mode-option.is-selected.is-pending', { timeout: 15000 }).catch(() => { });
      const lead = ep.locator('.energy-setup-lead:not([hidden])');
      check('Manual with an open setup is marked pending and says it is not active yet',
        (await ep.locator('.energy-mode-option.is-selected.is-pending').count()) === 1
        && /not active yet/.test(await lead.first().innerText().catch(() => '')));
      const offAnswered = ep.waitForResponse((r) => r.request().method() === 'PUT' && r.url().endsWith('/mode'), { timeout: 15000 });
      await ep.locator('.energy-mode-switch [role=radio]').first().click();
      await offAnswered;
      await ep.waitForSelector('.energy-mode-option.is-selected:not(.is-pending)', { timeout: 15000 });
      check('Off is never shown as pending', (await ep.locator('.energy-setup-lead:not([hidden])').count()) === 0);
    } else {
      check('Manual can be selected while the setup is open', false, String(manualResponse && manualResponse.status()));
    }

    // The verification form must never pre-select the sign conventions or the evidence checkboxes.
    const verifyPuts = [];
    ep.on('request', (r) => { if (r.method() === 'PUT' && r.url().endsWith('/hardware-verification')) verifyPuts.push(r.postDataJSON()); });
    // Basic mode only points to the verification (no strategy code / byte widths); the form is Expert-only
    // and Expert mode is switched on by the user, never by the setup step.
    check('Basic mode shows no hardware verification form or protocol fields',
      (await ep.locator('.energy-setup-step form').count()) === 0 && !/Strategy code|Byte width/.test(shownStep), shownStep.slice(0, 300));
    if (/Hardware verification/.test(shownStep)) {
      check('the Basic hardware step offers "Verify hardware" and leaves Expert mode off',
        (await ep.locator('.energy-setup-step button', { hasText: 'Verify hardware' }).count()) === 1
        && (await ep.locator('#energy-expert-mode').isChecked()) === false, shownStep.slice(0, 300));
      // Shared primary metrics and AA contrast on the warning box; this context is light, dark brand
      // fill vs surface is a global token matter.
      const buttonStyle = await ep.locator('.energy-setup-step button', { hasText: 'Verify hardware' }).evaluate((button) => {
        const probe = document.createElement('button');
        probe.className = 'btn btn-primary';
        probe.textContent = 'Probe';
        button.after(probe);
        const luminance = (color) => {
          const [r, g, b] = color.match(/[\d.]+/g).slice(0, 3).map((v) => { const c = Number(v) / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
          return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        };
        const ratio = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
        const metrics = (el) => { const s = getComputedStyle(el); return [s.fontSize, s.fontWeight, s.lineHeight, s.padding, s.borderRadius, s.display, s.backgroundColor, el.getBoundingClientRect().height.toFixed(1)].join('|'); };
        let surface = 'rgba(0, 0, 0, 0)';
        for (let node = button.parentElement; node && /rgba\(0, 0, 0, 0\)|transparent/.test(surface); node = node.parentElement) surface = getComputedStyle(node).backgroundColor;
        const own = getComputedStyle(button);
        const result = { button: metrics(button), canonical: metrics(probe), text: ratio(own.color, own.backgroundColor), boundary: ratio(own.backgroundColor, surface) };
        probe.remove();
        return result;
      });
      check('"Verify hardware" has the computed style of the shared primary button',
        buttonStyle.button === buttonStyle.canonical, `${buttonStyle.button} vs ${buttonStyle.canonical}`);
      check('"Verify hardware" is readable on the setup box (text 4.5:1, boundary 3:1)',
        buttonStyle.text >= 4.5 && buttonStyle.boundary >= 3, `text ${buttonStyle.text.toFixed(2)}, boundary ${buttonStyle.boundary.toFixed(2)}`);
      // Guided assistant: cancelling at the consent step stores nothing and keeps the gate closed.
      await ep.locator('.energy-setup-step button', { hasText: 'Verify hardware' }).click();
      const dialog = ep.locator('.energy-assistant.show');
      await dialog.waitFor({ timeout: 5000 });
      check('"Verify hardware" opens the guided assistant without switching Expert mode on',
        (await ep.locator('#energy-expert-mode').isChecked()) === false && /Before you start/.test(await dialog.innerText()));
      await dialog.getByRole('button', { name: 'Start check' }).click();
      await dialog.getByRole('button', { name: 'Yes, continue' }).click();
      await dialog.getByRole('button', { name: /The battery is discharging/ }).click();
      await dialog.getByRole('button', { name: /feeding power into the grid/ }).click();
      check('the assistant reaches the test step and offers no register names',
        /Run the short test/.test(await dialog.innerText()) && !/power_mng|Strategy code|Byte width/.test(await dialog.innerText()));
      const runButton = dialog.getByRole('button', { name: 'Run test' });
      check('the test button stays disabled until the operator consents', await runButton.isDisabled());
      await dialog.getByRole('button', { name: 'Cancel' }).click();
      await ep.waitForSelector('.energy-assistant', { state: 'detached', timeout: 5000 });
      check('cancelling the assistant leaves the hardware unverified',
        (await ep.locator('.energy-setup-step button', { hasText: 'Verify hardware' }).count()) === 1
        && (await ep.locator('.energy-actions button').count()) >= 0 && (await ep.locator('.energy-setup:not([hidden])').count()) >= 1);
      // The Expert form checks below need the Expert section.
      await ep.locator('#energy-expert-mode').check();
    }
    const uiForm = ep.locator('.energy-expert form').filter({ hasText: 'Device model' });
    if (await uiForm.count()) {
      const blank = await uiForm.evaluate((form) => ({
        signs: [...form.querySelectorAll('select')].map((select) => select.value),
        checked: [...form.querySelectorAll('input[type=checkbox]')].filter((box) => box.checked).length,
      }));
      check('the verification form starts with empty sign selects and no evidence boxes ticked',
        blank.signs.length === 2 && blank.signs.every((value) => value === '') && blank.checked === 0, JSON.stringify(blank));
      await uiForm.getByLabel('Device model').fill('Simulator');
      await uiForm.getByLabel('Firmware').fill('1.0');
      await uiForm.getByLabel('Strategy code').fill('2');
      await uiForm.getByLabel('Enum byte width').fill('1');
      await uiForm.getByLabel('Bool byte width').fill('1');
      await uiForm.getByLabel('Evidence note').fill('e2e form');
      await uiForm.getByLabel('Write frame layout verified').check();
      await uiForm.getByLabel('Apply sequence verified').check();
      await uiForm.getByLabel('I verified these values on the hardware').check();
      await uiForm.locator('button[type=submit]').click();
      await sleep(500);
      check('submitting without choosing the sign conventions is rejected on the field',
        verifyPuts.length === 0 && (await uiForm.locator('.is-invalid').count()) >= 1, JSON.stringify(verifyPuts));
    }

    // Complete guided run, back in Basic mode so the Expert form state does not interfere.
    await ep.locator('#energy-expert-mode').uncheck();
    {
      const dialog = ep.locator('.energy-assistant.show');
      // Consent, test, save; the banner goes away and the manual actions appear.
      await ep.locator('.energy-setup-step button', { hasText: 'Verify hardware' }).click();
      await dialog.waitFor({ timeout: 5000 });
      await dialog.getByRole('button', { name: 'Start check' }).click();
      await dialog.getByRole('button', { name: 'Yes, continue' }).click();
      await dialog.getByRole('button', { name: /The battery is discharging/ }).click();
      await dialog.getByRole('button', { name: /feeding power into the grid/ }).click();
      await dialog.getByLabel(/I understand the battery will pause/).check();
      await dialog.getByRole('button', { name: 'Run test' }).click();
      await dialog.getByRole('button', { name: 'Save verification' }).waitFor({ timeout: 60000 });
      await dialog.getByRole('button', { name: 'Save verification' }).click();
      await ep.waitForSelector('.energy-assistant', { state: 'detached', timeout: 10000 });
      await ep.waitForSelector('.energy-setup', { state: 'hidden', timeout: 15000 });
      check('after the guided verification the setup banner is gone and Expert mode is still off',
        (await ep.locator('.energy-setup:not([hidden])').count()) === 0 && (await ep.locator('#energy-expert-mode').isChecked()) === false);
      check('the manual actions are visible after the guided verification',
        (await ep.locator('.energy-actions button:visible').count()) >= 3);
    }
    // A fresh dispatch store ships unverified hardware; verify it through the atomic admin endpoint.
    const verified = await ep.evaluate(async () => {
      const { csrf_token: csrf } = await (await fetch('/admin/api/session', { credentials: 'same-origin' })).json();
      const response = await fetch('/admin/api/energy/devices/sim/hardware-verification', {
        method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({
          verified_device_model: 'Simulator', verified_firmware: '1.0', note: 'e2e', soc_strategy_external_code: 2,
          enum_byte_width: 1, bool_byte_width: 1, write_frame_layout_verified: true, apply_sequence_verified: true,
          battery_discharge_positive: true, grid_import_positive: true, soc_target_unit: 'ratio',
        }),
      });
      return response.status;
    });
    check('the hardware verification endpoint accepts one complete attestation', verified === 200, String(verified));
    await ep.goto(`${energyBase}/ui/energy`);
    await ep.waitForSelector('.energy-panel');
    check('energy page renders a device panel', (await ep.locator('.energy-panel').count()) >= 1);
    // Stage 1 moved the flow graphic to the dashboard and removed the Energy page's own instance
    // (energyFlowGraphic() is now shared, single-instance); the panel keeps its control column.
    check('the Energy Manager panel no longer contains its own flow graphic', (await ep.locator('.energy-panel .energy-flow-svg').count()) === 0);
    // The relabelled, Operate-layer action buttons (presentation only; REST names unchanged).
    const buttons = ['Charge battery', 'Keep battery idle', 'Discharge battery']
      .map((name) => ep.locator('.energy-actions button', { hasText: new RegExp(`^${name}$`) }));
    const states = async () => Promise.all(buttons.map((button) => button.isDisabled()));
    const operateText = async () => (await ep.locator('.energy-panel').first().innerText());

    const commands = [];
    ep.on('request', (r) => { if (r.method() === 'POST' && r.url().endsWith('/command')) commands.push(r.postDataJSON()); });
    // The simulator needs ~10 s to apply a charge, and the buttons stay busy until then (waits below allow for it).
    // The simulator ships unverified hardware; verify it first, as an operator would in Expert.
    const csrf = await ep.evaluate(async () => (await (await fetch('/admin/api/session')).json()).csrf_token);
    const verification = await ep.request.put(`${energyBase}/admin/api/energy/devices/sim/hardware-verification`, {
      headers: { 'X-CSRF-Token': csrf, Origin: energyBase },
      data: {
        verified_device_model: 'Simulator', verified_firmware: '1.0', note: 'e2e simulator',
        soc_strategy_external_code: 2, enum_byte_width: 1, bool_byte_width: 1,
        write_frame_layout_verified: true, apply_sequence_verified: true,
        battery_discharge_positive: true, grid_import_positive: true, soc_target_unit: 'ratio',
      },
    });
    check('hardware verification endpoint accepts the evidence', verification.ok(), String(verification.status()));
    await ep.reload();
    await ep.waitForSelector('.energy-panel');
    // Operating mode: a three-state radio group. Off is the initial state; Write access is already on here.
    const radios = ep.locator('.energy-mode-switch [role=radio]');
    const checkedMode = () => ep.evaluate(() => document.querySelector('.energy-mode-switch [aria-checked=true]')?.textContent.trim());
    check('the mode control is a radio group with Off, Manual and External',
      (await ep.locator('.energy-mode-switch[role=radiogroup]').count()) === 1
      && (await radios.allInnerTexts()).map((text) => text.replace(/\s+/g, ' ').trim().replace(/^\S+ /, '')).join('|') === 'Off|Manual|External',
      JSON.stringify(await radios.allInnerTexts()));
    check('initially Off is selected and the only tab stop', /Off$/.test(await checkedMode()),
      String(await checkedMode()));
    check('roving tabindex: exactly one radio is tabbable', (await ep.locator('.energy-mode-switch [role=radio][tabindex="0"]').count()) === 1);
    check('every radio has an aria-label that starts with its visible text',
      (await radios.evaluateAll((nodes) => nodes.every((n) => n.getAttribute('aria-label').startsWith(n.lastElementChild.textContent.trim())))));
    const initialStates = await states();
    check('Off: the operate buttons are disabled and name the reason', initialStates.every(Boolean)
      && /switched off/.test(await buttons[0].getAttribute('title') || ''), JSON.stringify(initialStates));
    // Keyboard: arrows only move the focus; Space selects.
    const modeCalls = [];
    // Synchronise on the PUT /mode answer, then on the rendered state; a timeout fails the step loudly.
    const switchMode = async (act, label) => {
      const answered = ep.waitForResponse((r) => r.request().method() === 'PUT' && r.url().endsWith('/mode'), { timeout: 15000 });
      await act();
      await answered;
      await ep.waitForFunction((l) => (document.querySelector('.energy-mode-switch [aria-checked=true]')?.textContent.trim() || '').endsWith(l), label, { timeout: 15000 });
    };
    ep.on('request', (r) => { if (r.method() === 'PUT' && r.url().endsWith('/mode')) modeCalls.push(r.postDataJSON()); });
    await radios.first().focus();
    await ep.keyboard.press('ArrowRight');
    check('ArrowRight moves the focus to Manual without selecting it',
      (await ep.evaluate(() => document.activeElement?.textContent.trim().endsWith('Manual'))) && modeCalls.length === 0 && /Off$/.test(await checkedMode()));
    await switchMode(() => ep.keyboard.press('Space'), 'Manual');
    check('Space selects Manual and the focus stays on the control',
      /Manual$/.test(await checkedMode()) && modeCalls.length === 1 && modeCalls[0].mode === 'manual'
      && (await ep.evaluate(() => document.activeElement?.closest('.energy-mode-switch') !== null)), JSON.stringify(modeCalls));
    await ep.waitForFunction(() => [...document.querySelectorAll('.energy-actions button')].some((b) => !b.disabled), null, { timeout: 15000 }).catch(() => { });
    const afterArm = await states();
    check('Manual enables the available actions', afterArm.some((disabled) => !disabled), JSON.stringify(afterArm));

    // A token cannot command a Manual inverter: 409 energy_manager_not_external (the PAT is created below via the API).
    const patBody = await ep.evaluate(async () => {
      const token = await (await fetch('/admin/api/tokens', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': (await (await fetch('/admin/api/session')).json()).csrf_token }, body: JSON.stringify({ name: 'e2e-mode', role: 'read/write' }) })).json();
      return token;
    });
    const secret = patBody.token || patBody.secret || '';
    if (secret) {
      const viaPat = await ep.request.post(`${energyBase}/api/v1/devices/sim/energy/command`, { headers: { Authorization: `Bearer ${secret}` }, data: { action: 'hold' } });
      check('a PAT command on a Manual inverter answers 409 energy_manager_not_external',
        viaPat.status() === 409 && (await viaPat.json()).code === 'energy_manager_not_external', String(viaPat.status()));
      // This request belongs to the separate energy page, outside the main page's error listener.
    } else check('a PAT could be created for the Manual-mode check', false, JSON.stringify(patBody).slice(0, 200));

    const cmd = async (action) => {
      const before = commands.length;
      await action();
      const started = Date.now();
      while (commands.length === before && Date.now() - started < 8000) await sleep(100);
      return commands[before];
    };
    if (!afterArm[0]) {
      await buttons[0].click();
      check('Charge opens the target SoC area', await ep.locator('.energy-target').isVisible());
      const charge = await cmd(() => ep.locator('.energy-target button').click());
      check('Charge sends a command with a target SoC', charge?.action === 'charge' && Number.isFinite(charge.target_soc_percent), JSON.stringify(charge));
    } else check('Charge is available in Manual', false, 'disabled in Manual');
    await ep.waitForFunction(() => [...document.querySelectorAll('.energy-actions button')].some((b) => b.textContent.trim() === 'Keep battery idle' && !b.disabled), null, { timeout: 40000 }).catch(() => { });
    const hold = !(await states())[1] ? await cmd(() => buttons[1].click()) : null;
    const holdDiag = await ep.evaluate(async () => ({ buttons: [...document.querySelectorAll('.energy-actions button')].map((b) => [b.textContent.trim(), b.disabled, b.title]), status: document.querySelector('.energy-status')?.innerText, mode: document.querySelector('.energy-mode')?.innerText, api: await (await fetch('/admin/api/energy/devices')).json().then((l) => ({ state: l[0].state, connected: l[0].connected, mode: l[0].mode, actions: l[0].actions, stop: l[0].stop_reason, restore: l[0].restore_attempts })) }));
    await ep.screenshot({ path: path.join(OUT, 'energy-after-charge.png'), fullPage: true });
    check('Keep battery idle sends a hold command without a target SoC', hold?.action === 'hold' && !('target_soc_percent' in hold), JSON.stringify(hold) + JSON.stringify(holdDiag));
    const auto = await cmd(() => ep.locator('.energy-actions button', { hasText: 'Return to automatic' }).click());
    check('Return to automatic sends auto', auto?.action === 'auto', JSON.stringify(auto));

    // External: the GUI is refused and the controls are disabled with an info line; a PAT may command.
    await switchMode(() => radios.nth(2).click(), 'External');
    await ep.waitForFunction(() => [...document.querySelectorAll('.energy-actions button')].every((b) => b.disabled), null, { timeout: 5000 }).catch(() => { });
    const externalText = await operateText();
    check('External: the controls are disabled and the panel says an external app is in control',
      (await states()).every(Boolean) && /controlled by an external app through the API \(PAT required\)\./.test(externalText), externalText.slice(0, 700));
    check('External: a disabled control names the reason', /external app/.test(await buttons[0].getAttribute('title') || ''));
    const csrfNow = await ep.evaluate(async () => (await (await fetch('/admin/api/session')).json()).csrf_token);
    const viaGui = await ep.request.post(`${energyBase}/admin/api/energy/devices/sim/command`, { headers: { 'X-CSRF-Token': csrfNow, Origin: energyBase }, data: { action: 'hold' } });
    check('External: a GUI command answers 409 energy_manager_external', viaGui.status() === 409 && (await viaGui.json()).code === 'energy_manager_external', String(viaGui.status()));
    if (secret) {
      const viaPat = await ep.request.post(`${energyBase}/api/v1/devices/sim/energy/command`, { headers: { Authorization: `Bearer ${secret}` }, data: { action: 'hold' } });
      check('External: a PAT command is accepted', viaPat.status() === 200, String(viaPat.status()));
    }
    // Back to Manual so the remaining checks see the operable controls.
    await switchMode(() => radios.nth(1).click(), 'Manual');

    // The relabels are visible on Operate (presentation-only; the REST action names stayed on the wire
    // above: hold/charge/auto). Manual-control enable/disable wording replaces the old ON/OFF switch.
    const operate = await operateText();
    check('Operate shows the mode control', /Manual/.test(operate) && /External/.test(operate), operate.slice(0, 400));
    check('Operate shows the relabelled battery actions',
      /Charge battery/.test(operate) && /Keep battery idle/.test(operate) && /Discharge battery/.test(operate), operate.slice(0, 400));

    // Normal mode stays compact: no raw capability names, reject detail or expert controls.
    check('Normal mode shows no raw capability names, engineering mode or reject detail',
      !/write_path_convention|reject|Engineering mode|SoC target policy|Revoke verification|Strategy code/i.test(operate)
      && (await ep.locator('.energy-setup:visible').count()) === 0, operate.slice(0, 400));
    // Expert mode is a pure display switch: toggling must not send any write request.
    const writes = [];
    ep.on('request', (r) => { if (r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`); });
    await ep.locator('#energy-expert-mode').check();
    await ep.locator('.energy-expert').first().waitFor();
    check('Expert mode ON shows Revoke verification, engineering mode and SoC policy',
      /Revoke verification/.test(await ep.locator('.energy-expert').first().innerText())
      && /Engineering mode/.test(await ep.locator('.energy-expert').first().innerText())
      && /SoC target policy/.test(await ep.locator('.energy-expert').first().innerText()));
    await ep.locator('.energy-diagnostics summary').first().click();
    const diagText = await ep.locator('.energy-diagnostics').first().innerText();
    check('Diagnostics still exposes raw capability names',
      /write_path_convention/.test(diagText) && /battery_power_sign_convention/.test(diagText), diagText.slice(0, 400));
    const expertText = await ep.locator('.energy-expert').first().innerText();
    check('Expert shows power limits in kW', /Maximum charging power \(kW\)/.test(expertText) && /Maximum discharging power \(kW\)/.test(expertText), expertText.slice(0, 400));
    await ep.locator('#energy-expert-mode').uncheck();
    check('Expert mode OFF hides expert content again; toggling sent no write request',
      (await ep.locator('.energy-expert').count()) === 0 && (await ep.getByText('Revoke verification').count()) === 0
      && writes.length === 0, writes.join(', '));

    // Saving the power limits must carry the existing engineering_mode instead of resetting it.
    const limitPuts = [];
    ep.on('request', (r) => { if (r.method() === 'PUT' && /\/dispatch\/devices\//.test(r.url())) limitPuts.push(r.postDataJSON()); });
    await ep.locator('#energy-expert-mode').check();
    const expertBox = ep.locator('.energy-expert').first();
    await expertBox.getByLabel('Engineering mode').check();
    await expertBox.getByRole('button', { name: 'Save engineering mode' }).click();
    await sleep(1000);
    await expertBox.getByRole('button', { name: 'Save limits' }).click();
    await sleep(800);
    check('saving the power limits keeps the existing engineering_mode',
      limitPuts.length >= 2 && limitPuts.at(-1).engineering_mode === true, JSON.stringify(limitPuts));
    await expertBox.getByLabel('Engineering mode').uncheck(); // restore the simulator state
    await expertBox.getByRole('button', { name: 'Save engineering mode' }).click();
    await sleep(500);
    await ep.locator('#energy-expert-mode').uncheck();

    // Missing power limits open the limits step, without the engineering-mode control.
    mockNoLimits = true;
    await ep.reload();
    await ep.waitForSelector('.energy-setup-step', { timeout: 15000 }).catch(() => { });
    const noLimitsText = await ep.locator('.energy-panel').first().innerText();
    check('missing power limits show the limits step without engineering mode',
      /Maximum charging power/.test(noLimitsText) && !/Engineering mode/.test(noLimitsText), noLimitsText.slice(0, 400));
    mockNoLimits = false;

    await ep.setViewportSize({ width: 390, height: 844 });
    await sleep(300);
    const energyOverflow = await ep.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check('energy page has no horizontal page scroll at 390px', energyOverflow <= 0, String(energyOverflow));
    check('energy page raised no console, network or script errors', energyProblems.length === 0, energyProblems.join('; '));
    await energyContext.close();
  } finally {
    spawned.proc.kill('SIGTERM');
    energyFake.proc.kill('SIGTERM');
  }
}

// 6a. GridStack dashboard layout: edit mode, move+resize a widget, autosave, reload, reset.
await page.goto(base + '/ui/dashboard');
await page.waitForFunction(() => document.querySelector('#dashboard-grid .grid-stack-item[data-widget-id="pv-power"]')?.gridstackNode);
const editToggle = page.locator('#dashboard-edit-toggle');
check('dashboard starts in view mode with the drag handle hidden',
  await editToggle.isVisible() && !(await page.locator('.dashboard-widget-header').first().isVisible())
  && !(await page.locator('#dashboard-edit-done').isVisible()));
await editToggle.click();
await page.waitForSelector('.dashboard-editing');
check('edit mode shows Add widget, Reset layout and Done',
  await page.locator('#dashboard-add-widget:visible').count() === 1
  && await page.locator('#dashboard-reset-layout:visible').count() === 1
  && await page.locator('#dashboard-edit-done:visible').count() === 1);

let dashboardLayoutPut = null;
await page.route('**/admin/api/dashboard-layout', async (route) => {
  if (route.request().method() === 'PUT') dashboardLayoutPut = route.request().postDataJSON();
  await route.continue();
});
// GridStack's own API moves/resizes the widget (equivalent to a completed drag/resize) and fires
// the 'change' event dashboard.js listens on, which triggers the debounced autosave.
const movedPosition = await page.evaluate(() => {
  const grid = document.querySelector('#dashboard-grid').gridstack;
  const el = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]');
  grid.update(el, { x: 6, y: 6, w: 4, h: 3 });
  const node = el.gridstackNode;
  return { x: node.x, y: node.y, w: node.w, h: node.h };
});
await sleep(700); // past the 300-500ms autosave debounce
check('moving/resizing a widget autosaves the layout via PUT /admin/api/dashboard-layout',
  Boolean(dashboardLayoutPut) && dashboardLayoutPut.widgets.some((w) =>
    w.id === 'pv-power' && w.x === movedPosition.x && w.y === movedPosition.y
    && w.w === movedPosition.w && w.h === movedPosition.h),
  JSON.stringify({ movedPosition, dashboardLayoutPut }));
check('autosave shows the "Dashboard layout saved." toast once', (await toastText()).includes('Dashboard layout saved.'));
await page.unroute('**/admin/api/dashboard-layout');

await page.reload();
await page.waitForFunction(() => document.querySelector('#dashboard-grid .grid-stack-item[data-widget-id="pv-power"]')?.gridstackNode);
const afterReload = await page.evaluate(() => {
  const node = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]').gridstackNode;
  return { x: node.x, y: node.y, w: node.w, h: node.h };
});
check('the moved/resized position persists after a browser reload',
  afterReload.x === movedPosition.x && afterReload.y === movedPosition.y
  && afterReload.w === movedPosition.w && afterReload.h === movedPosition.h,
  JSON.stringify(afterReload));

await editToggle.click();
await page.waitForSelector('.dashboard-editing');
await page.locator('#dashboard-reset-layout').click();
await page.waitForSelector('#confirm-modal.show', { timeout: 5000 });
check('"Restore default layout?" confirmation was shown', (await page.locator('#confirm-title').innerText()) === 'Restore default layout?');
await page.click('#confirm-accept');
await sleep(400);
const afterReset = await page.evaluate(() => {
  const node = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]').gridstackNode;
  return { x: node.x, y: node.y, w: node.w, h: node.h };
});
check('reset layout restores the default pv-power position immediately',
  afterReset.x === 0 && afterReset.y === 0 && afterReset.w === 2 && afterReset.h === 2, JSON.stringify(afterReset));
await page.reload();
await page.waitForFunction(() => document.querySelector('#dashboard-grid .grid-stack-item[data-widget-id="pv-power"]')?.gridstackNode);
const afterResetReload = await page.evaluate(() => {
  const node = document.querySelector('.grid-stack-item[data-widget-id="pv-power"]').gridstackNode;
  return { x: node.x, y: node.y, w: node.w, h: node.h };
});
check('default position survives a reload after reset',
  afterResetReload.x === 0 && afterResetReload.y === 0 && afterResetReload.w === 2 && afterResetReload.h === 2,
  JSON.stringify(afterResetReload));
check('dashboard GridStack flow raised no script errors', problems.filter((p) => /dashboard\.js|gridstack/i.test(p)).length === 0,
  problems.filter((p) => /dashboard\.js|gridstack/i.test(p)).join('; '));

// 7. restart persistence
const finalRestart = problems.length;
await stop(server);
server = await startServer(db, httpPort, devicePort, 'second');
scrubExpected(finalRestart, (p) => expectedRestartProblem(p, base));
check('no initial password printed after restart', !/FIRST START - admin login/.test(server.output()));
await page.goto(base + '/ui/prometheus');
check('session survives restart', page.url().endsWith('/ui/prometheus'));
await page.waitForSelector('#exposed-list .metric-row');
check('order persists after server restart', JSON.stringify(await names()) === JSON.stringify(afterDrag));
check('log level persisted', (await (await context.request.get(base + '/admin/api/settings')).json()).settings.log_level === 'DEBUG');

// 8. logout
await page.goto(base + '/ui/dashboard');
await page.click('#logout-button');
await page.waitForURL('**/login');
await page.goto(base + '/ui/dashboard');
check('logout ends the session', page.url().endsWith('/login'));

const relevant = [...problems];
console.log('console/network problems:', JSON.stringify(relevant, null, 1));
check('browser console and network clean', relevant.length === 0);
fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, problems, relevant }, null, 2));
await browser.close();
await stop(server);
fake.proc.kill('SIGTERM');
fs.rmSync(dbDir, { recursive: true, force: true });
const failed = results.filter((r) => !r.ok);
console.log(`${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
