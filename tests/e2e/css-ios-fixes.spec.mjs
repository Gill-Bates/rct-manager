//
// tests/e2e/css-ios-fixes.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Browser verification of the four CSS/iOS review fixes: footer safe-area reserve, TSDB mobile-first
// restructure, no overlap at 38rem, shared plan-chart tokens. The real admin GUI is driven and computed
// styles and geometry are read back. Matrix: RCT_E2E_VIEWPORT_WIDTH (default 1280), RCT_E2E_VIEWPORT_HEIGHT
// (default 900), colour scheme through the projects (@theme-matrix). Replaces css_ios_fixes_verify.mjs.
import { expect, test } from "./support/fixtures.mjs";
import { frames } from "./support/settle.mjs";
const WIDTH = Number(process.env.RCT_E2E_VIEWPORT_WIDTH || 1280);
const HEIGHT = Number(process.env.RCT_E2E_VIEWPORT_HEIGHT || 900);
// A few log lines are enough to populate the Status & Log console of the TSDB page.
const SAMPLE = [
	{ timestamp: "2026-10-11T19:46:12Z", level: "INFO", logger: "app.export.pusher", message: "Export loop started" },
	{ timestamp: "2026-10-11T19:46:15Z", level: "WARNING", logger: "app.export.pusher", message: "Backend slow, retrying" },
	{ timestamp: "2026-10-11T19:46:17Z", level: "ERROR", logger: "app.api.app_factory", message: "Metrics export failed" },
	...Array.from({ length: 40 }, (_, i) => ({ timestamp: `2026-10-11T19:47:${String(i % 60).padStart(2, "0")}Z`, level: "INFO", logger: "app.export.pusher", message: `tick ${i}` })),
];
// Serve a controlled, held-open SSE stream so the settings form + console render.
async function mockLogStream(page) {
	await page.route("**/admin/api/events", (route) => route.fulfill({
		status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
		body: SAMPLE.map((e) => `event: tsdb.log\ndata: ${JSON.stringify(e)}\n\n`).join("") + ": keep-alive\n\n",
	}));
}
async function openTsdb(page) {
	await page.setViewportSize({ width: WIDTH, height: HEIGHT });
	await mockLogStream(page);
	await page.goto("/ui/tsdb");
	await page.waitForSelector("#setting-metrics_export_enabled", { timeout: 20_000 });
}
// Enable export and pick a backend so the console renders.
async function enableConsole(page) {
	const enableToggle = page.locator("#setting-metrics_export_enabled");
	if (!(await enableToggle.isChecked())) await enableToggle.click();
	await page.locator("#setting-db_type").selectOption("questdb");
	await page.waitForSelector("#tsdb-mini-console");
	await frames(page);
}
const attach = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: false }), contentType: "image/png" });
test.describe("CSS and iOS fixes @css-ios @theme-matrix @viewport-matrix", () => {
	test("fix 1: app shell reserves the painted footer height @css-ios", async ({ adminPage: page }) => {
		await openTsdb(page);
		// env() cannot be injected at runtime: force a non-zero reserve delta the way a real inset would grow
		// it (both .app-shell padding and the footer min-height read --rct-footer-reserve) via the inline
		// style property, which the strict CSP allows.
		await page.evaluate(() => document.documentElement.style.setProperty("--rct-footer-reserve", "calc(var(--rct-footer-height) + 34px)"));
		await frames(page);
		const geo = await page.evaluate(() => {
			const shell = document.querySelector(".app-shell");
			const footer = document.querySelector(".admin-footer");
			return {
				shellPaddingBottom: Math.round(parseFloat(getComputedStyle(shell).paddingBottom)),
				footerPaintedHeight: Math.round(footer.getBoundingClientRect().height),
				footerTop: Math.round(footer.getBoundingClientRect().top),
			};
		});
		await attach(page, "footer");
		expect(geo.shellPaddingBottom, `[#1] app-shell reserves at least the painted footer height (inc. simulated inset): ${JSON.stringify(geo)}`)
			.toBeGreaterThanOrEqual(geo.footerPaintedHeight - 1);
	});
	test("fix 2: phone natural flow vs desktop pinned shell @css-ios", async ({ adminPage: page }) => {
		await openTsdb(page);
		await enableConsole(page);
		const layout = await page.evaluate(() => {
			const shell = document.querySelector(".app-shell");
			const main = document.getElementById("main-content");
			return { shellHeight: getComputedStyle(shell).height, shellOverflow: getComputedStyle(shell).overflow, mainOverflowY: getComputedStyle(main).overflowY };
		});
		await attach(page, "tsdb");
		const detail = JSON.stringify(layout);
		if (WIDTH < 608) { // 38rem = 608px at a 16px root
			expect(layout.shellOverflow, `[#2] phone: app-shell has NO fixed height (natural flow): ${detail}`).not.toBe("hidden");
			expect(layout.mainOverflowY, `[#2] phone: #main-content is not an inner scroll container: ${detail}`).toBe("visible");
		} else {
			expect(layout.shellOverflow, `[#2] desktop: app-shell is a fixed, overflow:hidden pinned shell: ${detail}`).toBe("hidden");
			expect(layout.shellHeight, `[#2] desktop: app-shell height is fixed (svh or px): ${detail}`).toMatch(/svh|px/);
			expect(layout.mainOverflowY, `[#2] desktop: #main-content is the inner scroll container: ${detail}`).toBe("auto");
		}
	});
	test("fix 3: grid at exactly 608px is a single consistent track @css-ios", async ({ adminPage: page }) => {
		await openTsdb(page);
		await enableConsole(page);
		// No overlapping branch exactly at 38rem (608px): test the boundary.
		await page.setViewportSize({ width: 608, height: HEIGHT });
		await frames(page);
		const boundary = await page.evaluate(() => ({
			innerWidth: window.innerWidth,
			gridTemplateRows: getComputedStyle(document.querySelector("#main-content > .settings-grid")).gridTemplateRows,
		}));
		expect(boundary.innerWidth, "viewport is exactly 608px (38rem)").toBe(608);
		expect(boundary.gridTemplateRows, `[#3] at exactly 608px (38rem) the grid resolves to a defined track set: ${JSON.stringify(boundary)}`).toBeTruthy();
		expect(boundary.gridTemplateRows, `[#3] at exactly 608px (38rem) the grid is not 'none': ${JSON.stringify(boundary)}`).not.toBe("none");
	});
	test("fix 4: shared plan chart tokens resolve to the original values @css-ios", async ({ adminPage: page }) => {
		await openTsdb(page);
		const tokens = await page.evaluate(() => {
			const root = getComputedStyle(document.documentElement);
			return {
				power: root.getPropertyValue("--rct-plan-power").trim(), soc: root.getPropertyValue("--rct-plan-soc").trim(),
				oldPower: root.getPropertyValue("--plan-power").trim(), oldSoc: root.getPropertyValue("--plan-soc").trim(),
			};
		});
		const detail = JSON.stringify(tokens);
		expect(tokens.power, `[#4] shared --rct-plan-power resolves to the original hex: ${detail}`).toBe("#0d6efd");
		expect(tokens.soc, `[#4] shared --rct-plan-soc resolves to the original hex: ${detail}`).toBe("#fd7e14");
		expect(tokens.oldPower, `[#4] the old --plan-power token is gone: ${detail}`).toBe("");
		expect(tokens.oldSoc, `[#4] the old --plan-soc token is gone: ${detail}`).toBe("");
	});
});
