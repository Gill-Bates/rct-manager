//
// tests/e2e/admin-energy-charts.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Energy page charts: household consumption (legend, loading, empty history), spot market and weather
// previews, and the page health at 390 px. Replaces admin_flow.mjs lines 2625-2692. All data is stubbed.
// Own energy gateway with the required write registers approved and the hardware verified.
import { expect, test } from "./support/fixtures.mjs";
import { freshGateway } from "./support/fresh.mjs";
import { chartsIn, installPlanMocks } from "./support/energy-mocks.mjs";
import { acceptHardwareVerification, approveRequiredWrites } from "./support/energy-setup.mjs";
import { reportInverterAs, trackProblems } from "./support/problems.mjs";
test.describe.configure({ mode: "serial" });
test.describe("energy consumption, spot market and weather charts @energy", () => {
	const fresh = freshGateway("energy");
	let context;
	let page;
	let watch;
	let mocks;
	test.beforeAll(async ({ browser, browserName }) => {
		({ context, page } = await fresh.newPage(browser, { viewport: { width: 1280, height: 844 } }));
		watch = trackProblems(page, browserName, fresh.gateway.base);
		expect(await approveRequiredWrites(page.request), "required write registers approved").toBe(200);
		expect(await acceptHardwareVerification(page.request), "hardware verification accepted").toBe(200);
		await reportInverterAs(page, () => "manual");
		mocks = await installPlanMocks(page);
	});
	test.afterAll(async () => { await context?.close(); });

	test("consumption chart: average, coverage, legend toggles and loading state @energy", async () => {
		await page.goto("/ui/energy");
		await page.locator("#energy-tab-plan").click();
		await page.waitForSelector("#energy-plan-schedule canvas");
		await page.waitForSelector("#energy-consumption canvas");
		expect(await chartsIn(page, "#energy-consumption"), "consumption chart renders one chart").toBe(1);
		await expect(page.locator("#energy-consumption-status"), "consumption chart shows the daily average").toContainText(/kWh\/day/);
		await expect(page.locator("#energy-consumption-import-status"), "consumption chart shows the history coverage").toContainText(/History coverage · \d+\/4 days/);
		const legendState = () => page.evaluate(() => {
			const chart = window.Chart.getChart(document.querySelector("#energy-consumption canvas"));
			return [...document.querySelectorAll(".energy-consumption-legend-item")].map((button, index) => [chart.isDatasetVisible(index), button.getAttribute("aria-pressed")].join("/"));
		});
		const legendItem = (index) => page.locator(".energy-consumption-legend-item").nth(index);
		await legendItem(1).click();
		const afterBlue = await legendState();
		await legendItem(0).click();
		const afterGreen = await legendState();
		await legendItem(1).click();
		await legendItem(0).click();
		const restored = await legendState();
		expect([afterBlue, afterGreen, restored], "each consumption legend entry toggles exactly its own series, repeatedly")
			.toEqual([["true/true", "false/false"], ["false/false", "false/false"], ["true/true", "true/true"]]);
		let releaseConsumption;
		mocks.consumptionGate = new Promise((resolve) => { releaseConsumption = resolve; });
		await page.locator("#energy-plan-device").dispatchEvent("change");
		await page.waitForFunction(() => document.querySelector("#energy-consumption-section").hidden);
		expect(await chartsIn(page, "#energy-consumption"), "consumption section has no chart while the next history loads").toBe(0);
		await expect(page.locator("#energy-consumption canvas"), "consumption section has no canvas while the next history loads").toHaveCount(0);
		mocks.consumptionGate = null;
		releaseConsumption();
		await page.waitForSelector("#energy-consumption canvas");
		expect(await chartsIn(page, "#energy-consumption"), "consumption chart is drawn once after a reload").toBe(1);
	});

	test("an empty consumption history shows only the neutral coverage line @energy", async () => {
		mocks.consumption = { days: [], recording_since: null };
		await page.locator("#energy-plan-device").dispatchEvent("change");
		// An empty history keeps the section for the neutral coverage line only (no chart, no explanation).
		await expect(page.locator("#energy-consumption-import-status"), "the neutral coverage line is shown").toHaveText(/History coverage · 0\/0 days/);
		expect(await chartsIn(page, "#energy-consumption"), "without consumption data no chart is drawn").toBe(0);
		await expect(page.locator("#energy-consumption canvas"), "without consumption data no canvas exists").toHaveCount(0);
		expect(await page.locator("#energy-tab-content").innerText(), "without consumption data no explanation is shown").not.toMatch(/Recording locally|Importing daily totals|No consumption/);
	});

	test("spot market and weather previews draw one chart each and have no enable toggle @energy", async () => {
		await page.locator("#energy-tab-spot-market").click();
		await page.waitForSelector("#spot-market-chart");
		await page.waitForFunction(() => window.Chart.getChart(document.querySelector("#spot-market-chart")));
		expect(await chartsIn(page, "#energy-pane-spot-market"), "spot market preview draws exactly one chart").toBe(1);
		// The 'Enabled' toggle fell away: enablement follows the Energy Manager mode, so no toggle.
		await expect(page.locator('#spot-market-fields [data-field="enabled"]'), "the Spot Market form has no Enabled field").toHaveCount(0);
		expect(await page.locator("#spot-market-fields").getByText(/^Enabled$/).first().isVisible().catch(() => false), "the Spot Market form shows no Enabled label").toBe(false);
		await page.locator("#energy-tab-weather").click();
		await page.waitForSelector("#weather-chart");
		await page.waitForFunction(() => window.Chart.getChart(document.querySelector("#weather-chart")));
		expect(await chartsIn(page, "#energy-pane-weather"), "weather preview draws exactly one chart").toBe(1);
		await expect(page.locator('#weather-fields [data-field="enabled"]'), "the Weather form has no Enable toggle").toHaveCount(0);
		await expect(page.locator('[data-field="enabled"]'), "no enable toggle exists anywhere on the page").toHaveCount(0);
		await page.locator("#energy-tab-plan").click();
	});

	test("the energy page has no horizontal scroll at 390 px and raised no problems @energy", async () => {
		await page.setViewportSize({ width: 390, height: 844 });
		await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth),
			{ message: "energy page has no horizontal page scroll at 390px" }).toBeLessThanOrEqual(0);
		expect(watch.problems, "energy page raised no console, network or script errors").toEqual([]);
		expect([...watch.external], "no request leaves the gateway").toEqual([]);
	});
});
