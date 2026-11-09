//
// tests/e2e/plan-chart-cases.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Three Charging-plan chart behaviours that the admin flow does not cover on their own: the plan chart
// renders as a real Chart.js instance; a 25-hour DST fall-back day (Europe/Berlin, 2026-10-25) renders
// without a collapsing or overflowing time axis; the null/NaN tooltip filter shows no tooltip for a slot
// gap and a NaN-power slot never crashes the chart. The data is supplied to the real /dynamic-plan
// endpoint shape via page.route(), so the SHIPPED client rendering (drawPlanChart, planChartSeries, the
// tooltip filter in admin-energy-setup.js) is exercised; a provider-fed `ok` plan is a separate gap.
// Replaces plan_chart_cases.mjs.
import { expect, test } from "./support/fixtures.mjs";
import { collectProblems, reportInverterAs } from "./support/problems.mjs";
test.use({ gatewayMode: "planning" });
// A plan spanning the 25-hour Europe/Berlin fall-back day on 2026-10-25 (CEST->CET at 01:00 UTC).
// Times are UTC instants; the chart plots a real time axis, so the local day is 25 h wide. The last
// idle slot is deliberately NOT adjacent to the previous one, forcing an all-null gap point for the
// tooltip-filter test. One charge slot carries a non-numeric power to prove NaN never crashes.
const slot = (start, end, action, maxPowerW, targetSoc, reason) => ({ start, end, action, max_power_w: maxPowerW, target_soc_percent: targetSoc, reason });
const PLAN = {
	device_id: "sim", status: "ok", detail: null,
	created_at: "2026-10-24T22:00:00+00:00", valid_until: "2026-10-26T00:00:00+00:00",
	horizon_hours: 25, preview_only: true, forecast_basis: "stored history", forecast_confidence: "high",
	consumption_history_available: true, policy: "gentle", reserve_soc_percent: 10,
	window: {
		consumption_from: "2026-10-24T22:00:00+00:00", consumption_until: "2026-10-25T23:00:00+00:00",
		solar_from: "2026-10-24T22:00:00+00:00", solar_until: "2026-10-25T23:00:00+00:00",
		prices_from: "2026-10-24T22:00:00+00:00", prices_until: "2026-10-25T23:00:00+00:00",
	},
	energy: { grid_charge_energy_wh: 5400 },
	slots: [
		slot("2026-10-24T22:00:00+00:00", "2026-10-24T23:30:00+00:00", "idle", null, null, "forecast PV covers demand"),
		slot("2026-10-24T23:30:00+00:00", "2026-10-25T01:30:00+00:00", "charge", 3000, 62, "negative spot price, forecast deficit"),
		// A NaN power must not crash the chart (planNumber -> NaN -> treated as 0 kW, finite guard holds).
		slot("2026-10-25T01:30:00+00:00", "2026-10-25T03:00:00+00:00", "charge", "not-a-number", 70, "forecast deficit, charged as late as permitted"),
		slot("2026-10-25T03:00:00+00:00", "2026-10-25T12:00:00+00:00", "idle", null, null, "battery reserve covers demand"),
		// GAP: this slot does not start where the previous ended -> an all-null point is inserted here.
		slot("2026-10-25T14:00:00+00:00", "2026-10-25T23:00:00+00:00", "idle", null, null, "battery reserve covers demand"),
	],
	price_disclaimer: null,
};
const chartState = (page) => page.evaluate(() => {
	const canvas = document.querySelector("#energy-plan-schedule canvas");
	const chart = canvas && window.Chart.getChart(canvas);
	if (!chart) return null;
	const x = chart.options.scales.x;
	return { spanHours: (x.max - x.min) / 3600000, datasets: chart.data.datasets.map((d) => d.data.length) };
});
const pageOverflow = (page) => page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth));
const fitAt = (page) => page.evaluate(() => {
	const vw = document.documentElement.clientWidth;
	const cells = [...document.querySelectorAll("#energy-plan-schedule table td")];
	// Purpose/Reason prose must wrap inside its cell: no cell wider than its box or the viewport.
	const prose = [...document.querySelectorAll("#energy-plan-schedule td.data-table-stack-prose")];
	return {
		overflow: Math.max(0, document.documentElement.scrollWidth - vw),
		rows: document.querySelectorAll("#energy-plan-schedule details tbody tr").length,
		cellsOutside: cells.filter((c) => c.getBoundingClientRect().right > vw + 0.5).length,
		proseOverflowing: prose.filter((c) => c.scrollWidth > c.clientWidth + 1).length,
		proseCells: prose.length,
	};
});
const frames = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
test.describe("Charging plan chart cases @plan-chart @theme-matrix", () => {
	// Timezone Europe/Berlin is fixed for the DST day, so these tests use their own context.
	async function openPlan({ browser, adminState, gateway, colorScheme }) {
		const context = await browser.newContext({
			storageState: adminState, baseURL: gateway.base, viewport: { width: 1280, height: 900 },
			locale: "en-GB", colorScheme: colorScheme || "light", timezoneId: "Europe/Berlin",
		});
		const page = await context.newPage();
		const collected = collectProblems(page, test.info().project.use.browserName);
		// The dynamic-plan response carries the DST/NaN/gap plan; the real client renders it.
		await page.route("**/dynamic-plan**", (route) => route.fulfill({ json: PLAN }));
		await reportInverterAs(page, () => "manual");
		await page.goto("/ui/energy");
		await page.waitForSelector(".energy-tabs .nav-link");
		await page.click("#energy-tab-plan");
		await page.waitForSelector("#energy-pane-plan.active");
		// The plan loads when a device is chosen; wait for the selector, then for the rendered chart.
		await page.waitForSelector("#energy-plan-device option", { state: "attached" });
		await page.waitForSelector("#energy-plan-schedule canvas");
		await page.locator("#energy-plan-schedule canvas").scrollIntoViewIfNeeded();
		await frames(page);
		return { context, page, collected };
	}
	test("plan chart renders as a Chart.js instance @plan-chart", async ({ browser, adminState, gateway, colorScheme }) => {
		const { context, page, collected } = await openPlan({ browser, adminState, gateway, colorScheme });
		try {
			await test.info().attach("plan-chart.png", { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
			const chartCount = await page.evaluate(() => [...document.querySelectorAll("#energy-plan-schedule canvas")]
				.filter((c) => window.Chart.getChart(c)).length);
			expect(chartCount, "plan/forecast chart renders as a real Chart.js instance (exactly one)").toBe(1);
			expect(collected.filter((p) => !/style-src|Content Security Policy|status of 503|Failed to load resource/.test(p)), "no unexpected console or page errors").toEqual([]);
		} finally { await context.close(); }
	});
	test("DST fall-back day renders a >24 h axis without overflow @plan-chart", async ({ browser, adminState, gateway, colorScheme }) => {
		const { context, page } = await openPlan({ browser, adminState, gateway, colorScheme });
		try {
			// The DST day spans 25 local hours; the axis is wider than 24 h and the chart did not collapse.
			const state = await chartState(page);
			expect(state !== null && state.spanHours > 24 && state.spanHours <= 26, `DST day (25 h fall-back) renders with a > 24 h time axis: ${JSON.stringify(state)}`).toBe(true);
			expect(await pageOverflow(page), "DST chart does not cause horizontal page overflow").toBe(0);
		} finally { await context.close(); }
	});
	test("charging schedule tables fit per width @plan-chart", async ({ browser, adminState, gateway, colorScheme }) => {
		const { context, page } = await openPlan({ browser, adminState, gateway, colorScheme });
		try {
			// The opened all-intervals table stays inside the page at phone widths.
			await page.locator("#energy-plan-schedule details summary").click();
			for (const width of [320, 375, 390, 768]) {
				await page.setViewportSize({ width, height: 844 });
				// Reflow after a resize is asynchronous: poll the measured fit (same conditions as before).
				await expect.poll(async () => {
					const fit = await fitAt(page);
					return fit.overflow === 0 && fit.rows >= 1 && fit.cellsOutside === 0 && fit.proseCells > 0 && fit.proseOverflowing === 0 ? "fits" : JSON.stringify(fit);
				}, { message: `charging schedule tables fit at ${width}px` }).toBe("fits");
				await test.info().attach(`plan-schedule-${width}.png`, { body: await page.locator("#energy-plan-schedule").screenshot(), contentType: "image/png" });
			}
		} finally { await context.close(); }
	});
	test("tooltips never show NaN/undefined/null; no console errors @plan-chart", async ({ browser, adminState, gateway, colorScheme }) => {
		const { context, page, collected } = await openPlan({ browser, adminState, gateway, colorScheme });
		try {
			// Sweep the whole chart width (crossing the NaN-power slot and the all-null gap between the two idle
			// slots) and collect every tooltip body shown. A broken tooltip would print the raw "NaN", "undefined"
			// or "null" of a non-finite datapoint; the shipped filter keeps only finite points.
			const box = await page.locator("#energy-plan-schedule canvas").boundingBox();
			const allLines = [];
			for (let f = 0.02; f <= 0.98; f += 0.04) {
				await page.mouse.move(box.x + box.width * f, box.y + box.height * 0.5);
				await frames(page);
				allLines.push(...await page.evaluate(() => {
					const chart = window.Chart.getChart(document.querySelector("#energy-plan-schedule canvas"));
					return (chart.tooltip.body || []).flatMap((b) => b.lines);
				}));
			}
			await test.info().attach("plan-tooltip-lines.json", { body: JSON.stringify(allLines, null, 2), contentType: "application/json" });
			const broken = allLines.filter((l) => /\bNaN\b|\bundefined\b|\bnull\b/.test(l));
			expect(broken, "no tooltip across the chart shows NaN/undefined/null for a non-finite point").toEqual([]);
			// A finite slot still produces a readable tooltip (the filter did not suppress everything).
			const readable = allLines.filter((l) => /^(Grid charging|No grid charging)/.test(l));
			expect(readable.length, `a finite slot still shows a readable tooltip: ${readable.slice(0, 2).join(" | ")}`).toBeGreaterThan(0);
			expect(allLines.filter((l) => /n\/a/.test(l)), "no tooltip row is an n/a placeholder").toEqual([]);
			expect(collected.filter((p) => !/style-src|Content Security Policy|status of 503|Failed to load resource/.test(p)), "no unexpected console or page errors").toEqual([]);
		} finally { await context.close(); }
	});
});
