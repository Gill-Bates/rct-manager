//
// tests/e2e/mobile_layout.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser E2E on a phone viewport: the Energy tabs stay on one swipeable row, the footer is one
// centred line, and every flow-graphic icon sits inside its own circle. Geometry is measured from
// the rendered boxes, so a WebKit-only text-anchor/ligature misplacement fails here.
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<python> BROWSER=webkit|chromium DEVICE='iPhone 14' \
//   COLOR_SCHEME=light|dark node tests/e2e/mobile_layout.mjs <artifact-dir>
import fs from 'node:fs';
import path from 'node:path';
import { check, devices, engine, freePort, NEW_PASSWORD, OUT, spawnPy, sleep } from './harness.mjs';

const scheme = process.env.COLOR_SCHEME || 'dark';
const tag = `${process.env.BROWSER || 'chromium'}-${scheme}`;
const device = devices[process.env.DEVICE || 'iPhone 14'];
const dbDir = fs.mkdtempSync(path.join(OUT, 'db-'));
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, 'fake.log'));
await sleep(1500);
const server = spawnPy(['tests.e2e.run_server', path.join(dbDir, 'e2e.db'), String(httpPort), String(devicePort), 'energy'], path.join(OUT, 'server.log'));
for (let i = 0; i < 100; i++) {
  try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* not up yet */ }
  await sleep(200);
}
const initial = /One-time password:\s+(\S+)/.exec(server.output())?.[1];
if (!initial) throw new Error('first-start password was not printed in the banner');

const browser = await engine.launch();
try {
  const context = await browser.newContext({ ...device, locale: 'en-GB', colorScheme: scheme });
  const page = await context.newPage();
  await page.goto(`${base}/`);
  await page.fill('#password', initial);
  await page.click('#login-form button[type=submit]');
  await page.waitForURL('**/change-password');
  await page.fill('#current-password', initial);
  await page.fill('#new-password', NEW_PASSWORD);
  await page.fill('#confirm-password', NEW_PASSWORD);
  await page.click('#change-password-form button[type=submit]');
  await page.waitForURL('**/ui/dashboard');
  try {
    await page.locator('#disclaimer-accept').waitFor({ state: 'visible', timeout: 5000 });
    await Promise.all([page.waitForNavigation(), page.click('#disclaimer-accept')]);
  } catch { /* already accepted */ }

  const vw = device.viewport.width;
  const footerGeometry = () => page.evaluate(() => {
    const footer = document.querySelector('.admin-footer');
    const range = document.createRange();
    range.selectNodeContents(footer.firstElementChild);
    const rects = [...range.getClientRects()].filter((r) => r.width > 0);
    const box = range.getBoundingClientRect();
    const tops = new Set(rects.map((r) => Math.round(r.top / 4)));
    return {
      center: box.left + box.width / 2, width: box.width, height: box.height, lines: tops.size,
      lineHeight: parseFloat(getComputedStyle(footer).lineHeight) || parseFloat(getComputedStyle(footer).fontSize) * 1.5,
      docWidth: document.documentElement.clientWidth, scrollW: document.documentElement.scrollWidth,
    };
  });
  const checkFooter = async (where) => {
    const f = await footerGeometry();
    check(`[${tag}] ${where}: footer is a single line`, f.lines === 1 && f.height <= f.lineHeight * 1.6, JSON.stringify(f));
    check(`[${tag}] ${where}: footer text is centred on the viewport`, Math.abs(f.center - f.docWidth / 2) <= 3, JSON.stringify(f));
    check(`[${tag}] ${where}: footer fits the viewport width`, f.width <= f.docWidth && f.scrollW <= f.docWidth + 1, JSON.stringify(f));
  };

  const flowGeometry = (scope) => page.evaluate((sel) => [...document.querySelectorAll(`${sel} .flow-node`)].map((node) => {
    const ring = node.querySelector('.flow-node-ring').getBoundingClientRect();
    const icon = node.querySelector('.flow-node-icon').getBoundingClientRect();
    const rc = { x: ring.left + ring.width / 2, y: ring.top + ring.height / 2 };
    return {
      label: node.querySelector('.flow-node-label').textContent, glyphs: node.querySelector('.flow-node-icon').textContent.length,
      inside: icon.width > 0 && icon.left >= ring.left - 1 && icon.right <= ring.right + 1 && icon.top >= ring.top - 1 && icon.bottom <= ring.bottom + 1,
      dx: Math.round(icon.left + icon.width / 2 - rc.x), dy: Math.round(icon.top + icon.height / 2 - rc.y), ring: Math.round(ring.width),
    };
  }), scope);
  const checkFlow = async (where, scope) => {
    await page.waitForSelector(`${scope} .flow-node`, { timeout: 30000 });
    await page.evaluate(() => document.fonts.ready);
    await sleep(500);
    const nodes = await flowGeometry(scope);
    check(`[${tag}] ${where}: flow graphic has its nodes`, nodes.length >= 3, JSON.stringify(nodes));
    for (const n of nodes) {
      check(`[${tag}] ${where}: ${n.label} icon is one glyph centred inside its circle`,
        n.glyphs === 1 && n.inside && Math.abs(n.dx) <= 3 && Math.abs(n.dy) <= 3, JSON.stringify(n));
    }
  };

  // Dashboard: flow graphic and footer.
  await page.waitForSelector('.device-flow-graphic:not([hidden]) .flow-node', { timeout: 60000 });
  await checkFlow('dashboard', '.device-flow-graphic');
  await checkFooter('dashboard');
  await page.screenshot({ path: path.join(OUT, `dashboard-${tag}.png`), fullPage: true });

  // Energy page: tabs, compact flow graphic, footer.
  await page.goto(`${base}/ui/energy`);
  await page.waitForSelector('.energy-tabs .nav-link');
  const tabs = () => page.evaluate(() => {
    const nav = document.querySelector('.energy-tabs');
    const links = [...nav.querySelectorAll('.nav-link')];
    const rects = links.map((l) => l.getBoundingClientRect());
    const navRect = nav.getBoundingClientRect();
    const cs = getComputedStyle(nav);
    const active = nav.querySelector('.nav-link.active').getBoundingClientRect();
    return {
      count: links.length, tops: rects.map((r) => Math.round(r.top)), tabHeight: rects[0].height, navHeight: navRect.height,
      scrollable: nav.scrollWidth > nav.clientWidth, overflowX: cs.overflowX, wrap: cs.flexWrap,
      activeVisible: active.left >= navRect.left - 1 && active.right <= navRect.right + 1,
      pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
  let t = await tabs();
  check(`[${tag}] tabs: all ${t.count} tabs share one row`, t.count === 4 && new Set(t.tops).size === 1, JSON.stringify(t));
  check(`[${tag}] tabs: bar is one tab tall (no wrap)`, t.navHeight <= t.tabHeight + 4, JSON.stringify(t));
  check(`[${tag}] tabs: bar scrolls horizontally without a page overflow`, t.overflowX !== 'visible' && t.wrap === 'nowrap' && !t.pageOverflow, JSON.stringify(t));
  await page.click('#energy-tab-weather');
  await page.waitForSelector('#energy-pane-weather.active');
  await sleep(600);
  t = await tabs();
  check(`[${tag}] tabs: the active tab is scrolled into view`, t.activeVisible, JSON.stringify(t));
  await page.screenshot({ path: path.join(OUT, `energy-weather-${tag}.png`) });
  await page.click('#energy-tab-inverter');
  await sleep(400);
  await page.screenshot({ path: path.join(OUT, `energy-inverter-${tag}.png`), fullPage: true });
  await checkFlow('energy page', '.energy-flow-lite');
  await checkFooter('energy page');
} finally {
  await browser.close();
}
const failed = (await import('./harness.mjs')).results.filter((r) => !r.ok);
process.exitCode = failed.length ? 1 : 0;
