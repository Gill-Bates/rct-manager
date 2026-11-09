//
// tests/e2e/spot-weather-tabs.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// The planning tabs follow the inverter mode, and the Spot Market and Weather tabs of the Energy page
// render their real content (settings form, preview region, status) without console or page errors.
// The day-ahead prices and the Open-Meteo forecast come from external providers; this environment has
// no outbound network, so the live values are not retrievable. What is verifiable offline is checked:
// the tab structure, operable fields, a defined (non-broken) preview state and no runtime errors.
// Retrieving real provider values stays a network-dependent gap. Replaces spot_weather_tabs.mjs.
import { expect, test } from "./support/fixtures.mjs";
import { collectProblems, reportInverterAs } from "./support/problems.mjs";
test.use({ gatewayMode: "planning" });
const IGNORED = /style-src|Content Security Policy|status of 503|Failed to load resource|access control checks/;
const TABS = ["plan", "spot-market", "weather"];
const allTabsHidden = (page) => page.evaluate((names) => names.every((id) =>
	Boolean(document.querySelector(`#energy-tab-${id}`)?.parentElement.hidden && document.querySelector(`#energy-pane-${id}`)?.hidden)), TABS);
const allTabsVisible = (page) => page.evaluate((names) => names.every((id) =>
	document.querySelector(`#energy-tab-${id}`) && !document.querySelector(`#energy-tab-${id}`).parentElement.hidden), TABS);
const planningResponse = (page) => page.waitForResponse((response) => new URL(response.url()).pathname === "/admin/api/energy/planning-devices");
// "Hidden" is also the initial state, so after the planning response the page gets two animation frames to apply
// it (api() parse and syncPlanSelection run in microtasks/tasks right after the response) before it is asserted.
const afterPlanningApplied = (page) => page.evaluate(() => new Promise((resolve) =>
	requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 0)))));
const shot = async (page, name) => test.info().attach(`${name}.png`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
function expectClean(problems) {
	expect(problems.filter((p) => p.startsWith("pageerror:")), "no page error was raised").toEqual([]);
	expect(problems.filter((p) => !IGNORED.test(p)), "no unexpected console errors on the data-source tabs").toEqual([]);
}
// Text of a preview region once it resolved to some state (status or text), whitespace-collapsed.
async function previewState(page, prefix) {
	await page.waitForFunction((id) => (document.querySelector(`#${id}-preview-status`)?.textContent.trim()
		|| document.querySelector(`#${id}-preview`)?.textContent.trim()), prefix, { timeout: 10_000 });
	return page.evaluate((id) => ({
		previewPresent: !!document.querySelector(`#${id}-preview`),
		status: document.querySelector(`#${id}-preview-status`)?.textContent.trim() || "",
		previewText: (document.querySelector(`#${id}-preview`)?.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160),
	}), prefix);
}
test.describe("Energy page planning, Spot Market and Weather tabs @spot-weather @theme-matrix", () => {
	test("planning tabs per inverter mode (Off/External/Manual) @spot-weather", async ({ adminPage: page }) => {
		const problems = collectProblems(page, test.info().project.use.browserName);
		let energyMode = "off";
		await reportInverterAs(page, () => energyMode);
		const [firstPlanningResponse, firstResponse] = await Promise.all([planningResponse(page), page.goto("/ui/energy")]);
		const firstHtml = await firstResponse.text();
		await firstPlanningResponse.finished();
		await afterPlanningApplied(page);
		// Current product contract (commit 18b5613, energy.html + syncPlanSelection): Charging plan, Spot Market and
		// Weather appear together only while an inverter is Manual; Off and External keep all three hidden.
		// The former driver expected Spot Market and Weather to stay visible and Charging plan to show for External.
		expect(
			["plan", "spot-market", "weather"].every((name) => new RegExp(`<li[^>]*hidden[^>]*>\\s*<button[^>]*id="energy-tab-${name}"`).test(firstHtml)),
			"Charging plan, Spot Market and Weather tabs are hidden in server markup before scripts run").toBe(true);
		expect(firstPlanningResponse.ok(), `planning device list loads in Off mode (status ${firstPlanningResponse.status()})`).toBe(true);
		expect(await allTabsHidden(page), "Off mode hides the Charging plan, Spot Market and Weather tabs").toBe(true);
		energyMode = "external";
		const [externalResponse] = await Promise.all([planningResponse(page), page.reload()]);
		await externalResponse.finished();
		await afterPlanningApplied(page);
		expect(await allTabsHidden(page), "External mode keeps the Charging plan, Spot Market and Weather tabs hidden").toBe(true);
		energyMode = "manual";
		await page.reload();
		await page.waitForFunction(() => !document.querySelector("#energy-tab-plan")?.parentElement.hidden);
		expect(await allTabsVisible(page), "Manual mode shows all planning tabs").toBe(true);
		expectClean(problems);
	});
	test("Spot Market tab form and preview state @spot-weather", async ({ adminPage: page }) => {
		const problems = collectProblems(page, test.info().project.use.browserName);
		await reportInverterAs(page, () => "manual");
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		await page.click("#energy-tab-spot-market");
		await page.waitForSelector("#energy-pane-spot-market.active");
		await page.waitForSelector("#spot-market-form");
		// The form fields are rendered by admin.js; wait until at least one control attaches.
		await page.waitForFunction(() => document.querySelectorAll("#spot-market-fields input, #spot-market-fields select").length > 0);
		const preview = await previewState(page, "spot-market");
		await shot(page, "spot-market");
		const spot = await page.evaluate(() => ({
			fields: document.querySelectorAll("#spot-market-fields input, #spot-market-fields select").length,
			hasSave: !!document.querySelector("#spot-market-save"),
		}));
		await test.info().attach("spot-market.json", { body: JSON.stringify({ ...spot, ...preview }, null, 2), contentType: "application/json" });
		expect(spot.fields > 0 && spot.hasSave && preview.previewPresent, `Spot Market tab renders its settings form and preview region: ${JSON.stringify({ ...spot, ...preview })}`).toBe(true);
		expect(!/undefined|NaN|\[object/.test(preview.previewText) && (preview.status.length > 0 || preview.previewText.length > 0),
			`Spot Market preview shows a defined, non-broken state: ${JSON.stringify(preview)}`).toBe(true);
		expectClean(problems);
	});
	test("Weather tab, tab switching and console clean @spot-weather", async ({ adminPage: page }) => {
		const problems = collectProblems(page, test.info().project.use.browserName);
		await reportInverterAs(page, () => "manual");
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		// Switch Inverter -> Spot Market -> Weather, as the former driver's story did.
		await page.click("#energy-tab-spot-market");
		await page.waitForSelector("#energy-pane-spot-market.active");
		await page.click("#energy-tab-weather");
		await page.waitForSelector("#energy-pane-weather.active");
		await page.waitForFunction(() => document.querySelectorAll("#energy-pane-weather input, #energy-pane-weather select, #energy-pane-weather button").length > 0);
		const preview = await previewState(page, "weather");
		await shot(page, "weather");
		const controls = await page.locator("#energy-pane-weather input, #energy-pane-weather select, #energy-pane-weather button").count();
		await test.info().attach("weather.json", { body: JSON.stringify({ controls, ...preview }, null, 2), contentType: "application/json" });
		expect(controls > 0 && preview.previewPresent, `Weather tab renders its settings controls and preview region: ${JSON.stringify({ controls, ...preview })}`).toBe(true);
		expect(!/undefined|NaN|\[object/.test(preview.previewText) && (preview.status.length > 0 || preview.previewText.length > 0),
			`Weather preview shows a defined, non-broken state: ${JSON.stringify(preview)}`).toBe(true);
		expectClean(problems);
	});
});
