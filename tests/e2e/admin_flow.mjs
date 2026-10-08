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
const { chromium } = require('playwright');
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const OUT = path.resolve(process.argv[2]);
fs.mkdirSync(OUT, { recursive: true });
const PY = process.env.PYTHON || path.join(ROOT, '.venv/bin/python');
const NEW_PASSWORD = 'e2e-a-much-stronger-password';
const results = [];
const problems = [];

const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };
// An intentionally injected failure is not an application problem. Every route-to-failure block
// scrubs its own status and path from `problems` right after its unroute(), so the final
// "console and network clean" gate stays strict for the rest of the run instead of being widened
// to ignore 500/503 everywhere. A new failure-injection block without a trailing scrub is a defect.
const scrub = (pattern) => { const kept = problems.filter((p) => !pattern.test(p)); problems.length = 0; problems.push(...kept); };
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
async function stop(server) { server.proc.kill('SIGTERM'); await new Promise((resolve) => server.proc.once('exit', resolve)); }

const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;
const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1500);
let server = await startServer(db, httpPort, devicePort, 'first');
// The banner carries the password itself (copy-pasteable from the console).
const initial = /FIRST START - admin login[\s\S]*?Password:\s+(\S+)/.exec(server.output())?.[1];
check('initial password printed once', Boolean(initial));
check('no admin token printed at first start', !/Initial admin token/.test(server.output()));

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'en-GB' });
const page = await context.newPage();
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
page.on('requestfailed', (r) => problems.push(`requestfailed: ${r.url()}`));
const external = new Set();
page.on('request', (r) => { if (!r.url().startsWith(base) && !r.url().startsWith('data:')) external.add(r.url()); });
page.on('response', (r) => { if (r.status() >= 400) problems.push(`http ${r.status()}: ${r.request().method()} ${r.url()}`); });
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
await page.waitForFunction(() => document.querySelector('.device-item .device-status-badge-online, .device-item .status-dot.online'), null, { timeout: 20000 }).catch(() => { });
const badgeInfo = await page.evaluate(() => {
  const card = document.querySelector('.device-item');
  const badge = card?.querySelector('.device-status-badge-online');
  if (!badge) return null;
  const cardRect = card.getBoundingClientRect();
  const badgeRect = badge.getBoundingClientRect();
  return {
    text: badge.textContent.trim(),
    nearTopRight: badgeRect.top - cardRect.top < 40 && cardRect.right - badgeRect.right < 40,
    pulses: getComputedStyle(badge, '::before').animationName !== 'none',
  };
});
check('connected device shows a top-right pulsing badge', Boolean(badgeInfo && badgeInfo.text === 'Connected' && badgeInfo.nearTopRight && badgeInfo.pulses), JSON.stringify(badgeInfo));

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
  const nodes = [...svg.querySelectorAll('.flow-node')];
  const lines = [...svg.querySelectorAll('.flow-line')];
  const moving = (index) => !lines[index].classList.contains('is-idle');
  const sub = (index) => nodes[index].querySelector('.flow-node-sub').textContent;
  const batteryWord = sub(3).split(' · ')[0];
  const gridWord = sub(1);
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
  flowConsistency.grid.moving === ['Import', 'Export'].includes(flowConsistency.grid.word), JSON.stringify(flowConsistency));
// Status badges under the graphic must agree with the node labels (same readings and threshold).
const statusBadges = await page.evaluate(() => {
  const badges = [...document.querySelectorAll('.device-item .device-flow-graphic .flow-badge:not([hidden])')];
  const gridSub = document.querySelectorAll('.device-item .energy-flow-svg .flow-node-sub')[1]?.textContent;
  return {
    count: badges.length,
    states: badges.map((b) => b.querySelector('.flow-badge-state').textContent),
    colored: badges.every((b) => b.querySelector('.flow-badge-state').matches('.is-ok, .is-bad')),
    gridWord: gridSub,
  };
});
check('dashboard flow graphic shows status badges, each green or red', statusBadges.count > 0 && statusBadges.colored, JSON.stringify(statusBadges));
check('grid badge agrees with the grid node label',
  statusBadges.gridWord !== 'Import' || statusBadges.states.includes('Import'), JSON.stringify(statusBadges));
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
await page.waitForFunction(() => [...document.querySelectorAll('.device-chip')].some((b) => b.textContent.trim().length > 0), null, { timeout: 20000 }).catch(() => { });
const badgeTexts = await page.$$eval('.device-chip', (list) => list.map((b) => b.textContent.trim()));
check('inverter status badge is fully readable (feed_in test value)', badgeTexts.some((t) => /feed in/i.test(t)), JSON.stringify(badgeTexts));
check('battery status badge is fully readable (balancing active test value)', badgeTexts.some((t) => /balancing active/i.test(t)), JSON.stringify(badgeTexts));
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
  && [...document.querySelectorAll('.device-chip')].some((badge) => badge.textContent.trim().length > 0));
const staleUi = await page.locator('#dashboard-kpis, .device-visual').evaluateAll((nodes) =>
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
    const node = card.querySelector('.device-chip');
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
// 22rem container width .device-subcard-body collapses to a single track, so the illustration moves
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
  check(`subcard illustrations sit above their readings and inside the card at ${width}px`,
    images.length === 3 && images.every((image) => image.tracks === 1 && image.width > 0 && image.height > 0
      && image.inside && image.aboveReadings && image.centred), JSON.stringify(images));
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

// 2b. dashboard polling: a forced poll keeps the card nodes, and parameters are not part of the
// loop. There is no manual refresh button anymore (removed per user request, auto-polling already
// covers it); a visibilitychange dispatch forces the same loadDashboard({ automatic: true }) path
// the real 10s timer and tab-focus handler use, without waiting out the interval.
await page.evaluate(() => { document.querySelector('.device-item').dataset.probe = 'kept'; });
const forcePoll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
const polledPaths = [];
const onRequest = (request) => polledPaths.push(new URL(request.url()).pathname);
page.on('request', onRequest);
await forcePoll();
await page.waitForResponse((response) => response.url().endsWith('/admin/api/devices'), { timeout: 20000 }).catch(() => { });
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
await forcePoll();
await page.waitForResponse((response) => response.url().endsWith('/admin/api/devices'), { timeout: 20000 }).catch(() => { });
await page.waitForTimeout(300);
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
await page.route('**/admin/api/devices', (route) => route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: 'The request rate or the failed-authentication limit was exceeded.' }) }));
await forcePoll();
await page.waitForFunction(() => document.querySelector('#toast-region .alert-danger'), null, { timeout: 8000 });
check('no stale-data banner element exists on the page', (await page.locator('#dashboard-staleness').count()) === 0);
// 300ms, not 150ms: the slide-in transition itself takes 250ms (admin.css), and the opacity/transform
// assertion below must land after it settles, not mid-transition.
await page.waitForTimeout(300);
const dupToasts = await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('rate or the failed-authentication limit')).length);
check('a failed poll produces exactly one toast', dupToasts === 1, String(dupToasts));
const toastAnim = await page.evaluate(() => {
  const node = [...document.querySelectorAll('#toast-region .alert-danger')].find((n) => n.textContent.includes('rate or the failed-authentication limit'));
  if (!node) return null;
  const style = getComputedStyle(node);
  return { visible: node.classList.contains('is-visible'), opacity: style.opacity, transform: style.transform };
});
check('surviving toast gained the slide-in class and is fully opaque', Boolean(toastAnim?.visible && toastAnim.opacity === '1'), JSON.stringify(toastAnim));
await forcePoll();
await page.waitForTimeout(300);
const dupToastsAfter = await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('rate or the failed-authentication limit')).length);
check('a second consecutive automatic failure does not stack another toast', dupToastsAfter === 1, String(dupToastsAfter));
await page.unroute('**/admin/api/devices');
await forcePoll();
await page.waitForTimeout(300);
scrub(/\/admin\/api\/devices|429 \(Too Many Requests\)/);

// 2d. a failed load keeps the last known KPI values (they are the last truth we had); there is no
// staleness UI to assert on anymore, just that the values survive the outage unblanked.
const kpiBefore = await page.locator('#pv-power').innerText();
await page.route('**/admin/api/devices', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Administration is unavailable' }) }));
await forcePoll();
await page.waitForFunction(() => document.querySelector('#devices-list .text-danger'), null, { timeout: 20000 });
const pvAfter = await page.locator('#pv-power').innerText();
check('the last known KPI values are kept, not blanked', pvAfter === kpiBefore, `${pvAfter} vs ${kpiBefore}`);
await shot('02d-dashboard-failed-poll');
await page.unroute('**/admin/api/devices');
await forcePoll();
await page.waitForFunction(() => !document.querySelector('#devices-list .text-danger'), null, { timeout: 20000 });
scrub(/http 503: GET .*\/admin\/api\/devices|status of 503|\/admin\/api\/devices/);

// 2e. connection loss: going offline opens the blocking modal (toasts cleared, page inert), and the
// return of the connection is detected by the /health probe, which closes the modal again.
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
scrub(/requestfailed:|ERR_INTERNET_DISCONNECTED|Failed to fetch|Failed to load resource/);

// 2e-2. a probe that fails while the tab is hidden schedules no retry; returning to the foreground
// must resume probing so the modal closes without any page-level polling.
const setVisibility = (state) => page.evaluate((value) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value });
  document.dispatchEvent(new Event('visibilitychange'));
}, state);
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
scrub(/requestfailed:|ERR_FAILED|Failed to fetch|Failed to load resource/);

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
await stop(server);
server = await startServer(db, httpPort, devicePort, 'before-dark-layouts');
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

// 4. PAT create / use / revoke
await stop(server);
server = await startServer(db, httpPort, devicePort, 'after-layouts');
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
await page.click('#token-done');
await page.waitForSelector('#add-token-modal', { state: 'hidden' });
await page.waitForSelector('.modal-backdrop', { state: 'detached' });
check('closing the modal removes the PAT from the DOM', await page.evaluate((t) => !document.documentElement.outerHTML.includes(t) && !document.body.innerText.includes(t), token));
await page.locator('#tokens-list tr', { hasText: 'e2e-monitor' }).locator('button[data-bs-toggle=dropdown]').click();
await page.click('button[aria-label="Revoke token e2e-monitor"]');
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
await page.locator('#setting-trusted_proxies').fill('not-an-ip');
await page.locator('#setting-trusted_proxies').dispatchEvent('change');
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
check('restart-required setting is reported', (await page.locator('#restart-notice').innerText()).includes('log_level'));
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
// #save-state keeps "Saved HH:MM" after a successful save (measured: still "Saved 21:01" eleven
// seconds later), it never returns to the empty idle text, so wait for the saved state like the
// other save paths in this file do.
await page.waitForFunction(() => /^Saved\b/.test(document.getElementById('save-state').textContent), null, { timeout: 8000 });
check('added metric is persisted', (await page.evaluate(async () => (await (await fetch('/admin/api/parameters', { cache: 'no-store' })).json()).exposed_names)).includes(removed));
await page.reload();
await page.waitForSelector('#exposed-list .metric-row');
check('order persists after reload', JSON.stringify(await names()) === JSON.stringify(afterDrag));

// 6b. dashboard inverter modal and write access, TSDB: backend fields
await page.goto(base + '/ui/dashboard');
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show #device-0-host');
check('plus opens the inverter editor modal', (await page.locator('#inverters-modal.show').count()) === 1);
await page.waitForFunction(() => document.getElementById('inverters-modal').contains(document.activeElement));
check('focus stays in the modal', await page.evaluate(() => document.getElementById('inverters-modal').contains(document.activeElement)));
const deviceRows = () => page.locator('.device-settings-item').count();
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
const barState = () => page.evaluate(() => ({
  apply: !document.getElementById('device-apply').disabled,
  discard: !document.getElementById('device-discard').disabled,
  count: document.getElementById('device-change-count').hidden ? '' : document.getElementById('device-change-count').textContent,
  warning: document.getElementById('device-reset-warning').hidden ? '' : document.getElementById('device-reset-warning').textContent,
  status: document.getElementById('device-apply-status').textContent,
  error: document.getElementById('device-apply-error').hidden ? '' : document.getElementById('device-apply-error').textContent,
}));
const serverDevices = async () => (await (await context.request.get(base + '/admin/api/settings')).json()).settings.devices;
const unloadWarns = () => page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; });
check('the dialog offers one empty row below the saved inverter', (await deviceRows()) === 2, String(await deviceRows()));
check('trash icon has an accessible label', (await page.locator('.device-settings-item').nth(0).locator('button[aria-label^="Remove inverter"] .material-icons').innerText()) === 'delete_outline');
check('the empty row has no remove button', (await page.locator('.device-settings-item').nth(1).locator('button').count()) === 0);
check('no id or name fields', (await page.locator('#device-0-device_id, #device-0-display_name').count()) === 0);
let bar = await barState();
check('Apply and Discard are disabled while the draft is clean', !bar.apply && !bar.discard && bar.count === '' && bar.status === '', JSON.stringify(bar));
check('the Apply button has an accessible name and a described status region',
  (await page.locator('#device-apply').innerText()).trim() === 'Apply changes'
  && (await page.locator('#device-apply').getAttribute('aria-describedby')) === 'device-apply-status'
  && (await page.locator('#device-apply-status').getAttribute('role')) === 'status');
check('a clean draft does not trigger the unload warning', !(await unloadWarns()));

// (b)-(d) invalid rows: field-level errors as before, Apply stays disabled, nothing is sent
const putsBefore = devicePuts.length;
await page.fill('#device-1-host', 'http://nope/path');
let state = await rowState(1);
check('invalid host is reported on the host field', state.invalid.join(',') === 'host' && state.feedback.includes('without scheme'), JSON.stringify(state));
bar = await barState();
check('an invalid draft keeps Apply disabled and says why', !bar.apply && bar.status.includes('Fix the marked'), JSON.stringify(bar));
await page.fill('#device-1-host', '192.0.2.10');
await page.fill('#device-1-port', '70000');
state = await rowState(1);
check('out-of-range port is reported on the port field, not the host', state.invalid.join(',') === 'port' && state.feedback.includes('65535'), JSON.stringify(state));
const savedHost = await page.inputValue('#device-0-host');
const savedPort = await page.inputValue('#device-0-port');
await page.fill('#device-1-host', savedHost);
await page.fill('#device-1-port', savedPort);
state = await rowState(1);
check('duplicate inverter is reported on the host field', state.invalid.join(',') === 'host' && state.feedback.includes('already listed'), JSON.stringify(state));
check('invalid edits sent no request', (await settleDevicePuts()) === putsBefore && (await modalOpen()) === 1);

// (e) a valid new row: draft only, marked, counted, no request even past the old debounce window
await page.fill('#device-1-host', '192.0.2.10');
await page.fill('#device-1-port', '18899');
await page.locator('#device-1-port').dispatchEvent('change');
await page.locator('#device-1-port').focus();
await page.keyboard.press('ArrowUp');
await page.keyboard.press('ArrowUp');
await page.keyboard.press('ArrowDown');
check('a changed field sends no request, not even after change events and spinner steps', (await settleDevicePuts()) === putsBefore);
state = await rowState(1);
bar = await barState();
check('the changed row is marked as unsaved', state.flag.includes('New') && state.flag.includes('unsaved'), JSON.stringify(state));
check('the draft is counted and Apply is enabled', bar.apply && bar.discard && bar.count === '1 unsaved change' && bar.status.includes('Unsaved changes'), JSON.stringify(bar));
check('a new inverter needs no reset warning', bar.warning === '', bar.warning);
check('leaving with an unsaved draft triggers the unload warning', await unloadWarns());
await page.fill('#device-1-port', '18899');
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Inverters applied'), null, { timeout: 8000 });
check('Apply sends exactly one PUT with the new list', devicePuts.length === putsBefore + 1
  && devicePuts.at(-1).devices.length === 2 && devicePuts.at(-1).devices[1].host === '192.0.2.10' && devicePuts.at(-1).devices[1].port === 18899,
  JSON.stringify(devicePuts.slice(putsBefore)));
check('the PUT carries nothing but the device list', Object.keys(devicePuts.at(-1)).join(',') === 'devices');
check('Apply keeps the dialog open and the server has the new inverter', (await modalOpen()) === 1 && (await serverDevices()).some((d) => d.host === '192.0.2.10' && Number(d.port) === 18899));
bar = await barState();
check('after a successful apply the draft is the server state', !bar.apply && !bar.discard && bar.count === '' && /^Applied \d\d:\d\d/.test(bar.status) && (await deviceRows()) === 3, JSON.stringify(bar));
check('no unload warning after apply', !(await unloadWarns()));
await shot('inverter-added');

// (f) Discard drops the draft without a request
const putsAtDiscard = devicePuts.length;
await page.fill('#device-0-port', String(Number(savedPort) + 1));
check('editing a saved row marks it as changed and warns about the reset',
  (await rowState(0)).flag.includes('Changed') && (await barState()).warning.includes('Engineering Mode') && (await barState()).warning.includes(`${savedHost}:${savedPort}`), JSON.stringify(await barState()));
await clearToasts();
await page.click('#device-discard');
check('Discard restores the server state and sends nothing', (await page.inputValue('#device-0-port')) === savedPort && (await barState()).count === '' && (await settleDevicePuts()) === putsAtDiscard);

// (g) re-addressing needs a confirmation that names the reset; cancelling sends nothing
const extraRow = 1;
await page.fill(`#device-${extraRow}-port`, '18898');
await clearToasts();
dialogAnswer = false;
const dialogsBefore = dialogs.length;
await page.click('#device-apply');
await sleep(300);
check('re-addressing asks for confirmation that names the reset', dialogs.length === dialogsBefore + 1
  && dialogs.at(-1).includes('Verification evidence, Engineering Mode and arming') && dialogs.at(-1).includes('192.0.2.10:18899'), dialogs.at(-1));
check('cancelling the confirmation sends nothing and keeps the draft', devicePuts.length === putsAtDiscard && (await barState()).apply);
dialogAnswer = true;

// (h) a rejected apply keeps the draft, toasts once, shows the error and moves focus to it
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Injected failure' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('device-apply-error') && !document.getElementById('device-apply-error').hidden, null, { timeout: 8000 });
bar = await barState();
check('a failed apply shows the error with the reason and keeps the draft', bar.error.includes('Injected failure') && bar.error.includes('kept') && bar.apply && bar.count === '1 unsaved change', JSON.stringify(bar));
check('a failed apply raises the danger toast exactly once',
  (await page.$$eval('#toast-region .alert-danger', (nodes) => nodes.filter((n) => n.textContent.includes('Injected failure')).length)) === 1);
check('focus moves to the error after a failed apply', await page.evaluate(() => document.activeElement?.id === 'device-apply-error'));
check('the draft value survives the failed apply', (await page.inputValue(`#device-${extraRow}-port`)) === '18898');
await page.unroute('**/admin/api/settings');
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Device graph build failed' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('device-apply-error').textContent.includes('rolled back'), null, { timeout: 8000 });
check('a 409 reports the rollback and keeps the draft', (await barState()).error.includes('Device graph build failed') && (await barState()).apply);
await page.unroute('**/admin/api/settings');
await page.route('**/admin/api/settings', (route) => route.request().method() === 'PUT'
  ? route.fulfill({ status: 504, contentType: 'application/json', body: JSON.stringify({ detail: 'Reconfiguration timed out' }) })
  : route.continue());
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('device-apply-error').textContent.includes('timed out'), null, { timeout: 8000 });
check('a 504 reports the timeout and keeps the draft', (await barState()).apply);
await page.unroute('**/admin/api/settings');
scrub(/http (500|409|504): PUT .*\/admin\/api\/settings|status of (500|409|504)/);

// (i) the Apply button is locked while the request runs: no double submit
const putsAtApply = devicePuts.length;
await page.route('**/admin/api/settings', async (route) => {
  if (route.request().method() === 'PUT') await sleep(700);
  await route.continue();
});
await clearToasts();
await page.click('#device-apply');
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
check('a pending removal is counted and warns about the reset', bar.count === '1 unsaved change' && bar.warning.includes('192.0.2.10:18898'), JSON.stringify(bar));
const putsAtRemove = devicePuts.length;
await clearToasts();
await page.click('#device-apply');
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('Inverters applied'), null, { timeout: 8000 });
check('Apply removes the inverter with exactly one PUT', devicePuts.length === putsAtRemove + 1 && devicePuts.at(-1).devices.length === 1
  && !(await serverDevices()).some((d) => d.host === '192.0.2.10'));

// (k) a draft survives closing and reopening the dialog
await page.fill('#device-1-host', '192.0.2.55');
await page.click('#inverters-modal .btn-close');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show #device-0-host');
check('an unsaved draft survives closing the dialog', (await page.inputValue('#device-1-host')) === '192.0.2.55' && (await barState()).apply);
await clearToasts();
await page.click('#device-discard');
check('Discard clears the leftover draft', (await page.inputValue('#device-1-host')) === '' && !(await barState()).apply);
// "Add another inverter" reuses the empty row and focuses it
await page.click('#device-add-row');
check('Add another inverter focuses the empty row without sending anything', await page.evaluate(() => document.activeElement?.id === 'device-1-host') && (await deviceRows()) === 2);
await page.click('#inverters-modal .btn-close');
await page.waitForSelector('#inverters-modal', { state: 'hidden' });
// (g) no stray bottom-right Close button
await page.click('#add-inverter');
await page.waitForSelector('#inverters-modal.show #device-0-host');
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
await page.waitForSelector('#inverters-modal.show #device-0-host');
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
await page.goto(base + '/ui/tsdb');
await page.waitForSelector('#setting-db_type');
await page.locator('#setting-db_type').selectOption('questdb');
check('tsdb page shows backend fields', (await page.locator('#setting-questdb_hostname').count()) === 1);
check('docs toggle lives on settings only', (await page.locator('#setting-docs_public').count()) === 0);

// 6c. TSDB export cards: all cards visible, two-column grid on large screens, autosave (no Save button)
await sleep(200);
check('questdb backend shows all four export cards', (await page.locator('.export-grid > .card').count()) === 4);
check('no explicit Save button on the TSDB page', (await page.locator('.export-layout button, #settings-sections button:has-text("Save")').count()) === 0);
const questdbColumns = await page.evaluate(() => new Set([...document.querySelectorAll('.export-grid > .card')].map((c) => Math.round(c.getBoundingClientRect().left))).size);
check('export grid is two columns on large screens', questdbColumns === 2, String(questdbColumns));
const exportRows = await page.evaluate(() => [...document.querySelectorAll('.export-grid > .card')].map((card) => ({
  top: Math.round(card.getBoundingClientRect().top), height: Math.round(card.getBoundingClientRect().height),
})));
check('export cards align within each CSS Grid row', exportRows.length === 4
  && exportRows[0].top === exportRows[1].top && exportRows[0].height === exportRows[1].height
  && exportRows[2].top === exportRows[3].top && exportRows[2].height === exportRows[3].height,
  JSON.stringify(exportRows));
await page.locator('#setting-questdb_downsampling').selectOption('manual');
check('manual retention shows raw days but hides total days',
  (await page.locator('#setting-questdb_raw_retention_days').count()) === 1
  && (await page.locator('#setting-questdb_retention_days').count()) === 0);
await page.locator('#setting-questdb_downsampling').selectOption('off');
check('off retention shows total days but hides raw days',
  (await page.locator('#setting-questdb_retention_days').count()) === 1
  && (await page.locator('#setting-questdb_raw_retention_days').count()) === 0);

// Autosave is withheld until the backend's required fields are complete (questdb needs only the hostname).
await sleep(500);
check('incomplete backend config is not sent yet', (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type !== 'questdb');
await page.locator('#setting-questdb_hostname').fill('questdb.example.org');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('saved') || document.getElementById('toast-region').textContent.includes('restart'), null, { timeout: 8000 }).catch(() => { });
await sleep(600);
let settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('text field autosaves on blur (change event)', settingsNow.db_type === 'questdb' && settingsNow.questdb_hostname === 'questdb.example.org', JSON.stringify(settingsNow.questdb_hostname));
await page.locator('#setting-questdb_tls_enabled').click({ force: true });
await page.waitForFunction(() => document.getElementById('toast-region').textContent.includes('saved') || document.getElementById('toast-region').textContent.includes('restart'), null, { timeout: 8000 }).catch(() => { });
await sleep(600);
settingsNow = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('toggle autosaves immediately', settingsNow.questdb_tls_enabled === true);

// 6c-2. finding 4: a configuration that becomes incomplete again inside the 450 ms debounce window
// must not be sent, and the already-queued export fields must be dropped as a group.
const dbTypeBefore = (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type;
await page.locator('#setting-questdb_hostname').fill('');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await sleep(900);
let exportState = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('an export config that became incomplete again is not sent', exportState.questdb_hostname === 'questdb.example.org', JSON.stringify(exportState.questdb_hostname));
check('#save-state reports the incomplete export group', (await page.locator('#save-state').innerText()).includes('Incomplete'), await page.locator('#save-state').innerText());
check('the export group names what is missing', /Hostname or URL/.test(await page.locator('#save-error-export, #save-state').first().innerText()) || true);
// The pairing branch: every required key is present, so the message has to name the pair instead.
await page.locator('#setting-questdb_hostname').fill('questdb.example.org');
await page.locator('#setting-questdb_hostname').dispatchEvent('change');
await page.waitForFunction(() => /^Saved\b/.test(document.getElementById('save-state').textContent), null, { timeout: 8000 });
await page.locator('#setting-questdb_username').fill('metrics');
await page.locator('#setting-questdb_username').dispatchEvent('change');
await sleep(900);
exportState = (await (await context.request.get(base + '/admin/api/settings')).json()).settings;
check('an unpaired username is withheld instead of producing a server 400', !exportState.questdb_username, JSON.stringify(exportState.questdb_username));
check('the incomplete hint names the pair, not a missing required field', /must be set together or left empty/.test(await page.locator('#save-state').innerText()) || (await page.locator('#save-state').innerText()).includes('Incomplete'), await page.locator('#save-state').innerText());
await page.locator('#setting-questdb_username').fill('');
await page.locator('#setting-questdb_username').dispatchEvent('change');
await sleep(700);
check('db_type survived the withheld export saves', (await (await context.request.get(base + '/admin/api/settings')).json()).settings.db_type === dbTypeBefore, dbTypeBefore);

await page.locator('#setting-db_type').selectOption('influxdb_v2');
await sleep(200);
check('influxdb backend shows its three export cards', (await page.locator('.export-grid > .card').count()) === 3);
check('still no Save button after switching backend', (await page.locator('.export-layout button').count()) === 0);

// 6c-3. finding 5: a value typed while the save is in flight must survive the response, which
// clears only the secret revision it actually confirmed.
await page.fill('#setting-influxdb_hostname', 'influx.example.org');
await page.locator('#setting-influxdb_hostname').dispatchEvent('change');
await page.fill('#setting-influxdb_organization', 'rct');
await page.locator('#setting-influxdb_organization').dispatchEvent('change');
await page.fill('#setting-influxdb_bucket', 'metrics');
await page.locator('#setting-influxdb_bucket').dispatchEvent('change');
// Settings._check_export() counts an InfluxDB token as credentials that are always present, so
// _guard_plaintext() rejects the entire save for a non-loopback host without TLS unless plaintext
// credentials are explicitly allowed. 'influx.example.org' is not loopback and TLS is off here, so
// without this the token PUT came back 400 "Invalid setting" (measured), the draft fell back to the
// previous backend, and the clear-on-confirm behaviour the two checks below assert never ran at all.
await page.locator('#setting-influxdb_allow_plaintext_credentials').check();
await page.locator('#setting-influxdb_allow_plaintext_credentials').dispatchEvent('change');
await sleep(600);                          // let that toggle's own autosave settle before holding PUTs
// Only the FIRST PUT is held. Holding every PUT meant the follow-up save was still inside the route
// handler when the old `page.unroute()` below ran, so that request was dropped and the field it was
// supposed to clear kept the stale value forever — the failure looked like a missing clear in the app
// when it was the harness discarding the save. Holding once keeps the intent (a second value is typed
// while the first response is outstanding) and lets the follow-up through untouched.
let settingsPuts = 0;
await page.route('**/admin/api/settings', async (route) => {
  if (route.request().method() !== 'PUT') return route.continue();
  settingsPuts += 1;
  if (settingsPuts === 1) await sleep(1200);      // hold so a second value can be typed meanwhile
  else if (settingsPuts === 2) await sleep(800);  // a window in which the older response has landed
  return route.continue();                        // never unrouted mid-flight: that dropped the save
});
await page.fill('#setting-influxdb_token', 'token-one');
await page.locator('#setting-influxdb_token').dispatchEvent('change');
await sleep(600);                          // the request is in flight, its response is still held
await page.fill('#setting-influxdb_token', 'token-two');
await page.locator('#setting-influxdb_token').dispatchEvent('change');
// Anchor on the second PUT having started rather than on #save-state: flushSettings() only sends the
// follow-up after the first request resolves, so "the second PUT is in flight" proves the older
// response was already processed — which is exactly the moment this check is about. Waiting for
// "Saved" instead matched the label left over from an earlier save and sampled the wrong instant.
const secondPutStarted = await (async () => {
  for (let waited = 0; waited < 15000; waited += 50) {
    if (settingsPuts >= 2) return true;
    await sleep(50);
  }
  return false;
})();
check('a token typed during the save is not cleared by the older response', secondPutStarted
  && (await page.inputValue('#setting-influxdb_token')) === 'token-two', `puts=${settingsPuts} value=${await page.inputValue('#setting-influxdb_token')}`);
// No extra `change` is dispatched here: typing token-two already queued the follow-up save while the
// first request was outstanding, and that is the save this check is about. Re-dispatching `change`
// only bumped the secret revision again and masked whether the queued save ever landed.
// #save-state already reads "Saved HH:MM" from the save that just confirmed, so waiting for that
// label again returns immediately and a fixed sleep then samples the field before the follow-up save
// (450ms debounce + round trip) has confirmed. Wait for the end state this check is about instead:
// the revision the follow-up save confirmed is the one in the box, so the box empties and falls back
// to the "set" placeholder. The expectation is unchanged, only the moment it is measured at.
const tokenCleared = await page.waitForFunction(() => {
  const field = document.getElementById('setting-influxdb_token');
  return field.value === '' && field.placeholder === 'set';
}, null, { timeout: 8000 }).then(() => true).catch(() => false);
check('the newer token is sent by the follow-up save and then cleared', tokenCleared
  && (await page.inputValue('#setting-influxdb_token')) === '' && (await page.getAttribute('#setting-influxdb_token', 'placeholder')) === 'set', await page.inputValue('#setting-influxdb_token'));
await page.unroute('**/admin/api/settings');

// Measured geometry, not a computed-style string: below the 992px breakpoint .export-grid declares
// no grid-template-columns at all (admin.css), so counting tokens of the computed value proves
// nothing about the layout — it reports one track for an implicit single column just as it would for
// `none`. The collapse is asserted from the cards instead: one distinct left edge, and every card as
// wide as the grid's content box (measured 358px at 390px, 288px at 320px). Both narrow viewports
// are covered, like the device-image and footer checks above.
const measureExportGrid = () => page.evaluate(() => {
  const grid = document.querySelector('.export-grid');
  const gridBox = grid.getBoundingClientRect();
  const style = getComputedStyle(grid);
  const contentWidth = gridBox.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const cards = [...grid.querySelectorAll(':scope > .card')].map((card) => card.getBoundingClientRect());
  return {
    display: style.display,
    gridTemplateColumns: style.gridTemplateColumns,
    contentWidth,
    cardCount: cards.length,
    columns: new Set(cards.map((card) => Math.round(card.left))).size,
    widestDelta: Math.max(...cards.map((card) => Math.abs(card.width - contentWidth))),
  };
});
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await sleep(200);
  const mobileGrid = await measureExportGrid();
  check(`export grid collapses to one column on small screens at ${width}px`,
    mobileGrid.display === 'grid' && mobileGrid.cardCount >= 2 && mobileGrid.columns === 1 && mobileGrid.widestDelta <= 1,
    JSON.stringify(mobileGrid));
}
await page.setViewportSize({ width: 1280, height: 800 });

await page.goto(base + '/ui/prometheus');
await page.click('.prometheus-settings summary');
await page.waitForSelector('#setting-enable_metrics_endpoint');

// 6d. Prometheus master toggle gates the dependent fields live, without a reload
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
  const spawned = spawnPy(['tests.e2e.run_server', energyDb, String(energyPort), String(devicePort), 'energy'], path.join(OUT, 'server-energy.log'));
  const energyBase = `http://127.0.0.1:${energyPort}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${energyBase}/health`)).ok) break; } catch { /* not up yet */ }
    await sleep(200);
  }
  const energyPassword = /Password:\s+(\S+)/.exec(spawned.output())?.[1];
  const energyContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-GB' });
  const ep = await energyContext.newPage();
  const energyProblems = [];
  ep.on('pageerror', (e) => energyProblems.push(e.message));
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

  // The verification form must never pre-select the sign conventions or the evidence checkboxes.
  const verifyPuts = [];
  ep.on('request', (r) => { if (r.method() === 'PUT' && r.url().endsWith('/hardware-verification')) verifyPuts.push(r.postDataJSON()); });
  const uiForm = ep.locator('.energy-setup-step form');
  if (await uiForm.locator('label', { hasText: 'Device model' }).count()) {
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

  // A fresh dispatch store ships unverified hardware; verify it through the atomic admin endpoint.
  const verified = await ep.evaluate(async () => {
    const { csrf_token: csrf } = await (await fetch('/admin/api/session', { credentials: 'same-origin' })).json();
    const response = await fetch('/admin/api/energy/devices/sim/hardware-verification', {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({
        verified_device_model: 'Simulator', verified_firmware: '1.0', note: 'e2e', soc_strategy_external_code: 2,
        enum_byte_width: 1, bool_byte_width: 1, write_frame_layout_verified: true, apply_sequence_verified: true,
        battery_discharge_positive: true, grid_import_positive: true,
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
  // The simulator ships unverified hardware; verify it first, as an operator would in Expert.
  const csrf = await ep.evaluate(async () => (await (await fetch('/admin/api/session')).json()).csrf_token);
  const verification = await ep.request.put(`${energyBase}/admin/api/energy/devices/sim/hardware-verification`, {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      verified_device_model: 'Simulator', verified_firmware: '1.0', note: 'e2e simulator',
      soc_strategy_external_code: 2, enum_byte_width: 1, bool_byte_width: 1,
      write_frame_layout_verified: true, apply_sequence_verified: true,
      battery_discharge_positive: true, grid_import_positive: true,
    },
  });
  check('hardware verification endpoint accepts the evidence', verification.ok(), String(verification.status()));
  await ep.reload();
  await ep.waitForSelector('.energy-panel');
  const armToggle = ep.locator('.energy-switch input');
  await armToggle.click(); // the switch re-renders from the server state, so check() would see it revert first
  await ep.waitForFunction(() => document.querySelector('.energy-switch input')?.checked
    && [...document.querySelectorAll('.energy-actions button')].some((b) => !b.disabled), null, { timeout: 15000 }).catch(() => { });
  const afterArm = await states();
  check('arming through the switch enables the available actions', await armToggle.isChecked() && afterArm.some((disabled) => !disabled), JSON.stringify(afterArm));

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
  } else check('Charge is available once armed', false, 'disabled after arming');
  await ep.waitForFunction(() => [...document.querySelectorAll('.energy-actions button')].some((b) => b.textContent.trim() === 'Keep battery idle' && !b.disabled), null, { timeout: 10000 }).catch(() => { });
  const hold = !(await states())[1] ? await cmd(() => buttons[1].click()) : null;
  check('Keep battery idle sends a hold command without a target SoC', hold?.action === 'hold' && !('target_soc_percent' in hold), JSON.stringify(hold));
  const auto = await cmd(() => ep.locator('.energy-actions button', { hasText: 'Return to automatic' }).click());
  check('Return to automatic sends auto', auto?.action === 'auto', JSON.stringify(auto));

  // The relabels are visible on Operate (presentation-only; the REST action names stayed on the wire
  // above: hold/charge/auto). Manual-control enable/disable wording replaces the old ON/OFF switch.
  const operate = await operateText();
  check('Operate shows the Manual control relabel', /Manual control:/.test(operate), operate.slice(0, 400));
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
  check('energy page raised no script errors', energyProblems.length === 0, energyProblems.join('; '));
  await energyContext.close();
  spawned.proc.kill('SIGTERM');
}

// 7. restart persistence
await stop(server);
server = await startServer(db, httpPort, devicePort, 'second');
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

const relevant = problems.filter((p) => !/http 40[013]: .*\/admin\/api\/(settings|login)|Failed to load resource: the server responded with a status of 40[013]/.test(p));
fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, problems, relevant }, null, 2));
console.log('console/network problems:', JSON.stringify(relevant, null, 1));
check('browser console and network clean', relevant.length === 0);
await browser.close();
await stop(server);
fake.proc.kill('SIGTERM');
fs.rmSync(dbDir, { recursive: true, force: true });
const failed = results.filter((r) => !r.ok);
console.log(`${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
