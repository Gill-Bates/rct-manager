//
// tests/e2e/live-resize.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// A viewport resize driven mid-session (no reload) must reflow the dashboard and the Energy page
// without horizontal overflow and without tearing the layout down: the page is loaded once at desktop
// width, then resized through tablet, phone and back. Replaces live_resize.mjs.
import { expect, test } from "./support/fixtures.mjs";
import { collectProblems } from "./support/problems.mjs";
test.use({ gatewayMode: "planning" });
// Desktop -> tablet landscape -> tablet portrait -> phone -> back to desktop, all without a reload.
const STEPS = [[1280, 900], [1024, 768], [820, 1180], [390, 844], [1280, 900]];
const overflow = (page) => page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth));
const shot = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: false }), contentType: "image/png" });
const IGNORED = /style-src|Content Security Policy|status of 503|Failed to load resource/;
test.describe("live viewport resize @resize @theme-matrix", () => {
	test("dashboard survives live resize without overflow @resize", async ({ adminPage: page }) => {
		const problems = collectProblems(page, test.info().project.use.browserName);
		await page.goto("/ui/dashboard");
		await page.waitForSelector(".device-item");
		await page.waitForFunction(() => !document.getElementById("dashboard-grid")?.classList.contains("dashboard-grid--loading"), null, { timeout: 60_000 });
		const baseCards = await page.locator(".device-item").count();
		expect(baseCards, "dashboard renders at least one device card before resizing").toBeGreaterThanOrEqual(1);
		for (const [width, height] of STEPS) {
			await page.setViewportSize({ width, height });
			// The resize-driven reflow (gridstack / container queries) settles asynchronously: poll the measured state.
			await expect.poll(() => overflow(page), { message: `dashboard: no horizontal overflow after live resize to ${width}px` }).toBe(0);
			await expect.poll(() => page.locator(".device-item").count(), { message: `dashboard: the cards survive the live resize to ${width}px` }).toBe(baseCards);
			await shot(page, `resize-dashboard-${width}`);
		}
		expect(problems.filter((p) => !IGNORED.test(p)), "no unexpected console or page errors during the resize session").toEqual([]);
	});
	test("energy page survives live resize without overflow @resize", async ({ adminPage: page }) => {
		const problems = collectProblems(page, test.info().project.use.browserName);
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		const baseTabs = await page.locator(".energy-tabs .nav-link").count();
		for (const [width, height] of STEPS) {
			await page.setViewportSize({ width, height });
			await expect.poll(() => overflow(page), { message: `energy: no horizontal overflow after live resize to ${width}px` }).toBe(0);
			await expect.poll(() => page.locator(".energy-tabs .nav-link").count(), { message: `energy: all tabs survive the live resize to ${width}px` }).toBe(baseTabs);
			await shot(page, `resize-energy-${width}`);
		}
		expect(problems.filter((p) => !IGNORED.test(p)), "no unexpected console or page errors during the resize session").toEqual([]);
	});
});
