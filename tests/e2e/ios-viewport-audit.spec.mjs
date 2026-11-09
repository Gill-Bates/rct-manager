//
// tests/e2e/ios-viewport-audit.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// The iOS failure classes headless WebKit does not show on its own, audited on every admin page across a
// phone/tablet device matrix (ported from fetchly's and wirebuddy's ui-lint):
//   - env(safe-area-inset-*) without viewport-fit=cover (every inset silently resolves to 0),
//   - height/min-height/max-height in vh without a dvh/svh replacement (iOS URL-bar jump),
//   - html/body locked by a viewport unit plus overflow hidden (scroll trap),
//   - text controls rendered below 16px after the content zoom (Safari zooms in on focus and stays),
//   - one scroll container really scrolling inside another one (nested scroll trap),
//   - single elements wider than the viewport outside a horizontal scroller (named, not just "the page"),
//   - buttons and form controls under 44px on a phone (the admin.css touch-target contract).
// Matrix: one test per device (RCT_E2E_DEVICES, comma list of Playwright device names; default is the six
// profiles below) and colour scheme through the projects (@theme-matrix). The former driver defaulted to
// WebKit: run it with RCT_E2E_BROWSERS=webkit. Every measurement of a device and page is an expect.soft so
// one run lists all offenders. Replaces ios_viewport_audit.mjs.
import { devices } from "@playwright/test";
import { expect, test } from "./support/fixtures.mjs";
import { acceptHardwareVerification, withAdminRequest } from "./support/energy-setup.mjs";
import { reportInverterAs } from "./support/problems.mjs";
import { settle } from "./support/settle.mjs";
test.use({ gatewayMode: "energy" });
// iPhone SE: narrowest supported phone. iPad Mini landscape: exactly 1024px, the off-by-one breakpoint
// edge. iPad Pro 11 landscape: a touch screen wider than every width breakpoint.
const DEVICE_NAMES = (process.env.RCT_E2E_DEVICES
	|| "iPhone SE,iPhone 14,iPhone 14 landscape,iPad Mini,iPad Mini landscape,iPad Pro 11 landscape")
	.split(",").map((d) => d.trim()).filter(Boolean);
const PAGES = ["dashboard", "inverters", "energy", "tsdb", "prometheus", "tokens", "settings", "about"];
const PHONE_MAX_WIDTH = 767.98; // admin.css touch-target band
// Runs in the page. Everything is measured from computed styles and rendered boxes.
function audit({ phoneMaxWidth }) {
  const vw = document.documentElement.clientWidth;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const describe = (el) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${[...el.classList].slice(0, 3).map((c) => `.${c}`).join('')}`;
  // CSS zoom compounds down the tree; the rendered size is the computed size times every ancestor zoom.
  const zoomOf = (el) => {
    let z = 1;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) z *= parseFloat(getComputedStyle(n).zoom) || 1;
    return z;
  };

  // Own stylesheets only, flattened so rules inside @media/@supports are inspected one by one.
  const rules = [];
  const walk = (list) => { for (const r of list) { if (r.cssRules?.length) walk(r.cssRules); else if (r.selectorText && r.style) rules.push(r); } };
  for (const sheet of document.styleSheets) {
    if (!sheet.href || sheet.href.includes('/vendor/')) continue;
    try { walk(sheet.cssRules); } catch { /* cross-origin */ }
  }
  const css = rules.map((r) => r.cssText).join('\n');
  const meta = document.querySelector('meta[name="viewport"]')?.content || '';

  // CSSOM keeps only the last declaration of a property, so a rule still reporting vh has no fallback.
  const vhTraps = [];
  for (const r of rules) {
    for (const prop of ['height', 'min-height', 'max-height']) {
      const v = r.style.getPropertyValue(prop);
      if (/\d(?:\.\d+)?vh\b/.test(v)) vhTraps.push(`${r.selectorText.slice(0, 80)} { ${prop}: ${v} }`);
    }
  }

  const locked = [document.documentElement, document.body].filter((el) => {
    const cs = getComputedStyle(el);
    return (cs.overflowY === 'hidden' || cs.overflow === 'hidden') && /vh/.test(el.style.height + el.style.minHeight)
      && !document.querySelector('.modal.show'); // Bootstrap locks the body while a modal is open, by design
  }).map(describe);

  // All text controls, hidden ones (closed modals, inactive tabs) included: their font does not depend on visibility.
  const smallInputs = [...document.querySelectorAll('input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=hidden]):not([type=submit]):not([type=button]):not([type=color]):not([type=file]), select, textarea')]
    .map((el) => ({ el: describe(el), px: Math.round(parseFloat(getComputedStyle(el).fontSize) * zoomOf(el) * 100) / 100 }))
    .filter((c) => c.px < 16 - 0.01);

  const scrollers = [...document.querySelectorAll('body *')].filter((el) => {
    const cs = getComputedStyle(el);
    return visible(el) && /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 2;
  });
  const nested = scrollers.filter((el) => scrollers.some((outer) => outer !== el && outer.contains(el))).map(describe);

  const horizontallyClipped = (el) => {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(n).overflowX)) return true;
    }
    return false;
  };
  const overflowing = [...document.querySelectorAll('body *')]
    .filter((el) => visible(el) && getComputedStyle(el).position !== 'fixed')
    .filter((el) => { const r = el.getBoundingClientRect(); return r.right > vw + 1 || r.left < -1; })
    .filter((el) => !horizontallyClipped(el))
    // Report the outermost offender only; its children overflow with it.
    .filter((el, _, all) => !all.some((o) => o !== el && o.contains(el)))
    .map((el) => { const r = el.getBoundingClientRect(); return `${describe(el)} [${Math.round(r.left)}..${Math.round(r.right)}]`; });

  const smallTargets = vw > phoneMaxWidth ? [] : [...document.querySelectorAll('#main-content :is(.btn, .device-toggle, .form-control, .form-select)')]
    .filter(visible)
    .map((el) => { const r = el.getBoundingClientRect(); return { el: describe(el), w: Math.round(r.width), h: Math.round(r.height) }; })
    // Height is the contract for every control; width only for buttons (inputs are full-width anyway).
    .filter((t) => t.h < 44 - 0.5 || (/^(button|a)/.test(t.el) && t.w < 44 - 0.5));

  return {
    vw, docScroll: document.documentElement.scrollWidth,
    safeAreaWithoutCover: /env\(\s*safe-area-inset-/.test(css) && !/viewport-fit\s*=\s*cover/.test(meta),
    bottomInsetDeclared: /env\(\s*safe-area-inset-bottom/.test(css),
    vhTraps, locked, smallInputs, nested, overflowing, smallTargets,
  };
}

test.describe("iOS viewport audit @ios-audit @theme-matrix", () => {
	// The Energy page only renders its full controls for a hardware-verified device (idempotent per worker).
	test.beforeAll(async ({ browser, gateway, adminState }) => {
		const status = await withAdminRequest(browser, gateway, adminState, acceptHardwareVerification);
		expect(status, "hardware verification accepted").toBe(200);
	});
	for (const name of DEVICE_NAMES) {
		test(`iOS audit: ${name} has no safe-area, viewport-unit, zoom, scroll, overflow or touch-target failure @ios-audit`, async ({ browser, gateway, adminState, colorScheme }, testInfo) => {
			const descriptor = devices[name];
			expect(descriptor, `device profile "${name}" exists in Playwright`).toBeTruthy();
			// Firefox has no isMobile; drop it there so the matrix still runs on every engine.
			const { defaultBrowserType, ...options } = descriptor;
			const browserName = testInfo.project.use.browserName;
			if (browserName === "firefox") delete options.isMobile;
			const scheme = colorScheme || "light";
			const context = await browser.newContext({ ...options, storageState: adminState, baseURL: gateway.base, locale: "en-GB", colorScheme: scheme });
			try {
				const page = await context.newPage();
				// The Charging plan, Spot Market and Weather tabs only exist while an inverter is Manual (F3).
				await reportInverterAs(page);
				const tag = `[${browserName}-${scheme}] ${name}`;
				for (const slug of PAGES) {
					await page.goto(`/ui/${slug}`);
					// No networkidle: the SSE stream keeps the network busy for good.
					await page.waitForSelector("#main-content", { timeout: 15_000 });
					await settle(page, `${tag} ${slug}: layout settles`);
					const panes = slug === "energy"
						? await page.$$eval(".energy-tabs .nav-link", (links) => links.map((l) => `#${l.id}`))
						: [null];
					if (slug === "energy") expect.soft(panes.length, `${tag}: the Energy page offers all four tabs`).toBe(4);
					for (const tab of panes) {
						if (tab) {
							await page.click(tab);
							await settle(page, `${tag} ${slug} ${tab}: tab content settles`);
						}
						const where = `${tag} ${slug}${tab ? ` ${tab}` : ""}`;
						const a = await page.evaluate(audit, { phoneMaxWidth: PHONE_MAX_WIDTH });
						// The page-wide CSS checks are identical everywhere; report them once per device.
						if (slug === PAGES[0]) {
							expect.soft(a.safeAreaWithoutCover, `${tag}: safe-area insets are live (viewport-fit=cover)`).toBe(false);
							expect.soft(a.bottomInsetDeclared, `${tag}: the stylesheet reserves safe-area-inset-bottom`).toBe(true);
							expect.soft(a.vhTraps, `${tag}: no vh height without a dvh/svh replacement`).toEqual([]);
						}
						expect.soft(a.locked, `${where}: html/body are not viewport-locked`).toEqual([]);
						expect.soft(a.smallInputs.slice(0, 8), `${where}: text controls render at >=16px (no iOS focus zoom)`).toEqual([]);
						expect.soft(a.nested, `${where}: no scroll container scrolls inside another`).toEqual([]);
						expect.soft(a.overflowing, `${where}: no element sticks out of the viewport (vw=${a.vw} scrollW=${a.docScroll})`).toEqual([]);
						expect.soft(a.docScroll, `${where}: the document is not wider than the viewport (vw=${a.vw})`).toBeLessThanOrEqual(a.vw + 1);
						if (a.vw <= PHONE_MAX_WIDTH) {
							expect.soft(a.smallTargets.slice(0, 8), `${where}: buttons and controls are >=44px touch targets`).toEqual([]);
						}
					}
					await testInfo.attach(`${slug}-${name.replace(/\W+/g, "_")}-${browserName}-${scheme}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
				}
			} finally {
				await context.close();
			}
		});
	}
});
