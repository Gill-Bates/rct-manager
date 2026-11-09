//
// tests/e2e/mobile-layout.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Phone viewport: the Energy tabs stay on one swipeable row, the footer is one centred line and every
// flow-graphic icon sits inside its own circle. Geometry is measured from the rendered boxes, so a
// WebKit-only text-anchor/ligature misplacement fails here.
// Matrix: RCT_E2E_DEVICE (Playwright device name, default "iPhone 14"), colour scheme through the projects
// (@theme-matrix; the former driver defaulted to dark, the dark project covers that cell). Replaces
// mobile_layout.mjs.
import { devices } from "@playwright/test";
import { expect, test as base } from "./support/fixtures.mjs";
import { acceptHardwareVerification, approveRequiredWrites, withAdminRequest } from "./support/energy-setup.mjs";
import { reportInverterAs } from "./support/problems.mjs";
import { frames, settle } from "./support/settle.mjs";
const DEVICE = process.env.RCT_E2E_DEVICE || "iPhone 14";
const test = base.extend({
	// A logged-in phone page: device profile of RCT_E2E_DEVICE, project colour scheme.
	phone: async ({ browser, gateway, adminState, colorScheme }, use, testInfo) => {
		const descriptor = devices[DEVICE];
		expect(descriptor, `device profile "${DEVICE}" exists in Playwright`).toBeTruthy();
		const { defaultBrowserType, ...options } = descriptor;
		if (testInfo.project.use.browserName === "firefox") delete options.isMobile;
		const context = await browser.newContext({ ...options, storageState: adminState, baseURL: gateway.base, locale: "en-GB", colorScheme: colorScheme || "light" });
		try {
			await use(await context.newPage());
		} finally {
			await context.close();
		}
	},
});
test.use({ gatewayMode: "energy" });
const shot = async (page, name, fullPage = false) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage }), contentType: "image/png" });
const footerGeometry = (page) => page.evaluate(() => {
	const footer = document.querySelector(".admin-footer");
	const range = document.createRange();
	// Visible text only: stop after "GitHub" so the visually-hidden new-tab hint is not measured.
	const link = footer.querySelector("a");
	range.setStartBefore(footer.firstElementChild.firstChild);
	range.setEndAfter([...link.childNodes].find((n) => n.nodeType === Node.TEXT_NODE));
	const rects = [...range.getClientRects()].filter((r) => r.width > 0);
	const box = range.getBoundingClientRect();
	// Lines = distinct vertical bands of the text boxes (the inline icon box is offset by a few px).
	const bands = rects.filter((r) => r.height > 8).sort((a, b) => a.top - b.top)
		.reduce((n, r, i, all) => (i && r.top < all[i - 1].bottom - r.height / 2 ? n : n + 1), 0);
	return {
		center: box.left + box.width / 2, width: box.width, height: box.height, lines: bands,
		lineHeight: parseFloat(getComputedStyle(footer).lineHeight) || parseFloat(getComputedStyle(footer).fontSize) * 1.5,
		docWidth: document.documentElement.clientWidth, scrollW: document.documentElement.scrollWidth,
	};
});
async function checkFooter(page, where) {
	const f = await footerGeometry(page);
	const detail = JSON.stringify(f);
	expect.soft(f.lines, `${where}: footer is a single line: ${detail}`).toBe(1);
	expect.soft(f.height, `${where}: footer height is at most 1.6 line heights: ${detail}`).toBeLessThanOrEqual(f.lineHeight * 1.6);
	expect.soft(Math.abs(f.center - f.docWidth / 2), `${where}: footer text is centred on the viewport (<= 3px): ${detail}`).toBeLessThanOrEqual(3);
	expect.soft(f.width, `${where}: footer fits the viewport width: ${detail}`).toBeLessThanOrEqual(f.docWidth);
	expect.soft(f.scrollW, `${where}: footer causes no horizontal scroll: ${detail}`).toBeLessThanOrEqual(f.docWidth + 1);
}
const flowGeometry = (page, scope) => page.evaluate((sel) => [...document.querySelectorAll(`${sel} .flow-node`)].map((node) => {
	const ring = node.querySelector(".flow-node-ring").getBoundingClientRect();
	const icon = node.querySelector(".flow-node-icon").getBoundingClientRect();
	const rc = { x: ring.left + ring.width / 2, y: ring.top + ring.height / 2 };
	return {
		label: node.querySelector(".flow-node-label").textContent, glyphs: node.querySelector(".flow-node-icon").textContent.length,
		inside: icon.width > 0 && icon.left >= ring.left - 1 && icon.right <= ring.right + 1 && icon.top >= ring.top - 1 && icon.bottom <= ring.bottom + 1,
		dx: Math.round(icon.left + icon.width / 2 - rc.x), dy: Math.round(icon.top + icon.height / 2 - rc.y), ring: Math.round(ring.width),
	};
}), scope);
async function checkFlow(page, where, scope) {
	await page.waitForSelector(`${scope} .flow-node`, { timeout: 30_000 });
	await settle(page, `${where}: flow graphic settles`);
	const nodes = await flowGeometry(page, scope);
	expect.soft(nodes.length, `${where}: flow graphic has its nodes: ${JSON.stringify(nodes)}`).toBeGreaterThanOrEqual(3);
	for (const n of nodes) {
		const detail = JSON.stringify(n);
		expect.soft(n.glyphs, `${where}: ${n.label} icon is one glyph: ${detail}`).toBe(1);
		expect.soft(n.inside, `${where}: ${n.label} icon is inside its circle: ${detail}`).toBe(true);
		expect.soft(Math.abs(n.dx), `${where}: ${n.label} icon is centred horizontally (<= 3px): ${detail}`).toBeLessThanOrEqual(3);
		expect.soft(Math.abs(n.dy), `${where}: ${n.label} icon is centred vertically (<= 3px): ${detail}`).toBeLessThanOrEqual(3);
	}
}
test.describe("phone layout @mobile @theme-matrix", () => {
	// The compact flow view only renders once the required write registers are approved and the hardware is
	// verified, so both are seeded through the admin API like an operator would (idempotent per worker).
	test.beforeAll(async ({ browser, gateway, adminState }) => {
		await withAdminRequest(browser, gateway, adminState, async (request) => {
			expect(await approveRequiredWrites(request), "required write registers approved").toBe(200);
			expect(await acceptHardwareVerification(request), "hardware verification accepted").toBe(200);
		});
	});
	test("dashboard: flow graphic icons centred in their circles and one-line footer @mobile", async ({ phone: page }) => {
		await page.goto("/ui/dashboard");
		await page.waitForSelector(".device-flow-graphic:not([hidden]) .flow-node", { timeout: 60_000 });
		await checkFlow(page, "dashboard", ".device-flow-graphic");
		await checkFooter(page, "dashboard");
		await shot(page, "dashboard", true);
	});
	test("energy tabs: one swipeable row @mobile", async ({ phone: page }) => {
		await reportInverterAs(page);
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		const tabs = () => page.evaluate(() => {
			const nav = document.querySelector(".energy-tabs");
			const links = [...nav.querySelectorAll(".nav-link")];
			const rects = links.map((l) => l.getBoundingClientRect());
			const navRect = nav.getBoundingClientRect();
			const cs = getComputedStyle(nav);
			const active = nav.querySelector(".nav-link.active").getBoundingClientRect();
			return {
				count: links.length, tops: rects.map((r) => Math.round(r.top)), tabHeight: rects[0].height, navHeight: navRect.height,
				scrollable: nav.scrollWidth > nav.clientWidth, overflowX: cs.overflowX, wrap: cs.flexWrap, scrollLeft: nav.scrollLeft,
				activeVisible: active.left >= navRect.left - 1 && active.right <= navRect.right + 1,
				pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
			};
		});
		let t = await tabs();
		let detail = JSON.stringify(t);
		expect.soft(t.count, `tabs: the bar has 4 tabs: ${detail}`).toBe(4);
		expect.soft(new Set(t.tops).size, `tabs: all ${t.count} tabs share one row: ${detail}`).toBe(1);
		expect.soft(t.navHeight, `tabs: bar is one tab tall (no wrap): ${detail}`).toBeLessThanOrEqual(t.tabHeight + 4);
		expect.soft(t.overflowX, `tabs: bar scrolls horizontally (overflow-x not visible): ${detail}`).not.toBe("visible");
		expect.soft(t.wrap, `tabs: bar does not wrap: ${detail}`).toBe("nowrap");
		expect.soft(t.pageOverflow, `tabs: no page overflow: ${detail}`).toBe(false);
		await page.click("#energy-tab-weather");
		await page.waitForSelector("#energy-pane-weather.active");
		// The bar scrolls the activated tab into view (smooth scroll): poll the measured state.
		await expect.poll(async () => (await tabs()).activeVisible, { message: "tabs: the active tab is scrolled into view" }).toBe(true);
		await settle(page, "tab bar scroll settles");
		t = await tabs();
		detail = JSON.stringify(t);
		expect.soft(!t.scrollable || t.scrollLeft > 0, `tabs: the bar scrolled to reveal the last tab: ${detail}`).toBe(true);
		expect.soft(t.activeVisible, `tabs: the active tab is scrolled into view: ${detail}`).toBe(true);
		await shot(page, "energy-weather");
	});
	test("plan header: no manager badge, no preliminary note @mobile", async ({ phone: page }) => {
		await reportInverterAs(page);
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		await page.click("#energy-tab-plan");
		await page.waitForSelector("#energy-pane-plan.active");
		await page.waitForSelector("#energy-plan-title", { state: "visible" });
		const planHead = await page.evaluate(() => ({
			badge: Boolean(document.getElementById("energy-plan-manager-state")),
			pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
		}));
		expect.soft(planHead.badge, `plan header: no manager badge: ${JSON.stringify(planHead)}`).toBe(false);
		expect.soft(planHead.pageOverflow, `plan header: no page overflow: ${JSON.stringify(planHead)}`).toBe(false);
		await settle(page, "plan card settles");
		expect.soft(await page.locator("#energy-plan-confidence-note").count(), "plan header: no preliminary-plan note element").toBe(0);
		expect.soft(await page.locator("#energy-plan-card").innerText(), "plan header: no preliminary-plan wording").not.toMatch(/Preliminary plan|limited consumption history/i);
		await shot(page, "energy-plan");
	});
	test("energy page: compact flow view and wrapped action buttons @mobile", async ({ phone: page }) => {
		await reportInverterAs(page);
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		await settle(page, "inverter tab settles");
		await shot(page, "energy-inverter", true);
		// The compact view shares energyFlowGraphic() with the dashboard but needs approved write registers.
		const liteScope = ".energy-flow-lite:not([hidden])";
		await page.waitForSelector(`${liteScope} .flow-node`, { state: "attached", timeout: 20_000 });
		await page.locator(liteScope).scrollIntoViewIfNeeded();
		await checkFlow(page, "energy page", liteScope);
		const lite = await page.evaluate((sel) => {
			const box = document.querySelector(sel).getBoundingClientRect();
			return { left: box.left, right: box.right, width: box.width, docWidth: document.documentElement.clientWidth };
		}, liteScope);
		const liteDetail = JSON.stringify(lite);
		expect.soft(lite.width, `energy page: compact flow view is rendered: ${liteDetail}`).toBeGreaterThan(0);
		expect.soft(lite.left, `energy page: compact flow view fits the viewport (left): ${liteDetail}`).toBeGreaterThanOrEqual(-1);
		expect.soft(lite.right, `energy page: compact flow view fits the viewport (right): ${liteDetail}`).toBeLessThanOrEqual(lite.docWidth + 1);
		// A word that spans two line boxes was broken mid-word.
		const actions = await page.evaluate(() => [...document.querySelectorAll(".energy-actions .btn")].map((b) => {
			const text = b.firstChild;
			const broken = [...text.data.matchAll(/\S+/g)].filter((m) => {
				const r = document.createRange();
				r.setStart(text, m.index);
				r.setEnd(text, m.index + m[0].length);
				return r.getClientRects().length > 1;
			}).map((m) => m[0]);
			return { label: b.textContent.trim(), broken, overflow: b.scrollWidth > b.clientWidth + 1 };
		}));
		const actionDetail = JSON.stringify(actions);
		expect.soft(actions.length, `energy page: three action buttons: ${actionDetail}`).toBe(3);
		expect.soft(actions.filter((a) => a.broken.length || a.overflow), `energy page: action buttons wrap between words, never inside one: ${actionDetail}`).toEqual([]);
		await shot(page, "energy-flow");
		await frames(page);
		await checkFooter(page, "energy page");
	});
});
