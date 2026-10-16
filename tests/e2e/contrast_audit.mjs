//
// tests/e2e/contrast_audit.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Measures the WCAG contrast of the admin theme's energy tab links and the Expert mode switch in a
// REAL browser, in whichever theme COLOR_SCHEME selects. This is the acceptance method for item 1:
// the admin.css tokens are only trusted once a rendered page proves their ratios, not read as text.
// Usage: PLAYWRIGHT_DIR=<dir> PYTHON=<venv python> BROWSER=chromium COLOR_SCHEME=light|dark \
//   node tests/e2e/contrast_audit.mjs <artifact-dir>
//
// Targets (WCAG 2.2): tab-link text 1.4.3 >= 4.5:1; switch outline and knob 1.4.11 >= 3:1.
// axe-core is NOT installed anywhere and MUST NOT be installed; this driver computes the contrast
// from getComputedStyle instead and the FEAT records the axe gap. A disabled control is exempt from
// the contrast minimum (WCAG 1.4.11 excludes inactive components) and is only reported, not failed.
import fs from 'node:fs';
import path from 'node:path';
import { check, engine, freePort, NEW_PASSWORD, OUT, results, sleep, spawnPy } from './harness.mjs';

const THEME = process.env.COLOR_SCHEME === 'dark' ? 'dark' : 'light';
const BROWSER = process.env.BROWSER || 'chromium';

// ---- WCAG ratio math, ported verbatim from tests/test_admin_design_rules_badges.py -------------
// contrast()/_luminance() there operate on sRGB 0-255 triples with alpha compositing over the
// background; the browser already hands us resolved rgb()/rgba() strings, so we only parse those.
function rgba(value) {
  const hex = value.trim().match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
  if (hex) {
    let h = hex[1];
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
  }
  const numbers = (value.match(/[\d.]+/g) || []).map(Number);
  return [numbers[0], numbers[1], numbers[2], numbers.length > 3 ? numbers[3] : 1];
}
function luminance([r, g, b]) {
  const lin = [r, g, b].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}
// fg composited over bg, then the standard (L1+0.05)/(L2+0.05); mirrors contrast() in the pytest.
function contrast(fg, bg) {
  const [r, g, b, a] = rgba(fg);
  const back = rgba(bg).slice(0, 3);
  const mixed = [r, g, b].map((c, i) => c * a + back[i] * (1 - a));
  const [l1, l2] = [luminance(mixed), luminance(back)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const firstStartPassword = (output) => {
  const password = /One-time password:\s+(\S+)/.exec(output)?.[1];
  if (!password) throw new Error('first-start password was not printed in the banner');
  return password;
};

const dbDir = fs.mkdtempSync(path.join(OUT, `db-${THEME}-`));
const db = path.join(dbDir, 'e2e.db');
const httpPort = await freePort();
const devicePort = await freePort();
const base = `http://127.0.0.1:${httpPort}`;

const fake = spawnPy(['tests.e2e.fake_inverter', String(devicePort)], path.join(OUT, `fake-${THEME}.log`));
// planning mode: write support off, dispatch/limits store isolated next to the throwaway db.
const server = spawnPy(['tests.e2e.run_server', db, String(httpPort), String(devicePort), 'planning'],
  path.join(OUT, `server-${THEME}.log`));

let browser;
try {
  // Application-specific readiness: wait for the health endpoint, not a bare port or networkidle.
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

  // Forced-password-change login flow (same as admin_flow.mjs).
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
  await page.waitForSelector('#energy-expert-mode');
  // Confirm the theme actually rendered the way COLOR_SCHEME asked, so a token read below is the
  // theme we claim to be measuring.
  const renderedTheme = await page.evaluate(() => document.documentElement.getAttribute('data-bs-theme'));
  check(`page renders in the ${THEME} theme`, renderedTheme === THEME, `data-bs-theme=${renderedTheme}`);

  // Resolve the effective background behind an element by walking up until a non-transparent
  // background-color is found (mirrors how a sighted user perceives the colour behind the text).
  // Defined on each page in page context; the CSP forbids injecting an inline <script>, so the
  // helper is attached via evaluate (function body, not a string tag) instead.
  await page.evaluate(() => {
    window.__effectiveBg = (el) => {
      for (let node = el; node; node = node.parentElement) {
        const bg = getComputedStyle(node).backgroundColor;
        const m = (bg.match(/[\d.]+/g) || []).map(Number);
        const alpha = m.length > 3 ? m[3] : 1;
        if (bg && bg !== 'transparent' && alpha > 0) return bg;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
  });

  const findings = { theme: THEME, browser: BROWSER, tabLink: {}, switch: {} };

  // ---- Tab link text contrast: idle, hover, active, focus (>= 4.5:1) -------------------------
  // idle = a non-active tab link; active = the .active tab. Each measured against the bg behind it.
  const idleSel = '.energy-tabs .nav-link:not(.active)';
  const activeSel = '.energy-tabs .nav-link.active';

  const readTabColor = async (selector) => page.evaluate((sel) => {
    const el = document.querySelector(sel);
    return { color: getComputedStyle(el).color, bg: window.__effectiveBg(el) };
  }, selector);

  // idle
  let m = await readTabColor(idleSel);
  findings.tabLink.idle = { color: m.color, bg: m.bg, ratio: contrast(m.color, m.bg) };

  // hover
  await page.locator(idleSel).first().hover();
  await sleep(100);
  m = await readTabColor(idleSel);
  findings.tabLink.hover = { color: m.color, bg: m.bg, ratio: contrast(m.color, m.bg) };
  await page.mouse.move(0, 0);

  // active
  m = await readTabColor(activeSel);
  findings.tabLink.active = { color: m.color, bg: m.bg, ratio: contrast(m.color, m.bg) };

  // focus (keyboard focus on an idle tab)
  await page.locator(idleSel).first().focus();
  await sleep(100);
  m = await readTabColor(idleSel);
  findings.tabLink.focus = { color: m.color, bg: m.bg, ratio: contrast(m.color, m.bg) };
  await page.locator(idleSel).first().blur();

  for (const [state, data] of Object.entries(findings.tabLink)) {
    check(`tab link ${state} text >= 4.5:1 (${THEME})`, data.ratio >= 4.5, `${data.ratio.toFixed(2)}:1 (${data.color} on ${data.bg})`);
  }

  // ---- Switch outline + knob contrast: off, on, focus, disabled (>= 3:1) ---------------------
  // The off state uses --rct-switch-off (border) and --rct-switch-knob-off (the SVG knob fill via
  // --bs-form-switch-bg). We read the computed border-color for the outline, and parse the fill
  // colour out of the --bs-form-switch-bg SVG data URI for the knob. Backgrounds measured against:
  // the effective page/card background behind the control, and the switch track (its own bg).
  const SWITCH = '#energy-expert-mode';

  // Pull the fill colour out of the knob SVG data URI (first candidate that carries one). The fill
  // can be a hex (#rrggbb, url-encoded %23) or an rgba() with alpha — e.g. Bootstrap's low-contrast
  // default knob — so both forms are captured; a translucent fill is composited by contrast().
  const knobFillFrom = (candidates) => {
    for (const raw of candidates) {
      const decoded = decodeURIComponent(raw);
      const hit = decoded.match(/fill=['"]?(#[0-9a-fA-F]{3,8}|rgba?\([^'")]+\))/);
      if (hit) return hit[1];
    }
    return null;
  };

  const readSwitch = async () => page.evaluate((sel) => {
    const el = document.querySelector(sel);
    const cs = getComputedStyle(el);
    // The knob is a background-image SVG. Bootstrap exposes it through --bs-form-switch-bg, but the
    // resolved shorthand is what actually paints; read both and keep whichever carries the data URI
    // with a fill colour, so the measurement never depends on which one the engine chose to expose.
    const knobCandidates = [cs.getPropertyValue('--bs-form-switch-bg'), cs.backgroundImage]
      .filter((v) => v && v.includes('svg'));
    return {
      border: cs.borderTopColor,
      track: cs.backgroundColor,
      knobCandidates,
      pageBg: window.__effectiveBg(el.closest('.card, .energy-panel, body') || el),
      checked: el.checked,
      disabled: el.disabled,
    };
  }, SWITCH);

  const switchState = async (name) => {
    const s = await readSwitch();
    const knobFill = knobFillFrom(s.knobCandidates);
    const outlineRatios = {
      vsPage: contrast(s.border, s.pageBg),
      vsTrack: s.track && s.track !== 'rgba(0, 0, 0, 0)' ? contrast(s.border, s.track) : null,
    };
    // The knob sits on the track; where the track is transparent (off state) the page shows through.
    const knobBg = s.track && s.track !== 'rgba(0, 0, 0, 0)' ? s.track : s.pageBg;
    const knobRatio = knobFill ? contrast(knobFill, knobBg) : null;
    return {
      name, checked: s.checked, disabled: s.disabled,
      border: s.border, track: s.track, pageBg: s.pageBg, knobFill, knobBg,
      outline: outlineRatios, knobRatio,
    };
  };

  // Settle the control into view before the first read: the knob SVG background resolves lazily and
  // a measurement taken on the first frame after navigation can miss the --bs-form-switch-bg value.
  await page.locator(SWITCH).scrollIntoViewIfNeeded();
  await sleep(150);

  // off (default, unchecked)
  findings.switch.off = await switchState('off');

  // focus (still unchecked)
  await page.locator(SWITCH).focus();
  await sleep(100);
  findings.switch.focus = await switchState('focus');

  // on (checked) — toggle via the control
  await page.locator(SWITCH).check();
  await sleep(150);
  findings.switch.on = await switchState('on');
  await page.locator(SWITCH).uncheck();
  await sleep(150);

  // disabled — set programmatically so the disabled rendering can be captured (reported, exempt).
  await page.evaluate((sel) => { document.querySelector(sel).disabled = true; }, SWITCH);
  await sleep(100);
  findings.switch.disabled = await switchState('disabled');
  await page.evaluate((sel) => { document.querySelector(sel).disabled = false; }, SWITCH);

  // The off-state outline/knob are the acceptance-relevant tokens for item 1. off, focus and on are
  // held to >= 3:1 (1.4.11); disabled is exempt and only reported.
  // The off/focus states are item 1's own tokens (--rct-switch-off outline, --rct-switch-knob-off
  // knob); both the outline (vs page and vs the transparent track's effective background) and the
  // knob are gated at >= 3:1. The checked (on) state deliberately keeps Bootstrap's filled brand
  // look (item 1 does not restyle it): its "on" affordance is the white knob against the brand
  // track (gated), and its outer boundary is the brand track against the page — the same
  // brand-primary-on-surface consideration as every .btn-primary, governed by --rct-brand and out
  // of item 1's scope, so it is recorded as informational, not gated.
  for (const state of ['off', 'focus']) {
    const d = findings.switch[state];
    const outlineOk = d.outline.vsPage >= 3 && (d.outline.vsTrack === null || d.outline.vsTrack >= 3);
    check(`switch ${state} outline >= 3:1 (${THEME})`, outlineOk,
      `vsPage ${d.outline.vsPage.toFixed(2)}:1${d.outline.vsTrack !== null ? `, vsTrack ${d.outline.vsTrack.toFixed(2)}:1` : ''} (${d.border})`);
    if (d.knobRatio !== null) {
      check(`switch ${state} knob >= 3:1 (${THEME})`, d.knobRatio >= 3,
        `${d.knobRatio.toFixed(2)}:1 (${d.knobFill} on ${d.knobBg})`);
    } else {
      check(`switch ${state} knob fill resolved (${THEME})`, false, 'knob fill colour could not be parsed');
    }
  }
  const on = findings.switch.on;
  check(`switch on knob >= 3:1 (${THEME})`, on.knobRatio !== null && on.knobRatio >= 3,
    `${on.knobRatio !== null ? on.knobRatio.toFixed(2) : 'n/a'}:1 (${on.knobFill} on ${on.knobBg})`);
  console.log(`INFO switch on brand track vs page (informational, --rct-brand, out of item 1 scope): ${on.outline.vsPage.toFixed(2)}:1 (${on.border} on ${on.pageBg})`);
  const dis = findings.switch.disabled;
  console.log(`INFO switch disabled (exempt, reported only): outline vsPage ${dis.outline.vsPage.toFixed(2)}:1, knob ${dis.knobRatio ? dis.knobRatio.toFixed(2) + ':1' : 'n/a'}`);

  // axe-core gap: record it explicitly in the run artifacts, since no scanner is available.
  findings.axeCore = 'NOT INSTALLED and must not be installed; contrast computed from getComputedStyle via the ported WCAG ratio math instead.';

  fs.writeFileSync(path.join(OUT, `contrast-${THEME}.json`), JSON.stringify(findings, null, 2));
  await page.screenshot({ path: path.join(OUT, `energy-${THEME}.png`), fullPage: true });
} finally {
  if (browser) await browser.close();
  for (const proc of [server.proc, fake.proc]) if (proc.exitCode === null) proc.kill('SIGTERM');
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${THEME}: ${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
