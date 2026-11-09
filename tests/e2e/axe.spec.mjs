//
// tests/e2e/axe.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Automated accessibility scan (axe-core via @axe-core/playwright, WCAG 2.x A/AA tags) of the login page
// before authentication, the dashboard and the four Energy tabs. A page fails on any violation of impact
// serious or critical; moderate and minor ones are attached to the test as JSON and listed in the
// message, not failed (the project's advisory a11y policy, unchanged from the former driver).
// Matrix: colour scheme through the projects (@theme-matrix). Replaces axe_scan.mjs.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./support/fixtures.mjs";
import { reportInverterAs } from "./support/problems.mjs";
import { settle } from "./support/settle.mjs";
test.use({ gatewayMode: "planning" });
const FAIL_IMPACTS = new Set(["serious", "critical"]);
async function scan(page, label) {
	const out = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
	const summary = out.violations.map((v) => ({
		id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help, helpUrl: v.helpUrl,
		targets: v.nodes.map((n) => ({ target: n.target, summary: n.failureSummary })),
	}));
	await test.info().attach(`axe-${label}.json`, { body: JSON.stringify(summary, null, 2), contentType: "application/json" });
	await test.info().attach(`axe-${label}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
	const blocking = summary.filter((v) => FAIL_IMPACTS.has(v.impact));
	const detail = summary.length ? summary.map((v) => `${v.impact}:${v.id}(${v.nodes})`).join(", ") : "no violations";
	expect(blocking.map((v) => `${v.impact}:${v.id}(${v.nodes})`), `axe ${label} has no serious/critical violation (${detail})`).toEqual([]);
}
test.describe("axe accessibility scan @a11y @theme-matrix", () => {
	test("axe: login page has no serious or critical violation @a11y", async ({ browser, gateway, colorScheme }) => {
		// Before authentication: a fresh context without the admin session.
		const context = await browser.newContext({ baseURL: gateway.base, viewport: { width: 1280, height: 900 }, locale: "en-GB", colorScheme: colorScheme || "light" });
		try {
			const page = await context.newPage();
			await page.goto("/login");
			await page.waitForSelector("#login-form");
			await scan(page, "login");
		} finally {
			await context.close();
		}
	});
	test("axe: dashboard has no serious or critical violation @a11y", async ({ adminPage: page }) => {
		await page.goto("/ui/dashboard");
		await page.waitForSelector("#dashboard-edit-toggle");
		await page.waitForFunction(() => !document.getElementById("dashboard-grid")?.classList.contains("dashboard-grid--loading"), null, { timeout: 60_000 });
		await scan(page, "dashboard");
	});
	test("axe: energy inverter tab has no serious or critical violation @a11y", async ({ adminPage: page }) => {
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		await page.waitForSelector("#energy-list", { timeout: 15_000 });
		await scan(page, "energy-inverter");
	});
	for (const tab of ["spot-market", "weather", "plan"]) {
		test(`axe: energy ${tab} tab has no serious or critical violation @a11y`, async ({ adminPage: page }) => {
			await page.goto("/ui/energy");
			await page.waitForSelector(".energy-tabs .nav-link");
			// The Spot Market, Weather and Charging plan tabs are hidden until an inverter is Manual (product contract, F3).
			await reportInverterAs(page);
			await page.reload();
			await page.waitForSelector(`#energy-tab-${tab}`, { state: "visible" });
			await page.click(`#energy-tab-${tab}`);
			await page.waitForSelector(`#energy-pane-${tab}.active`, { timeout: 10_000 });
			// The tab's lazy content (preview placeholder / plan controls) attaches after the switch.
			await settle(page, `energy ${tab} tab content settles`);
			await scan(page, `energy-${tab}`);
		});
	}
});
